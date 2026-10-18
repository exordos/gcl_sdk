#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Unified NF responder for the ovs_evpn overlay (DHCP+DNS+proxy).

One process per hypervisor, one thread per VNI. Each thread enters the
VNI's namespace (``vni<n>``, port ``nf<n>``) and serves only its guests:
DHCP and DNS on a raw AF_PACKET socket, the metadata/netboot proxy on the
well-known address. The namespace keeps overlapping tenant addresses apart
and makes the VNI of a frame implicit.

The process is shared by every VNI, so a frame this loop cannot parse must
cost one request, not the thread: every handler is bounded and guarded.

Guests send DNS to the gateway (ARP-answered by evpn_connector); br-int
punts port 53 here before NORMAL, and the reply echoes the query's
destination as its source.
"""

import argparse
import glob
import ipaddress
import os
import socket
import socketserver
import struct
import threading
import sys
import time

from gcl_sdk.agents.universal.drivers import evpn_proxy
from gcl_sdk.agents.universal.drivers import evpn_records

ETH_P_ALL = 0x0003
METADATA_IP = evpn_proxy.METADATA_IP
# How often a worker re-reads which ports its proxy should listen on.
LISTENER_SWEEP_SECONDS = 5
# How often the supervisor rescans for VNIs coming and going.
RESCAN_SECONDS = 5
DNS_PORT = 53
DHCP_SERVER_PORT = 67

DHCP_MAGIC = bytes([99, 130, 83, 99])
DISCOVER, OFFER, REQUEST, DECLINE, ACK, NAK, RELEASE, INFORM = range(1, 9)
LEASE_SECONDS = 86400
RESPONDER_MAC = bytes.fromhex("020000000001")
BROADCAST_MAC = b"\xff" * 6
UPSTREAM_TIMEOUT = 3.0
# Upstream relays one VNI may have in flight: bounds what its guests can
# hold of the shared process.
UPSTREAM_WORKERS = 8
# A reply over max(512, guest MTU - headers) is truncated, not dropped: the
# frame would exceed the overlay MTU. 512 is the no-EDNS0 floor.
DNS_UDP_MIN = 512
_DNS_FRAME_OVERHEAD = 14 + 20 + 8


# --- records --------------------------------------------------------------


def _load_records(records_dir, zone_suffix="internal", dns_records=None):
    """Load one VNI's records into a mac index and its DNS zone."""
    by_mac = {}
    zone = {"a": {}, "ptr": {}}
    for rec in evpn_records.guest_records(records_dir):
        mac = (rec.get("mac") or "").lower()
        ipv4 = rec.get("ipv4")
        if not mac or not ipv4:
            continue
        by_mac[mac] = rec
    for rec in by_mac.values() if dns_records is None else dns_records:
        ipv4 = rec.get("ipv4")
        name = rec.get("name")
        # A port without the `dns` function is not published in the zone.
        if not rec.get("dns_enabled", True):
            continue
        if name and ipv4:
            suffix = (zone_suffix or "internal").strip(".")
            fqdn = ("%s.%s" % (name, suffix)).lower()
            zone["a"][fqdn] = ipv4
            rev = ".".join(reversed(ipv4.split("."))) + ".in-addr.arpa"
            zone["ptr"][rev] = fqdn
    return by_mac, zone


# --- shared wire helpers --------------------------------------------------


def _cksum(data):
    if len(data) % 2:
        data += b"\x00"
    s = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return (~s) & 0xFFFF


def _eth(dst_mac, src_mac, ethertype, payload):
    """A plain Ethernet frame — no 802.1Q; br-int maps it to the guest."""
    hdr = dst_mac + src_mac
    hdr += struct.pack("!H", ethertype)
    return hdr + payload


def _ipv4_udp(src_ip, dst_ip, sport, dport, payload, tos=0):
    """An IPv4+UDP packet (UDP checksum left out, as IPv4 allows)."""
    udp_len = 8 + len(payload)
    ip = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        tos,
        20 + udp_len,
        0,
        0,
        64,
        17,
        0,
        socket.inet_aton(src_ip),
        socket.inet_aton(dst_ip),
    )
    ip = ip[:10] + struct.pack("!H", _cksum(ip)) + ip[12:]
    return ip + struct.pack("!HHHH", sport, dport, udp_len, 0) + payload


def parse_eth(frame):
    """Return (ethertype, src_mac, l3_offset) or None.

    Untagged: the NF port is an access port, so OVS pops the VLAN.
    """
    if len(frame) < 14:
        return None
    return struct.unpack("!H", frame[12:14])[0], frame[6:12], 14


# --- DHCP -----------------------------------------------------------------


def parse_request(data):
    if len(data) < 240 or data[236:240] != DHCP_MAGIC:
        return None
    xid = data[4:8]
    flags = struct.unpack("!H", data[10:12])[0]
    chaddr = data[28:34]
    client_mac = ":".join("%02x" % b for b in chaddr)
    msg_type = None
    i = 240
    while i < len(data):
        opt = data[i]
        if opt == 255:
            break
        if opt == 0:
            i += 1
            continue
        length = data[i + 1]
        if opt == 53 and length == 1:
            msg_type = data[i + 2]
        i += 2 + length
    return xid, flags, client_mac, msg_type


def _opt(code, value):
    return bytes([code, len(value)]) + value


def _lease_seconds(rec):
    """The dhcp function's lease time, clamped; a bad record must not raise
    and take the shared responder down.
    """
    try:
        lease = int(rec.get("lease_seconds") or LEASE_SECONDS)
    except (TypeError, ValueError):
        return LEASE_SECONDS
    return min(max(lease, 60), 0xFFFFFFFF)


def build_reply(xid, flags, client_mac, reply_type, rec, cidr, server_ip, dhcp=None):
    net = ipaddress.ip_network(cidr, strict=False)
    chaddr = bytes(int(b, 16) for b in client_mac.split(":"))
    # Netboot filename: sent both in BOOTP `file` and as option 67 — ROMs
    # differ in which one they read.
    filename = ((dhcp or {}).get("filename") or "").encode()[:127]
    pkt = struct.pack(
        "!BBBB4sHH4s4s4s4s16s64s128s",
        2,
        1,
        6,
        0,
        xid,
        0,
        flags,
        b"\x00" * 4,
        socket.inet_aton(rec["ipv4"]),
        socket.inet_aton(server_ip),
        b"\x00" * 4,
        chaddr + b"\x00" * 10,
        b"\x00" * 64,
        filename + b"\x00" * (128 - len(filename)),
    )
    opts = DHCP_MAGIC
    opts += _opt(53, bytes([reply_type]))
    opts += _opt(54, socket.inet_aton(server_ip))
    opts += _opt(51, struct.pack("!I", _lease_seconds(dhcp or {})))
    opts += _opt(1, socket.inet_aton(str(net.netmask)))
    router = evpn_records.default_gateway(rec.get("routers"))
    if router:
        opts += _opt(3, socket.inet_aton(router))
    # The gateway is the resolver: br-int punts its port 53 to this NF.
    dns = rec.get("dns_servers") or ([router] if router else [])
    if dns:
        opts += _opt(6, b"".join(socket.inet_aton(d) for d in dns))
    if rec.get("mtu"):
        opts += _opt(26, struct.pack("!H", int(rec["mtu"])))
    if filename:
        opts += _opt(67, filename)
    opts += bytes([255])
    return pkt + opts


def handle_dhcp(frame, off, by_mac, dhcp=None):
    ihl = (frame[off] & 0x0F) * 4
    if frame[off + 9] != 17:  # UDP
        return None
    udp = off + ihl
    if struct.unpack("!H", frame[udp + 2 : udp + 4])[0] != 67:
        return None
    req = parse_request(frame[udp + 8 :])
    if not req:
        return None
    xid, flags, client_mac, msg_type = req
    if msg_type not in (DISCOVER, REQUEST):
        return None
    if bytes.fromhex(client_mac.replace(":", "")) != frame[6:12]:
        # A guest asks for its own lease, not for whichever MAC it writes in.
        return None
    rec = by_mac.get(client_mac.lower())
    if not rec:
        return None
    if not rec.get("dhcp_enabled", True):
        # No `dhcp` function: stay silent, the guest manages its addressing.
        return None
    configs = (dhcp or {}).get("_resource_configs")
    if configs is not None:
        subnet_uuid = rec.get("_subnet_uuid")
        dhcp = next(
            (
                config
                for config in configs.values()
                if subnet_uuid and config.get("_subnet_uuid") == subnet_uuid
            ),
            None,
        )
        if dhcp is None:
            return None
    gateway = evpn_records.default_gateway(rec.get("routers")) or rec["ipv4"]
    cidr = rec.get("cidr") or "%s/24" % rec["ipv4"]
    reply_type = OFFER if msg_type == DISCOVER else ACK
    payload = build_reply(
        xid, flags, client_mac, reply_type, rec, cidr, gateway, dhcp=dhcp
    )
    # Unicast to the asking guest so no neighbour sees its lease; the IP
    # destination stays broadcast, as a client with no address needs.
    return _eth(
        bytes.fromhex(client_mac.replace(":", "")),
        RESPONDER_MAC,
        0x0800,
        _ipv4_udp(gateway, "255.255.255.255", 67, 68, payload, tos=0x10),
    )


# --- DNS ------------------------------------------------------------------


def _read_name(data, off):
    labels = []
    jumped = False
    end = off
    # A compression pointer can point at itself (``C0 0C``); cap jumps at
    # the message length so a hostile frame cannot spin the responder.
    jumps = 0
    while True:
        if off >= len(data):
            break
        length = data[off]
        if length & 0xC0 == 0xC0:
            if off + 1 >= len(data):
                break
            if not jumped:
                end = off + 2
            jumps += 1
            if jumps > len(data):
                break
            off = struct.unpack("!H", data[off : off + 2])[0] & 0x3FFF
            jumped = True
            continue
        if length == 0:
            if not jumped:
                end = off + 1
            break
        off += 1
        labels.append(data[off : off + length].decode("latin-1"))
        off += length
        if len(labels) > 128:
            # A DNS name has at most 127 labels; refuse a runaway.
            break
    return ".".join(labels), end


def _encode_name(name):
    out = b""
    for label in name.split("."):
        if label:
            out += bytes([len(label)]) + label.encode("latin-1")
    return out + b"\x00"


def parse_dns_question(payload):
    if len(payload) < 12 or struct.unpack("!H", payload[4:6])[0] < 1:
        return None
    qname, off = _read_name(payload, 12)
    qtype = struct.unpack("!H", payload[off : off + 2])[0]
    return qname.lower(), qtype, off + 4


def build_dns_response(payload, qend, answers):
    header = payload[0:2] + struct.pack("!H", 0x8180)
    header += struct.pack("!HHHH", 1, len(answers), 0, 0)
    body = b""
    for rtype, rdata in answers:
        body += struct.pack("!HHHIH", 0xC00C, rtype, 1, 60, len(rdata)) + rdata
    return header + payload[12:qend] + body


def resolve_internal(zone, qname, qtype):
    if qtype == 1 and qname in zone["a"]:
        return [(1, socket.inet_aton(zone["a"][qname]))]
    if qtype == 12 and qname in zone["ptr"]:
        return [(12, _encode_name(zone["ptr"][qname]))]
    return None


def usable_forwarder(server):
    """Whether the responder may relay to this upstream at all.

    Tenants name forwarders, and the relay leaves the namespace from the
    host's side of the guard. An IPv4 literal only: a name would be
    resolved by the host's resolver. Never loopback, link-local (the
    metadata address), multicast or reserved space.
    """
    try:
        address = ipaddress.IPv4Address(str(server))
    except ValueError:
        return False
    return not (
        address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def forward_upstream(payload, forwarders):
    """Relay a query to the first upstream that answers.

    The socket is connected and the transaction id checked, so an off-path
    host cannot race the resolver and poison the answer.
    """
    want_id = payload[:2]
    for server in forwarders:
        if not usable_forwarder(server):
            # Silently: a tenant's query must not be a line in the host's log.
            continue
        up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            up.settimeout(UPSTREAM_TIMEOUT)
            up.connect((server, 53))
            up.send(payload)
            deadline = time.monotonic() + UPSTREAM_TIMEOUT
            while time.monotonic() < deadline:
                reply = up.recv(4096)
                if reply[:2] == want_id:
                    return reply
        except OSError:
            continue
        finally:
            up.close()
    return None


class ForwarderPool:
    """Upstream queries, off the VNI's serving thread.

    A blocking relay would stall the VNI's DHCP and DNS for every guest.
    The pool is capped; a query arriving when it is full is dropped (the
    client retries).
    """

    def __init__(self, send, limit=UPSTREAM_WORKERS):
        self._send = send
        self._limit = limit
        self._lock = threading.Lock()
        self._running = 0

    def submit(self, payload, forwarders, reply_to):
        """Relay ``payload``; hand what comes back to ``reply_to(reply)``."""
        with self._lock:
            if self._running >= self._limit:
                return False
            self._running += 1
        threading.Thread(
            target=self._run,
            args=(payload, forwarders, reply_to),
            daemon=True,
        ).start()
        return True

    def _run(self, payload, forwarders, reply_to):
        try:
            reply = forward_upstream(payload, forwarders)
            if reply is None:
                return
            frame = reply_to(reply)
            if frame:
                self._send(frame)
        except Exception as e:  # never take the pool slot's thread down
            sys.stderr.write("evpn-nf: upstream relay failed: %r\n" % (e,))
        finally:
            with self._lock:
                self._running -= 1


def platform_answer(qname, qtype, platform_names):
    """The metadata address, for the platform's own names (A only).

    The installation's real address is unroutable from an overlay; the
    hypervisor's metadata address serves every platform port.
    """
    if qtype != 1 or not platform_names:
        return None
    name = qname.rstrip(".").lower()
    if name in {str(n).rstrip(".").lower() for n in platform_names}:
        # (type, rdata), the shape `build_dns_response` expects.
        return [(1, socket.inet_aton(METADATA_IP))]
    return None


def truncate_dns(payload, qend):
    """The same query with TC set, sending the client to TCP/53."""
    header = payload[0:2] + struct.pack("!H", 0x8380)  # QR + AA + TC + RD/RA
    header += struct.pack("!HHHH", 1, 0, 0, 0)
    return header + payload[12:qend]


def dns_reply_frame(src_mac, src_ip, resolver_ip, sport, reply):
    """Wrap a DNS payload as a whole frame back to the guest that asked."""
    packet = _ipv4_udp(resolver_ip, src_ip, 53, sport, reply)
    return _eth(src_mac, RESPONDER_MAC, 0x0800, packet)


def resolve_locally(qname, qtype, zone, platform_names):
    """What this namespace can answer without asking anyone."""
    answers = platform_answer(qname, qtype, platform_names)
    if answers is None and zone:
        answers = resolve_internal(zone, qname, qtype)
    return answers


def handle_dns(
    frame, off, src_mac, by_mac, zone, forwarders, platform_names=(), *, pool
):
    ihl = (frame[off] & 0x0F) * 4
    if frame[off + 9] != 17:  # UDP
        return None
    # br-int punts port 53 here; echo the query's dst as the reply src so
    # the guest accepts it as coming from the resolver it addressed.
    src_ip = socket.inet_ntoa(frame[off + 12 : off + 16])
    resolver_ip = socket.inet_ntoa(frame[off + 16 : off + 20])
    udp = off + ihl
    sport, dport = struct.unpack("!HH", frame[udp : udp + 4])
    if dport != 53:
        return None
    q = parse_dns_question(frame[udp + 8 :])
    if not q:
        return None
    qname, qtype, qend = q
    payload = frame[udp + 8 :]
    guest_mac = ":".join("%02x" % b for b in src_mac)
    rec = by_mac.get(guest_mac.lower())
    if not rec:
        return None  # unknown guest: not ours to answer
    mtu = int(rec.get("mtu") or 1450)

    def _frame(reply):
        if reply is None:
            return None
        if len(reply) > max(DNS_UDP_MIN, mtu - _DNS_FRAME_OVERHEAD):
            reply = truncate_dns(payload, qend)
        return dns_reply_frame(src_mac, src_ip, resolver_ip, sport, reply)

    answers = resolve_locally(qname, qtype, zone, platform_names)
    if answers is not None:
        return _frame(build_dns_response(payload, qend, answers))
    pool.submit(payload, forwarders, _frame)
    return None


# --- loop -----------------------------------------------------------------


_CACHE = evpn_records.DirCache()


def _load_vni(records_dir):
    nfs = {
        kind: evpn_records.nf_config(records_dir, kind)
        for kind in evpn_records.NF_KINDS
    }
    dns = nfs.get("dns") or {}
    by_mac, zone = _load_records(
        records_dir, dns.get("zone_suffix"), dns.get("records")
    )
    return nfs, by_mac, zone


class RecordCache:
    """This VNI's guests and functions, reloaded when their files move."""

    def __init__(self, records_dir):
        self._dir = records_dir
        self.by_mac = {}
        self.zone = {"a": {}, "ptr": {}}
        self.nfs = {}

    @property
    def by_ip(self):
        """The same guests, indexed by IP (how a TCP peer is identified)."""
        return {rec["ipv4"]: rec for rec in self.by_mac.values() if rec.get("ipv4")}

    def refresh(self):
        self.nfs, self.by_mac, self.zone = _CACHE.get(
            self._dir,
            "records",
            _load_vni,
            watch=tuple(
                evpn_records.nf_config_file(self._dir, k) for k in evpn_records.NF_KINDS
            ),
        )


class _DnsTcpHandler(socketserver.StreamRequestHandler):
    """DNS over TCP, for answers truncated over UDP."""

    timeout = 10
    cache = None

    def handle(self):
        # Own refresh: a TCP-only guest never drives the raw loop's.
        self.cache.refresh()
        rec = self.cache.by_ip.get(self.client_address[0])
        if not rec or not rec.get("dns_enabled", True):
            return  # unknown guest, or one this function does not serve
        header = self.rfile.read(2)
        if len(header) != 2:
            return
        payload = self.rfile.read(struct.unpack("!H", header)[0])
        q = parse_dns_question(payload)
        if not q:
            return
        qname, qtype, qend = q
        dns = self.cache.nfs.get("dns") or {}
        answers = resolve_locally(
            qname, qtype, self.cache.zone, dns.get("platform_names") or ()
        )
        if answers is not None:
            reply = build_dns_response(payload, qend, answers)
        else:
            reply = forward_upstream(payload, dns.get("forwarders") or ())
        if reply is None:
            return
        self.wfile.write(struct.pack("!H", len(reply)) + reply)


class _DnsTcpServer(evpn_proxy.BoundedThreadingMixIn, socketserver.TCPServer):
    """This VNI's tcp/53, thread-bounded so one guest cannot exhaust the
    shared process; the handler timeout frees idle slots.
    """


def serve_dns_tcp(cache, port=DNS_PORT):
    """Answer tcp/53 in the namespace this thread is already in."""
    handler = type("VniDnsTcpHandler", (_DnsTcpHandler,), {"cache": cache})
    srv = _DnsTcpServer(("", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# --- per-VNI workers ------------------------------------------------------

NETNS_RUN_DIR = "/var/run/netns"
CLONE_NEWNET = 0x40000000


def enter_netns(name):
    """Move this thread (and threads it starts) into a netns, for good."""
    with open(os.path.join(NETNS_RUN_DIR, name)) as fl:
        os.setns(fl.fileno(), CLONE_NEWNET)


def _silence_kernel_ports():
    """Bind (never read) udp/53 and udp/67 so the kernel does not answer.

    The raw socket is invisible to the UDP layer; without a bound socket
    the kernel sends ICMP port-unreachable, which races and usually beats
    the responder's reply.
    """
    held = []
    for port in (DNS_PORT, DHCP_SERVER_PORT):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", port))
        except OSError as e:
            # Someone else holds it: the kernel stays quiet either way.
            sock.close()
            sys.stderr.write("evpn-nf: udp/%d not held: %r\n" % (port, e))
            continue
        held.append(sock)
    return held


def serve_vni(vni, records_dir, interface, stop):
    """Serve one VNI's guests from inside its namespace."""
    enter_netns(evpn_records.vni_ns(vni))
    # The raw socket first: on a namespace still being built this bind fails
    # ("Network is down") and the supervisor retries, which only converges
    # if the failed attempt left no listeners holding ports.
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    try:
        sock.bind((interface, 0))
    except OSError:
        sock.close()
        raise
    sock.settimeout(1.0)
    silencers = _silence_kernel_ports()
    # Held so this worker can close them: a socket outliving the worker
    # keeps the namespace alive after the driver deleted it.
    listeners = evpn_proxy.run_in_thread(records_dir)
    cache = RecordCache(records_dir)
    # Pool threads only send; the lock keeps whole frames from interleaving.
    send_lock = threading.Lock()

    def _send(frame):
        with send_lock:
            sock.send(frame)

    pool = ForwarderPool(_send)
    cache.refresh()
    try:
        tcp = serve_dns_tcp(cache)
    except OSError as e:
        # Degrade: DHCP, UDP DNS and the proxy still work without it.
        sys.stderr.write("evpn-nf: vni %s has no tcp/53 listener: %r\n" % (vni, e))
        tcp = None
    sys.stderr.write("evpn-nf: serving vni %s on %s\n" % (vni, interface))
    sys.stderr.flush()
    next_sweep = 0.0
    try:
        while not stop.is_set():
            if time.monotonic() >= next_sweep:
                # Doors change under a running VNI (a PaaS installed later);
                # the wanted set is cached against one file's stat.
                next_sweep = time.monotonic() + LISTENER_SWEEP_SECONDS
                try:
                    listeners.reconcile(
                        records_dir, evpn_proxy.wanted_ports(records_dir)
                    )
                except Exception as e:
                    sys.stderr.write(
                        "evpn-nf: vni %s could not sweep its listeners: %r\n" % (vni, e)
                    )
            try:
                frame = sock.recv(4096)
            except socket.timeout:
                continue
            parsed = parse_eth(frame)
            if not parsed:
                continue
            ethertype, src_mac, off = parsed
            cache.refresh()
            reply = None
            if ethertype == 0x0800:
                try:
                    reply = handle_dhcp(frame, off, cache.by_mac, cache.nfs.get("dhcp"))
                    if reply is None:
                        reply = handle_dns(
                            frame,
                            off,
                            src_mac,
                            cache.by_mac,
                            cache.zone,
                            (cache.nfs.get("dns") or {}).get("forwarders") or (),
                            (cache.nfs.get("dns") or {}).get("platform_names") or (),
                            pool=pool,
                        )
                except Exception as e:
                    # A bad frame costs one request, never the thread.
                    sys.stderr.write("evpn-nf: vni %s dropped a frame: %r\n" % (vni, e))
                    reply = None
            if reply:
                try:
                    _send(reply)
                except OSError as e:
                    sys.stderr.write("evpn-nf: vni %s could not reply: %r\n" % (vni, e))
    finally:
        # Release everything: a leftover listener holds its port and the
        # namespace from the supervisor's next attempt.
        if tcp is not None:
            tcp.shutdown()
            tcp.server_close()
        listeners.shutdown()
        sock.close()
        for held in silencers:
            held.close()


def served_vnis(records_dir):
    """The VNIs this host has guest records for.

    A directory holding only ``nf-*.json`` has no namespace behind it;
    serving it would fail and restart forever.
    """
    try:
        names = os.listdir(records_dir)
    except OSError:
        return set()
    served = set()
    for name in names:
        path = os.path.join(records_dir, name)
        if not name.isdigit() or not os.path.isdir(path):
            continue
        if any(
            not os.path.basename(rec).startswith(evpn_records.NF_CONFIG_PREFIX)
            for rec in glob.glob(os.path.join(path, "*.json"))
        ):
            served.add(name)
    return served


def supervise(records_dir):
    """Keep one worker thread per VNI in `served_vnis`."""
    workers = {}
    while True:
        want = served_vnis(records_dir)
        for vni in want - set(workers):
            stop = threading.Event()
            th = threading.Thread(
                target=_guarded_worker,
                args=(vni, records_dir, stop),
                daemon=True,
            )
            th.start()
            workers[vni] = (th, stop)
        for vni in set(workers) - want:
            _th, stop = workers.pop(vni)
            stop.set()
        for vni, (th, stop) in list(workers.items()):
            if not th.is_alive():
                workers.pop(vni)  # restarted on the next round
        time.sleep(RESCAN_SECONDS)


def _guarded_worker(vni, records_dir, stop):
    try:
        serve_vni(
            vni,
            os.path.join(records_dir, str(vni)),
            evpn_records.nf_port(vni),
            stop=stop,
        )
    except Exception as e:
        # Not fatal: the supervisor restarts it on the next round.

        sys.stderr.write("evpn-nf: vni %s worker stopped: %r\n" % (vni, e))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--records-dir",
        required=True,
        help="one subdirectory per VNI, named by it, holding its guests",
    )
    args = ap.parse_args()
    supervise(args.records_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
