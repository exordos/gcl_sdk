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

import json
import os
import socket
import struct
import threading
import time
from unittest import mock

import pytest

from gcl_sdk.agents.universal.drivers import evpn_nf as nf

MAC = "52:54:00:aa:bb:cc"
MAC_B = bytes.fromhex("525400aabbcc")


# --- helpers --------------------------------------------------------------


def _rec(**over):
    rec = {
        "mac": MAC,
        "ipv4": "10.42.0.7",
        "vni": 10005,
        "name": "hostx",
        "zone_suffix": "internal",
        "routers": [{"to": "0.0.0.0/0", "via": "10.42.0.254"}],
        "dns_servers": [],
        "mtu": 1450,
    }
    rec.update(over)
    return rec


def _dhcp_payload(mac=MAC, msg_type=nf.DISCOVER):
    chaddr = bytes(int(b, 16) for b in mac.split(":")) + b"\x00" * 10
    pkt = struct.pack(
        "!BBBB4sHH4s4s4s4s16s64s128s",
        1,
        1,
        6,
        0,
        b"\x12\x34\x56\x78",
        0,
        0x8000,
        b"\x00" * 4,
        b"\x00" * 4,
        b"\x00" * 4,
        b"\x00" * 4,
        chaddr,
        b"\x00" * 64,
        b"\x00" * 128,
    )
    return pkt + nf.DHCP_MAGIC + nf._opt(53, bytes([msg_type])) + bytes([255])


def _udp_frame(smac, sip, dip, sport, dport, payload):
    udp = struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload
    ip = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        20 + len(udp),
        0,
        0,
        64,
        17,
        0,
        socket.inet_aton(sip),
        socket.inet_aton(dip),
    )
    ip = ip[:10] + struct.pack("!H", nf._cksum(ip)) + ip[12:]
    dst = b"\xff" * 6 if dport == 67 else bytes.fromhex("123456789ab0")
    return nf._eth(dst, smac, 0x0800, ip + udp)


def _options(reply_payload):
    assert reply_payload[236:240] == nf.DHCP_MAGIC
    data = reply_payload[240:]
    opts = {}
    i = 0
    while i < len(data):
        code = data[i]
        if code == 255:
            break
        length = data[i + 1]
        opts[code] = data[i + 2 : i + 2 + length]
        i += 2 + length
    return opts


def _dns_query(qname, qtype):
    q = struct.pack("!HHHHHH", 0x4321, 0x0100, 1, 0, 0, 0)
    return q + nf._encode_name(qname) + struct.pack("!HH", qtype, 1)


# --- DHCP -----------------------------------------------------------------


def test_parse_discover():
    xid, flags, mac, mtype = nf.parse_request(_dhcp_payload())
    assert mac == MAC and mtype == nf.DISCOVER and xid == b"\x12\x34\x56\x78"


def test_offer_fields_hand_out_gateway_as_resolver():
    reply = nf.build_reply(
        b"\x00" * 4, 0x8000, MAC, nf.OFFER, _rec(), "10.42.0.0/24", "10.42.0.254"
    )
    assert reply[0] == 2  # BOOTREPLY
    assert socket.inet_ntoa(reply[16:20]) == "10.42.0.7"
    opts = _options(reply)
    assert opts[53] == bytes([nf.OFFER])
    assert socket.inet_ntoa(opts[54]) == "10.42.0.254"  # server id
    assert socket.inet_ntoa(opts[1]) == "255.255.255.0"  # mask
    assert socket.inet_ntoa(opts[3]) == "10.42.0.254"  # router
    # no explicit dns_servers -> the gateway is handed out as the resolver
    assert socket.inet_ntoa(opts[6]) == "10.42.0.254"
    assert struct.unpack("!H", opts[26])[0] == 1450  # mtu


def test_explicit_dns_servers_win_over_gateway():
    reply = nf.build_reply(
        b"\x00" * 4,
        0x8000,
        MAC,
        nf.OFFER,
        _rec(dns_servers=["10.9.9.9"]),
        "10.42.0.0/24",
        "10.42.0.254",
    )
    assert socket.inet_ntoa(_options(reply)[6]) == "10.9.9.9"


def test_record_without_default_router_omits_router_and_dns():
    rec = _rec(routers=[{"to": "10.42.0.0/24", "via": "10.42.0.1"}])
    opts = _options(
        nf.build_reply(
            b"\x00" * 4, 0x8000, MAC, nf.OFFER, rec, "10.42.0.0/24", "10.42.0.7"
        )
    )
    assert 3 not in opts and 6 not in opts


def test_handle_dhcp_discover_yields_offer():
    frame = _udp_frame(MAC_B, "0.0.0.0", "255.255.255.255", 68, 67, _dhcp_payload())
    et, src, off = nf.parse_eth(frame)
    by_mac = {MAC: _rec()}
    reply = nf.handle_dhcp(frame, off, by_mac)
    assert reply is not None
    payload = reply[14 + 20 + 8 :]  # untagged eth + ip + udp
    assert _options(payload)[53] == bytes([nf.OFFER])


def test_a_guest_is_leased_only_as_the_mac_it_sends_from():
    """`chaddr` is the guest's own claim: naming a neighbour's MAC in it
    had the responder answer with the neighbour's lease."""
    neighbour = bytes.fromhex("525400000099")
    frame = _udp_frame(neighbour, "0.0.0.0", "255.255.255.255", 68, 67, _dhcp_payload())
    _, _, off = nf.parse_eth(frame)
    assert nf.handle_dhcp(frame, off, {MAC: _rec()}) is None


def test_the_reply_carries_no_vlan_tag():
    """The namespace says which VNI this is; br-int puts the reply on the
    guest's access port by an explicit flow. Forging a tag here would be a
    second, disagreeing source of truth."""
    frame = _udp_frame(MAC_B, "0.0.0.0", "255.255.255.255", 68, 67, _dhcp_payload())
    _, _, off = nf.parse_eth(frame)
    reply = nf.handle_dhcp(frame, off, {MAC: _rec()})
    assert struct.unpack("!H", reply[12:14])[0] == 0x0800


def test_handle_dhcp_declines_dns_port():
    frame = _udp_frame(MAC_B, "10.42.0.7", "10.42.0.254", 4000, 53, b"x")
    _, _, off = nf.parse_eth(frame)
    assert nf.handle_dhcp(frame, off, {MAC: _rec()}) is None


# --- DNS ------------------------------------------------------------------


def test_dns_name_codec_roundtrip():
    enc = nf._encode_name("hostx.internal")
    name, end = nf._read_name(enc, 0)
    assert name == "hostx.internal" and end == len(enc)


def test_read_name_terminates_on_a_self_referential_pointer():
    # A compression pointer that points at itself (C0 00 -> offset 0) used to
    # spin _read_name forever and hang the single-threaded responder, taking
    # DHCP and DNS down for every tenant on the host. It must return instead.
    data = b"\xc0\x00" + b"\x00" * 8
    name, end = nf._read_name(data, 0)
    assert name == "" and end == 2


def test_read_name_terminates_on_a_forward_pointer_chain():
    data = b"\x00\x00\xc0\x02"  # pointer at offset 2 -> offset 2 (itself)
    name, _ = nf._read_name(data, 2)
    assert name == ""


def test_parse_dns_question():
    q = _dns_query("hostx.internal", 1)
    qname, qtype, qend = nf.parse_dns_question(q)
    assert qname == "hostx.internal" and qtype == 1 and qend == len(q)


def test_reply_packets_keep_their_bytes():
    """The DHCP reply (TOS 0x10, to broadcast) and the DNS reply frame."""
    dhcp = nf._ipv4_udp("10.42.0.1", "255.255.255.255", 67, 68, b"dhcp-payload-x", 0x10)
    assert dhcp.hex() == (
        "4510002a00000000401170890a2a0001ffffffff0043004400160000"
        "646863702d7061796c6f61642d78"
    )
    frame = nf.dns_reply_frame(
        bytes.fromhex("020000000007"), "10.42.0.7", "10.42.0.1", 40000, b"dns-reply"
    )
    assert frame.hex() == (
        "020000000007020000000001080045000025000000004011666d0a2a0001"
        "0a2a000700359c4000110000646e732d7265706c79"
    )


def test_load_records_builds_mac_index_and_zone(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(_rec()))
    by_mac, zone = nf._load_records(str(tmp_path))
    assert by_mac[MAC]["ipv4"] == "10.42.0.7"
    assert zone["a"]["hostx.internal"] == "10.42.0.7"
    assert zone["ptr"]["7.0.42.10.in-addr.arpa"] == "hostx.internal"


def test_handle_dns_answers_from_this_namespaces_zone():
    by_mac = {MAC: _rec()}
    zone = {"a": {"hostx.internal": "10.42.0.7"}, "ptr": {}}
    frame = _udp_frame(
        MAC_B, "10.42.0.7", "10.42.0.254", 5000, 53, _dns_query("hostx.internal", 1)
    )
    _, src, off = nf.parse_eth(frame)
    reply = nf.handle_dns(frame, off, src, by_mac, zone, ["8.8.8.8"], pool=None)
    assert reply is not None
    # the reply echoes the resolver the guest addressed, untagged
    assert socket.inet_ntoa(reply[14 + 12 : 14 + 16]) == "10.42.0.254"
    dns = reply[14 + 20 + 8 :]
    _, qend = nf._read_name(dns, 12)
    _, rtype, _, _, _ = struct.unpack("!HHHIH", dns[qend + 4 : qend + 16])
    assert rtype == 1
    assert socket.inet_ntoa(dns[qend + 16 : qend + 20]) == "10.42.0.7"


def test_handle_dns_unknown_guest_drops():
    frame = _udp_frame(
        bytes.fromhex("aaaaaaaaaaaa"),
        "10.42.0.9",
        "10.42.0.254",
        5000,
        53,
        _dns_query("x.internal", 1),
    )
    _, src, off = nf.parse_eth(frame)
    assert (
        nf.handle_dns(
            frame, off, src, {MAC: _rec()}, {"a": {}, "ptr": {}}, ["8.8.8.8"], pool=None
        )
        is None
    )


def test_a_vni_with_only_function_settings_is_not_served(tmp_path):
    """The control plane delivers a network's function settings to a host
    as their own resources, so a VNI's directory outlives the guests it was
    made for. Serving one means entering a namespace the driver had no
    reason to create — a worker that fails, restarts and fails again for as
    long as the directory stays."""
    orphan = tmp_path / "10000"
    orphan.mkdir()
    (orphan / "nf-dns.json").write_text('{"forwarders": []}')
    (orphan / "nf-dhcp.json").write_text("{}")

    served = tmp_path / "10001"
    served.mkdir()
    (served / "nf-dns.json").write_text('{"forwarders": []}')
    (served / "b0b0.json").write_text('{"mac": "%s", "ipv4": "10.42.0.7"}' % MAC)

    assert nf.served_vnis(str(tmp_path)) == {"10001"}


def test_the_worker_owns_dhcp_and_dns_so_the_kernel_stays_quiet():
    """The responder writes whole frames on a raw socket, which the UDP
    layer knows nothing about: to the kernel nothing listens on 53 or 67,
    and a guest's query to an address the namespace really holds earns an
    immediate ICMP port-unreachable that beats the responder's own reply.
    Holding the ports is what keeps the kernel out of the conversation."""
    bound = []

    class _Sock:
        def setsockopt(self, *args):
            pass

        def bind(self, addr):
            bound.append(addr)

        def close(self):
            pass

    with mock.patch.object(nf.socket, "socket", lambda *a, **kw: _Sock()):
        held = nf._silence_kernel_ports()
    assert bound == [("", nf.DNS_PORT), ("", nf.DHCP_SERVER_PORT)]
    assert len(held) == 2

    # a port someone else already holds is not fatal: the kernel will not
    # refuse for us either, which is all this is here for
    class _Taken(_Sock):
        def bind(self, addr):
            raise OSError(98, "Address already in use")

    with mock.patch.object(nf.socket, "socket", lambda *a, **kw: _Taken()):
        assert nf._silence_kernel_ports() == []


def test_the_guest_is_pointed_back_at_the_proxy(tmp_path):
    """A guest of an overlay can name one host: the metadata address.

    What it fetches through the proxy was written for a client on the
    installation's own networks — the boot script names the kernel, the
    initrd and the boot API by addresses the guest has no route to. Relaying
    that verbatim hands it a script that hangs on the first line.
    """
    from gcl_sdk.agents.universal.drivers import evpn_proxy

    records = tmp_path / "10001"
    records.mkdir()
    (records / "nf-proxy.json").write_text(
        json.dumps(
            {
                "forwards": [
                    "/v1/boots=http://10.30.0.2:11013/v1/boots",
                    "/bios=http://10.30.0.2:8080/bios",
                ],
                # The script arrives on the boot API's port and names the
                # kernel, which is served under a prefix on the metadata
                # address: what a listener *routes* and what a body may
                # *name* are different sets.
                "ports": {"11013": "http://10.30.0.2:11013"},
            }
        )
    )

    handler = evpn_proxy.ProxyHandler.__new__(evpn_proxy.ProxyHandler)
    handler.records_dir = str(records)
    handler.listen_port = evpn_proxy.BOOT_CHAIN_PORT

    # As the boot API really writes it: the artifacts under a prefix, and
    # the API itself named by its bare root in the kernel command line.
    script = (
        b"#!ipxe\n"
        b"kernel http://10.30.0.2:8080/bios/vmlinuz "
        b"gc_boot_api=http://10.30.0.2:11013\n"
        b"initrd http://10.30.0.2:8080/bios/initrd.img\n"
    )
    out = handler._rewrite_upstreams(script, "text/plain").decode()
    assert "10.30.0.2" not in out, "nothing may still name the boot network"
    assert "http://169.254.169.254/bios/vmlinuz" in out
    # The API the script came from is reached on its own port; the artifacts
    # under a prefix on the metadata address. Both have to be rewritten, and
    # each to the place the guest actually reaches it.
    assert "http://169.254.169.254:11013" in out

    # An upstream nobody granted is not ours to rewrite, and a body we
    # cannot read is relayed as it came.
    other = b"see http://10.30.0.9:8080/bios/vmlinuz"
    assert handler._rewrite_upstreams(other, "text/plain") == other
    assert handler._rewrite_upstreams(script, "image/png") == script


def test_the_platform_answers_as_the_hypervisor_inside_a_tenant():
    """An installed guest asks for the core by name and is given the address
    the installation answers with — one an overlay guest has no route to, so
    its agent restarts for ever saying only that it cannot connect. Inside a
    tenant's namespace the platform's own names resolve to the hypervisor
    serving it, where every platform port is answered."""
    names = ["core.local.genesis-core.tech"]
    # The shape matters as much as the value: the builder writes (type,
    # rdata), and a bare address here made every such query die in it.
    assert nf.platform_answer("core.local.genesis-core.tech", 1, names) == [
        (1, socket.inet_aton("169.254.169.254"))
    ]
    # ... which is exactly what the builder can consume.
    query = _dns_query("core.local.genesis-core.tech", 1)
    assert nf.build_dns_response(
        query,
        nf.parse_dns_question(query)[2],
        nf.platform_answer("core.local.genesis-core.tech", 1, names),
    )
    # Case and a trailing dot are the same name on the wire.
    assert nf.platform_answer("Core.Local.Genesis-Core.Tech.", 1, names)

    # A door held open deliberately, not a resolver that answers for anyone:
    # another name, another type, or no names configured get nothing.
    assert nf.platform_answer("example.com", 1, names) is None
    assert nf.platform_answer("core.local.genesis-core.tech", 28, names) is None
    assert nf.platform_answer("core.local.genesis-core.tech", 1, []) is None


def _handler_for(records, port=11013):
    from gcl_sdk.agents.universal.drivers import evpn_proxy

    handler = evpn_proxy.ProxyHandler.__new__(evpn_proxy.ProxyHandler)
    handler.records_dir = str(records)
    handler.listen_port = port
    handler.client_address = ("10.42.0.7", 5000)
    return handler


def _gate(handler, request, method="GET", payload=None, kind="platform"):
    """Ask the gate about a whole request, the way `_dispatch` does.

    The request line is what the handler carries (`self.path`), query
    included — the gate reads the query from there, because that is what is
    forwarded upstream.
    """
    handler.path = request
    return handler._about_the_caller(
        request.split("?", 1)[0], method, payload, kind=kind
    )


def test_a_guest_may_not_list_what_every_machine_reports(tmp_path):
    """The status API does not scope its collections to the caller, so a
    claim cannot narrow a listing to the guest's own business -- and no
    agent lists through here, it reads its own objects by uuid."""
    from gcl_sdk.agents.universal.drivers import evpn_proxy

    me = "11111111-1111-1111-1111-111111111111"
    records = tmp_path / "10001"
    records.mkdir()
    (records / "p.json").write_text(
        json.dumps({"mac": MAC, "ipv4": "10.42.0.7", "node": me})
    )
    handler = _handler_for(records)
    handler.headers = {evpn_proxy.NODE_UUID_HEADER: me}

    for listing in (
        "/v1/agents/",
        "/v1/kind/password/resources/",
        "/v1/kind/password/resources/?node=%s" % me,
        "/v1/node_verifiers/",
    ):
        assert not _gate(handler, listing, "GET", None), listing
    # What the agent does do still goes through.
    assert _gate(handler, "/v1/kind/password/resources/%s" % me, "GET", None)
    assert _gate(handler, "/v1/agents/", "POST", None)


def test_a_guest_may_ask_the_platform_about_itself_only(tmp_path):
    """The platform's ports are its whole API and an overlay guest reaches
    them only through this proxy, so this is the only place that can hold a
    guest to its own business. Without it a tenant reads any machine's boot
    script and any agent's payload — the installation's own included —
    because the boot API answers those to whoever can reach it."""
    from gcl_sdk.agents.universal.drivers import evpn_proxy

    me = "11111111-1111-1111-1111-111111111111"
    other = "22222222-2222-2222-2222-222222222222"
    records = tmp_path / "10001"
    records.mkdir()
    (records / "p.json").write_text(
        json.dumps({"mac": MAC, "ipv4": "10.42.0.7", "node": me})
    )
    handler = _handler_for(records)
    handler.headers = {}

    assert _gate(handler, "/v1/boots/%s" % me, "GET", None)
    assert _gate(handler, "/v1/agents/%s/actions/get_payload" % me, "GET", None)
    assert not _gate(handler, "/v1/boots/%s" % other, "GET", None)
    assert not _gate(handler, "/v1/agents/%s/actions/get_payload" % other, "GET", None)

    # Registration is the exception that proves the rule: an agent that does
    # not exist yet cannot be named in a path, so the body says who it is.
    assert _gate(handler, "/v1/agents/", "POST", json.dumps({"uuid": me}).encode())
    assert not _gate(
        handler, "/v1/agents/", "POST", json.dumps({"uuid": other}).encode()
    )

    # An agent introduces itself in a header, because a collection endpoint
    # has no uuid in its path to do it. The header only has to agree with
    # the record — which record answers is decided by the source address.
    handler.headers = {evpn_proxy.NODE_UUID_HEADER: me}
    assert _gate(handler, "/v1/agents/", "POST", None)
    handler.headers = {evpn_proxy.NODE_UUID_HEADER: other}
    assert not _gate(handler, "/v1/agents/", "POST", None)
    handler.headers = {}

    # A report is an envelope: status and hash on the outside, the object
    # the agent is reporting on the inside. That is where a booting node
    # says which machine it is — reading only the outside refused a guest
    # its own report and left it looping on 403 with nothing else to try.
    report = "/v1/kind/guest_machine/resources/%s" % me
    assert _gate(
        handler,
        report,
        "PUT",
        json.dumps({"status": "IN_PROGRESS", "value": {"uuid": me}}).encode(),
    )
    assert not _gate(
        handler,
        report,
        "PUT",
        json.dumps({"status": "IN_PROGRESS", "value": {"uuid": other}}).encode(),
    )

    # A caller the namespace cannot identify gets nothing, and the metadata
    # address keeps its own allowlist (static artifacts, no tenant data).
    handler.client_address = ("10.42.0.9", 5000)
    assert not _gate(handler, "/v1/boots/%s" % me, "GET", None)
    assert evpn_proxy.caller_uuid(str(records), "10.42.0.7") == me


def test_the_proxy_carries_the_conversation_it_relays(tmp_path):
    """An allowlist of headers looked safer and was not: the platform's
    agents sign their requests, and a header nobody thought to list — the
    nonce — was dropped silently, so the call arrived unauthenticated and
    failed at the far end complaining about something the guest had in fact
    sent. What belongs to this hop is dropped; everything else travels."""
    from gcl_sdk.agents.universal.drivers import evpn_proxy

    carried = {
        h
        for h in (
            "authorization",
            "x-genesis-nonce",
            "x-genesis-node-uuid",
            "content-type",
            "accept",
        )
        if h not in evpn_proxy.ProxyHandler._HOP_BY_HOP
    }
    assert len(carried) == 5, "nothing the caller signs with is curated away"
    for own in ("host", "connection", "content-length", "transfer-encoding"):
        assert own in evpn_proxy.ProxyHandler._HOP_BY_HOP


def test_the_metadata_listener_holds_a_guest_to_itself_too(tmp_path):
    """The gate was scoped by listener, and the boot API is granted twice.

    It is a platform port (checked) *and*, by the stock proxy defaults, the
    upstream at the metadata listener's root (unchecked) — so the very
    request the gate exists to refuse, `/v1/boots/<somebody-else>`, was
    answered to anyone who asked for it on port 80. The rule belongs to the
    path, not to the port a guest happened to knock on.
    """
    from gcl_sdk.agents.universal.drivers import evpn_proxy

    me = "11111111-1111-1111-1111-111111111111"
    other = "22222222-2222-2222-2222-222222222222"
    records = tmp_path / "10001"
    records.mkdir()
    (records / "port.json").write_text(
        json.dumps({"mac": "52:54:00:aa:bb:cc", "ipv4": "10.42.0.7", "node": me})
    )
    handler = _handler_for(records, port=evpn_proxy.METADATA_PORT)
    handler.headers = {}

    assert _gate(handler, "/v1/boots/%s" % me, "GET", None)
    assert not _gate(handler, "/v1/boots/%s" % other, "GET", None)
    # Naming somebody else *and* itself is still naming somebody else.
    assert not _gate(handler, "/v1/boots/%s/x/%s" % (other, me), "GET", None)
    # What the listener also serves belongs to no machine — the boot script,
    # the kernel, the initrd. Those are static, and which grant a request
    # lands in is the routing table's answer, not the path's shape.
    assert _gate(handler, "/boot/boot.ipxe", kind="boot")
    assert _gate(handler, "/artifacts/vmlinuz", kind="published")
    # Read-only, though: a subtree the installation published for guests is
    # not a subtree a guest writes to.
    assert not _gate(handler, "/artifacts/vmlinuz", "PUT", b"x", kind="published")


def test_naming_a_machine_is_an_equality_not_a_substring(tmp_path):
    """`me in path` passes for any path that merely contains the uuid.

    A request addressed to another machine that happens to carry the
    caller's uuid somewhere else in it — a query the caller controls, a
    nested path — read as the caller asking about itself.
    """

    me = "11111111-1111-1111-1111-111111111111"
    other = "22222222-2222-2222-2222-222222222222"
    records = tmp_path / "10001"
    records.mkdir()
    (records / "port.json").write_text(
        json.dumps({"mac": "52:54:00:aa:bb:cc", "ipv4": "10.42.0.7", "node": me})
    )
    handler = _handler_for(records)
    handler.headers = {}
    assert not _gate(handler, "/v1/machines/%s/peers/%s" % (other, me), "GET", None)


def test_a_grant_is_of_a_path_not_of_a_string():
    """`startswith` lets a grant of /boot carry /bootstrap into the upstream."""
    from gcl_sdk.agents.universal.drivers import evpn_proxy

    matches = evpn_proxy.ProxyHandler._prefix_matches
    assert matches("/artifacts/vmlinuz", "/artifacts")
    assert matches("/artifacts", "/artifacts")
    assert not matches("/artifactsX/secret", "/artifacts")
    assert matches("/anything", "/")


def test_an_oversized_answer_is_truncated_rather_than_dropped():
    """A reply bigger than the overlay's MTU never arrives, and the guest
    retries into the same silence. TC is the protocol's word for it, and it
    is what sends the client to the TCP listener."""
    query = _dns_query("big.example.com", 1)
    truncated = nf.truncate_dns(query, nf.parse_dns_question(query)[2])
    flags = struct.unpack("!H", truncated[2:4])[0]
    assert flags & 0x0200, "TC bit set"
    assert struct.unpack("!H", truncated[6:8])[0] == 0, "no answers to carry"
    assert truncated[:2] == query[:2], "same transaction"


def test_only_literal_public_forwarders_are_relayed_to():
    """Forwarders are the tenant's to name, and the relay runs on the
    host's side of the guard. A name went to the host's resolver (and
    `getaddrinfo`); loopback and link-local reached what the namespace
    itself serves."""
    connected = []

    class _Sock:
        def __init__(self, *args):
            pass

        def settimeout(self, value):
            pass

        def connect(self, address):
            connected.append(address)
            raise OSError("no upstream in a unit test")

        def close(self):
            pass

    forwarders = [
        "resolver.example",
        "127.0.0.1",
        "169.254.169.254",
        "224.0.0.1",
        "0.0.0.0",
        "2001:db8::1",
        "9.9.9.9",
    ]
    with mock.patch.object(nf.socket, "socket", _Sock):
        with mock.patch.object(nf.socket, "getaddrinfo") as resolve:
            assert nf.forward_upstream(b"\x00\x01q", forwarders) is None
    assert connected == [("9.9.9.9", 53)]
    resolve.assert_not_called()


def test_upstream_relay_leaves_the_serving_thread_alone():
    """One thread serves a VNI's DHCP *and* its DNS. Relaying inline meant a
    guest asking for names an upstream never answers stopped every other
    guest of the network from getting a lease — three seconds a query, for
    as long as it kept asking."""
    sent = []
    pool = nf.ForwarderPool(sent.append, limit=2)
    started = threading.Event()
    release = threading.Event()

    def _slow(payload, forwarders):
        started.set()
        release.wait(5)
        return b"\x00\x01reply"

    with mock.patch.object(nf, "forward_upstream", side_effect=_slow):
        assert pool.submit(b"\x00\x01q", ["10.0.0.1"], lambda r: b"frame")
        assert started.wait(5), "the relay runs off the caller's thread"
        # The caller returned while the relay is still blocked upstream.
        assert pool.submit(b"\x00\x01q", ["10.0.0.1"], lambda r: b"frame")
        # ... and the pool is a bound on what one VNI can hold of the host.
        assert not pool.submit(b"\x00\x01q", ["10.0.0.1"], lambda r: b"frame")
        release.set()
    for _ in range(50):
        if sent:
            break
        time.sleep(0.05)
    assert sent == [b"frame", b"frame"]


def test_dns_over_udp_defers_the_upstream_and_answers_locally_inline():
    """What the namespace knows is answered on the spot; what it must ask
    somebody else for is handed to the pool and returns nothing here."""
    zone = {"a": {"hostx.internal": "10.42.0.7"}, "ptr": {}}
    by_mac = {MAC: _rec()}
    frame = _udp_frame(
        MAC_B, "10.42.0.7", "10.42.0.254", 5000, 53, _dns_query("hostx.internal", 1)
    )
    _, src_mac, off = nf.parse_eth(frame)
    submitted = []

    class _Pool:
        def submit(self, payload, forwarders, reply_to):
            submitted.append((payload, tuple(forwarders)))
            return True

    reply = nf.handle_dns(frame, off, src_mac, by_mac, zone, ["8.8.8.8"], pool=_Pool())
    assert reply and not submitted, "the internal zone answers inline"

    frame = _udp_frame(
        MAC_B, "10.42.0.7", "10.42.0.254", 5000, 53, _dns_query("example.com", 1)
    )
    _, src_mac, off = nf.parse_eth(frame)
    reply = nf.handle_dns(frame, off, src_mac, by_mac, zone, ["8.8.8.8"], pool=_Pool())
    assert reply is None and submitted, "the upstream query is deferred"


def test_dns_over_tcp_answers_the_guest_that_asked(tmp_path):
    """br-int steals tcp/53 exactly as it steals udp/53, and nothing listened.

    Every query that a truncated UDP answer sent here was met with a
    connection refused, so a large answer failed both ways. The listener is
    the other half of truncation.
    """
    (tmp_path / "port.json").write_text(
        json.dumps(
            {
                "mac": MAC,
                "ipv4": "10.42.0.7",
                "name": "hostx",
                "cidr": "10.42.0.0/24",
                "routers": [{"to": "0.0.0.0/0", "via": "10.42.0.254"}],
            }
        )
    )
    cache = nf.RecordCache(str(tmp_path))
    cache.refresh()
    assert cache.by_ip["10.42.0.7"]["mac"] == MAC

    srv = nf.serve_dns_tcp(cache, port=0)
    try:
        port = srv.server_address[1]
        query = _dns_query("hostx.internal", 1)
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            # The handler identifies the guest by its source address, and the
            # loopback connection is not one — an unknown peer gets nothing,
            # which is itself the behaviour worth pinning.
            sock.sendall(struct.pack("!H", len(query)) + query)
            sock.settimeout(5)
            assert sock.recv(2) == b"", "an unknown source is not served"
        finally:
            sock.close()
    finally:
        srv.shutdown()
        srv.server_close()


def _gated_records(tmp_path, me):
    records = tmp_path / "10001"
    records.mkdir()
    (records / "port.json").write_text(
        json.dumps({"mac": MAC, "ipv4": "10.42.0.7", "node": me})
    )
    return records


ME = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


def test_a_path_that_names_nobody_is_not_a_path_that_is_allowed(tmp_path):
    """Paths that name no machine are refused, not waved through.

    The boot API answers unauthenticated at the listener's root, so its
    writable collections must not be reachable by any guest.
    """
    from gcl_sdk.agents.universal.drivers import evpn_proxy

    handler = _handler_for(_gated_records(tmp_path, ME), port=evpn_proxy.METADATA_PORT)
    handler.headers = {}

    # Registering an agent, and writing a node's reported state, with
    # nothing to say whose they are.
    assert not _gate(handler, "/v1/agents/", "POST", b"{}")
    assert not _gate(handler, "/v1/kind/evpn_port/resources/", "POST", b"{}")
    # Reading a collection of the platform's own API.
    assert not _gate(handler, "/v1/kind/")
    # And whatever is mounted there tomorrow: the default is refusal, which
    # is the whole difference between an allow-list and a shape.
    assert not _gate(handler, "/v1/something-new/")


def test_an_agent_reports_what_is_its_own(tmp_path):
    """A resource is named by a uuid of its own, not by its machine's — so
    holding the path to the caller's uuid would refuse an agent its own
    reports while proving nothing. What the objects do carry is `node`."""
    from gcl_sdk.agents.universal.drivers import evpn_proxy

    handler = _handler_for(_gated_records(tmp_path, ME), port=evpn_proxy.METADATA_PORT)
    handler.headers = {}
    res = "33333333-3333-3333-3333-333333333333"

    mine = json.dumps({"uuid": res, "kind": "evpn_port", "node": ME}).encode()
    theirs = json.dumps({"uuid": res, "kind": "evpn_port", "node": OTHER}).encode()
    assert _gate(handler, "/v1/kind/evpn_port/resources/", "POST", mine)
    assert not _gate(handler, "/v1/kind/evpn_port/resources/", "POST", theirs)
    # The same object under its own uuid: the path says nothing, the body
    # does, and the address the body is checked against cannot be chosen.
    assert _gate(handler, "/v1/kind/evpn_port/resources/%s" % res, "PUT", mine)
    assert not _gate(handler, "/v1/kind/evpn_port/resources/%s" % res, "PUT", theirs)


def test_the_platform_answers_only_the_calls_a_guest_makes(tmp_path):
    """Deny by default: a route nobody listed is refused, whatever the claim."""
    handler = _handler_for(_gated_records(tmp_path, ME))
    handler.headers = {}
    res = "33333333-3333-3333-3333-333333333333"
    mine = json.dumps({"uuid": res, "node": ME}).encode()

    for path, method, payload in (
        ("/v1/boots/%s" % ME, "GET", None),
        ("/v1/agents/", "POST", json.dumps({"uuid": ME, "node": ME}).encode()),
        ("/v1/agents/%s" % ME, "PUT", b"{}"),
        ("/v1/agents/%s/actions/get_payload?hash=x&version=1" % ME, "GET", None),
        ("/v1/node_verifiers/%s/actions/verify" % ME, "GET", None),
        ("/v1/nodes/%s/actions/refresh_secret/invoke" % ME, "POST", None),
        ("/v1/kind/password/resources/", "POST", mine),
        ("/v1/kind/password/resources/%s" % res, "GET", mine),
        ("/v1/kind/password/resources/%s" % res, "PUT", mine),
        ("/v1/kind/password/resources/%s" % res, "DELETE", mine),
    ):
        assert _gate(handler, path, method, payload), (method, path)

    for path, method, payload in (
        # A collection nobody listed, however the body claims the caller.
        ("/v1/iam/users/%s" % OTHER, "PUT", json.dumps({"node": ME}).encode()),
        ("/v1/nodes/%s" % ME, "GET", None),
        ("/v1/nodes/%s/actions/refresh_secret/invoke" % OTHER, "POST", None),
        ("/v1/agents/%s" % ME, "DELETE", None),
        # A collection root is a listing, slash or not.
        ("/v1/agents", "GET", None),
        ("/v1/agents?uuid=%s" % OTHER, "GET", None),
        ("/v1/kind/password/resources?node=%s" % ME, "GET", None),
    ):
        assert not _gate(handler, path, method, payload), (method, path)


def test_a_machine_named_after_the_question_mark_counts(tmp_path):
    """The gate read the path while `_forward` passed the query on
    untouched, so a machine named after the `?` was never looked at."""
    handler = _handler_for(_gated_records(tmp_path, ME))
    handler.headers = {}

    assert _gate(handler, "/v1/boots/%s" % ME)
    assert not _gate(handler, "/v1/boots/%s?about=%s" % (ME, OTHER))


def test_the_rom_needs_no_introduction(tmp_path):
    """A machine-scoped path is checked against the path alone on purpose:
    the client fetching `/v1/boots/<uuid>` is an iPXE ROM, which sends the
    GET and nothing else."""
    handler = _handler_for(_gated_records(tmp_path, ME))
    handler.headers = {}

    assert _gate(handler, "/v1/boots/%s" % ME)
    assert not _gate(handler, "/v1/boots/%s" % OTHER)
    # The collection itself belongs to no machine, so it needs the claim.
    assert not _gate(handler, "/v1/boots/")


def test_the_root_grant_is_the_platform_and_a_narrower_one_is_a_subtree(tmp_path):
    """Route first, gate second: `/` is the platform API, narrower grants
    are published subtrees.

    Unroutable requests get the API's rule (403, not 404), so they cannot
    probe which prefixes exist.
    """
    from gcl_sdk.agents.universal.drivers import evpn_proxy

    records = _gated_records(tmp_path, ME)
    (records / "nf-proxy.json").write_text(
        json.dumps(
            {
                "forwards": [
                    "/=http://10.30.0.2:11013",
                    "/artifacts=http://10.30.0.2:8080/artifacts",
                ]
            }
        )
    )
    handler = _handler_for(records, port=evpn_proxy.METADATA_PORT)
    handler.headers = {}
    seen = {}
    handler.send_error = lambda code, *a, **kw: seen.setdefault("error", code)
    handler._forward = lambda *args, **kwargs: seen.setdefault("forwarded", args[0])
    handler._serve_boot_script = lambda: seen.setdefault("boot", True)

    def dispatch(path, method="GET", payload=None):
        seen.clear()
        handler.path = path
        handler._dispatch(method, payload)
        return seen

    # A published subtree: any guest, no introduction, read only.
    assert dispatch("/artifacts/vmlinuz") == {"forwarded": "/artifacts"}
    assert dispatch("/artifacts/vmlinuz", "DELETE", None) == {"error": 403}
    # The boot script is the one answer this proxy gives out of itself.
    assert dispatch("/boot/boot.ipxe") == {"boot": True}
    # And it is the only thing this proxy answers itself: the prefix is not
    # a directory any more, so anything else under it is just another path
    # through the grants — here the root one, whose gate refuses it.
    assert dispatch("/boot/vmlinuz") == {"error": 403}
    # The platform's API behind the root grant: about the caller or nothing.
    assert dispatch("/v1/boots/%s" % ME) == {"forwarded": "/"}
    assert dispatch("/v1/boots/%s" % OTHER) == {"error": 403}
    assert dispatch("/v1/agents/", "POST", b"{}") == {"error": 403}
    # A root grant routes everything, so "nowhere" is still the platform's
    # API and is refused as such.
    assert dispatch("/nowhere/at/all") == {"error": 403}

    # Without one, nothing is granted there — and then there is nothing to
    # decide about the caller: an allow-list, never a relay.
    (records / "nf-proxy.json").write_text(
        json.dumps({"forwards": ["/artifacts=http://10.30.0.2:8080/artifacts"]})
    )
    assert dispatch("/v1/boots/%s" % ME) == {"error": 404}
    assert dispatch("/artifacts/vmlinuz") == {"forwarded": "/artifacts"}


def test_a_vni_gives_its_listeners_back(tmp_path):
    """A worker is stopped when its VNI leaves the host, and its listeners
    are daemon threads: left running they hold both their ports and — a live
    socket keeps a namespace alive after `ip netns del` unlinks its name —
    the namespace itself. The VNI that came back then found everything it
    wanted held by the corpse of the last one."""
    from gcl_sdk.agents.universal.drivers import evpn_proxy

    listeners = evpn_proxy.Listeners()
    closed = []

    class _Srv:
        def shutdown(self):
            closed.append("shutdown")

        def server_close(self):
            closed.append("close")

    assert listeners.add(80, _Srv()) is True
    listeners.shutdown()
    assert closed == ["shutdown", "close"]

    # A listener still waiting for its address when the worker stopped finds
    # the door shut, and closes itself instead of registering behind it.
    closed.clear()
    late = _Srv()
    assert listeners.add(80, late) is False
    assert closed == ["close"]


def test_tcp_dns_is_bounded_like_the_proxy(tmp_path):
    """br-int steals tcp/53 from every guest of the VNI, so nothing else can
    answer it — and an unbounded threading server let one guest spend the
    *process* out of threads, which is DHCP, DNS and metadata for every
    tenant on the hypervisor."""
    from gcl_sdk.agents.universal.drivers import evpn_proxy

    assert issubclass(nf._DnsTcpServer, evpn_proxy.BoundedThreadingMixIn)
    assert nf._DnsTcpServer.max_workers > 0


def test_a_percent_encoded_path_is_refused_before_the_gate(tmp_path):
    """The gate reads the path as sent, the upstream decodes it; an encoded
    path would let a guest name another machine past the gate."""
    from gcl_sdk.agents.universal.drivers import evpn_proxy

    records = _gated_records(tmp_path, ME)
    (records / "nf-proxy.json").write_text(
        json.dumps({"forwards": ["/=http://10.30.0.2:11013"]})
    )
    handler = _handler_for(records, port=evpn_proxy.METADATA_PORT)
    handler.headers = {evpn_proxy.NODE_UUID_HEADER: ME}
    seen = {}
    handler.send_error = lambda code, *a, **kw: seen.setdefault("error", code)
    handler._forward = lambda *args, **kw: seen.setdefault("forwarded", args[0])
    mine = json.dumps({"uuid": ME}).encode()
    encoded_other = OTHER.replace("-", "%2D", 1)

    for path, method, payload in (
        ("/v1/%%61gents/%s" % OTHER, "PUT", mine),
        ("/v1/agents/%s?node=%s" % (encoded_other, ME), "PUT", mine),
        ("/v1/agents%2F", "GET", None),
    ):
        seen.clear()
        handler.path = path
        handler._dispatch(method, payload)
        assert seen == {"error": 400}, path


def test_an_api_authenticates_its_own_callers(tmp_path):
    """The third kind of grant, and the reason it exists.

    A child realm calls its parent's ecosystem with the *realm's* tokens,
    not as the node it happens to run on — so the gate that holds an agent
    to its own uuid would refuse it, and a published subtree (read-only)
    cannot carry the POST it needs. What is behind an `api` checks
    credentials itself, so this proxy carries the conversation whole.
    """
    from gcl_sdk.agents.universal.drivers import evpn_proxy

    records = _gated_records(tmp_path, ME)
    (records / "nf-proxy.json").write_text(
        json.dumps(
            {
                "forwards": ["/artifacts=http://10.30.0.2:8080/artifacts"],
                "apis": ["/ecosystem=http://10.20.0.2"],
            }
        )
    )
    handler = _handler_for(records, port=evpn_proxy.METADATA_PORT)
    handler.headers = {}
    seen = {}
    handler.send_error = lambda code, *a, **kw: seen.setdefault("error", code)
    handler._forward = lambda *args, **kw: seen.setdefault("forwarded", args[0])

    def dispatch(path, method="GET", payload=None):
        seen.clear()
        handler.path = path
        handler._dispatch(method, payload)
        return seen

    # ... a POST a realm makes on its own credentials, which no other grant
    # could carry: a subtree is read-only and a platform port wants a uuid.
    assert dispatch("/ecosystem/v1/realms/", "POST", b'{"name": "child"}') == {
        "forwarded": "/ecosystem"
    }
    assert dispatch("/ecosystem/v1/realms/") == {"forwarded": "/ecosystem"}
    # the other grants keep their own rules
    assert dispatch("/artifacts/vmlinuz") == {"forwarded": "/artifacts"}
    assert dispatch("/artifacts/vmlinuz", "POST", b"x") == {"error": 403}


# --- doors onto services of the installation -------------------------------


def _with_doors(records, services):
    (records / "nf-proxy.json").write_text(json.dumps({"services": services}))


def test_a_door_is_routed_by_the_port_it_answers_on(tmp_path):
    """A door is a whole service, not a prefix of one.

    Which grant a request lands in is decided before any path is looked at,
    because the paths under a door belong to what answers there — narrowing
    them here would be this proxy inventing an API surface for somebody
    else's service.
    """

    records = _gated_records(tmp_path, ME)
    _with_doors(records, {"11020": "http://10.20.0.2:11020"})

    handler = _handler_for(records, port=11020)
    assert handler._route("/v1/anything/at/all") == (
        "/",
        "http://10.20.0.2:11020",
        "service",
    )
    # And only on its own port: the same request on a listener no door names
    # is not a door request.
    other = _handler_for(records, port=11013)
    assert other._route("/v1/anything/at/all")[2] != "service"


def test_a_door_refuses_a_caller_the_control_plane_does_not_know(tmp_path):
    """Being told who is calling is the whole difference between a door and
    an `api`, so a caller this VNI has no record of is refused rather than
    relayed with the header missing — an upstream cannot tell "nobody said"
    from "nobody asked"."""
    records = _gated_records(tmp_path, ME)
    _with_doors(records, {"11020": "http://10.20.0.2:11020"})

    known = _handler_for(records, port=11020)
    known.headers = {}
    assert _gate(known, "/v1/anything", "POST", b"{}", kind="service")

    stranger = _handler_for(records, port=11020)
    stranger.headers = {}
    stranger.client_address = ("10.42.0.99", 5000)
    assert not _gate(stranger, "/v1/anything", "POST", b"{}", kind="service")


def test_the_ports_a_vni_answers_on_follow_the_doors(tmp_path):
    """The platform's own ports are a constant — they are the same on every
    host — and the doors are data, because which services an installation
    runs is not something the data plane can know."""
    from gcl_sdk.agents.universal.drivers import evpn_proxy

    records = tmp_path / "10001"
    records.mkdir()
    assert evpn_proxy.wanted_ports(records) == {
        evpn_proxy.METADATA_PORT,
        *evpn_proxy.PLATFORM_PORTS,
    }

    _with_doors(records, {"11020": "http://10.20.0.2:11020", "bad": "x"})
    assert evpn_proxy.wanted_ports(records) == {
        evpn_proxy.METADATA_PORT,
        *evpn_proxy.PLATFORM_PORTS,
        11020,
    }


def test_a_vni_opens_and_closes_listeners_as_the_doors_change(tmp_path):
    """A door may be opened long after the guests were placed — a PaaS
    installed on a running stand — and closing the VNI's responder to pick
    it up would take DHCP and DNS away from everything already on it."""
    from gcl_sdk.agents.universal.drivers import evpn_proxy

    listeners = evpn_proxy.Listeners()
    started = []
    with mock.patch.object(
        evpn_proxy, "_serve_port", lambda d, p, ls: started.append(p)
    ):
        listeners.reconcile(str(tmp_path), {80, 11020})
        assert sorted(started) == [80, 11020]
        assert listeners.ports() == {80, 11020}

        # A port already held is not started twice...
        started.clear()
        listeners.reconcile(str(tmp_path), {80, 11020, 11021})
        assert started == [11021]

        # ...and one that stops being a door is given back.
        listeners.reconcile(str(tmp_path), {80})
        assert listeners.ports() == {80}


def test_the_proxy_fetches_nothing_but_http(tmp_path):
    """`urlopen`'s default opener carries a `FileHandler` and an `FTPHandler`,
    so `file:///etc/shadow` as an upstream is not a malformed grant — it is a
    working one that reads the hypervisor."""
    import urllib.request

    from gcl_sdk.agents.universal.drivers import evpn_proxy

    assert evpn_proxy._is_relayable("http://10.20.0.2:11020/x")
    assert evpn_proxy._is_relayable("https://example.test/x")
    assert not evpn_proxy._is_relayable("file:///etc/shadow")
    assert not evpn_proxy._is_relayable("ftp://10.20.0.2/x")
    assert not evpn_proxy._is_relayable("/etc/shadow")

    built = evpn_proxy._OPENER.handlers
    for unwanted in (
        urllib.request.FileHandler,
        urllib.request.FTPHandler,
        urllib.request.ProxyHandler,
    ):
        assert not any(isinstance(h, unwanted) for h in built), unwanted


def test_the_proxy_does_not_follow_a_redirect(tmp_path):
    """A grant is of one upstream, and a 302 is how one upstream silently
    becomes another. This process stands in the management network with the
    caller's credentials in hand, so following one is every check on the
    naming side walked around in a single hop. The 3xx is relayed to the
    guest instead, which is the party entitled to decide."""
    import urllib.request

    from gcl_sdk.agents.universal.drivers import evpn_proxy

    assert not any(
        isinstance(h, urllib.request.HTTPRedirectHandler)
        for h in evpn_proxy._OPENER.handlers
    )


def test_the_identity_header_is_the_proxys_word_and_not_the_guests(tmp_path):
    """The caller's own copy goes no further, whatever it said: what is
    behind a door has no way to tell a forged claim from a true one, so this
    hop must not pass one on."""
    import urllib.error

    from gcl_sdk.agents.universal.drivers import evpn_proxy

    records = _gated_records(tmp_path, ME)
    _with_doors(records, {"11020": "http://10.20.0.2:11020"})

    handler = _handler_for(records, port=11020)
    handler.path = "/v1/thing"
    handler.headers = {
        "Authorization": "Bearer t",
        evpn_proxy.NODE_UUID_HEADER: OTHER,  # the forgery
        # ... and so is every "the client is ..." a proxy would add
        "X-Forwarded-For": "10.20.0.1",
        "X-Forwarded-Host": "core.internal",
        "X-Forwarded-Proto": "https",
        "X-Real-IP": "10.20.0.1",
        "Forwarded": "for=10.20.0.1",
    }
    refused = []
    handler.send_error = lambda code, *a, **k: refused.append(code)

    seen = {}

    class _Opener:
        def open(self, req, timeout=None):
            seen["req"] = req
            raise urllib.error.URLError("no upstream in a unit test")

    with mock.patch.object(evpn_proxy, "_OPENER", _Opener()):
        handler._forward("/", "http://10.20.0.2:11020", "/v1/thing", "POST", b"{}")

    assert refused == [502]
    carried = {k.lower(): v for k, v in seen["req"].headers.items()}
    assert carried[evpn_proxy.NODE_UUID_HEADER.lower()] == ME, (
        "the record's answer, not the claim"
    )
    assert OTHER not in carried.values()
    assert not [h for h in carried if "forwarded" in h or h == "x-real-ip"]
    # Everything else the caller signs with still travels.
    assert carried["authorization"] == "Bearer t"


def test_a_grant_is_not_climbed_out_of_by_spelling_the_dots_differently(tmp_path):
    """The upstream resolves `..` after it decodes, so the check has to read
    the path the same way round. Spelled `%2e%2e`, the escape reached an
    nginx serving a published subtree and read whatever was above it."""
    import urllib.error

    from gcl_sdk.agents.universal.drivers import evpn_proxy

    records = _gated_records(tmp_path, ME)
    handler = _handler_for(records, port=evpn_proxy.METADATA_PORT)
    handler.headers = {}

    class _Opener:
        def open(self, req, timeout=None):
            raise urllib.error.URLError("no upstream in a unit test")

    def refused_for(rest):
        codes = []
        handler.path = "/artifacts/" + rest
        handler.send_error = lambda code, *a, **k: codes.append(code)
        with mock.patch.object(evpn_proxy, "_OPENER", _Opener()):
            handler._forward(
                "/artifacts",
                "http://10.30.0.2:8080/artifacts",
                handler.path,
                "GET",
                None,
            )
        return codes

    assert refused_for("..%2fsecret") == [403]
    assert refused_for("%2e%2e/secret") == [403]
    assert refused_for("%2e%2e%2fsecret") == [403]
    assert refused_for("../secret") == [403]
    # Double-encoded is not an escape either way round: the upstream decodes
    # once, so what reaches it is the literal `%2e%2e` and no path at all.
    assert refused_for("%252e%252e/secret") == [502]
    # And what the grant is for is still fetched — 502 is this stand-in
    # upstream refusing to answer, which only happens past the gate.
    assert refused_for("vmlinuz") == [502]


def test_a_body_bigger_than_the_ceiling_is_refused_and_never_read(tmp_path):
    """One responder serves every guest on the host, so a body it agrees to
    hold in memory is a guest deciding how much of the hypervisor to take.
    `max_workers` bounds the threads, not the bytes each of them reads."""
    import io

    from gcl_sdk.agents.universal.drivers import evpn_proxy

    handler = _handler_for(_gated_records(tmp_path, ME))
    handler.path = "/v1/agents/%s" % ME
    dispatched = []
    refused = []
    handler._dispatch = lambda *a: dispatched.append(a)
    handler.send_error = lambda code, *a, **k: refused.append(code)

    over = evpn_proxy.ProxyHandler._BODY_LIMIT + 1
    handler.headers = {"Content-Length": str(over)}
    handler.rfile = io.BytesIO(b"")
    handler.do_POST()
    assert refused == [413]
    assert dispatched == [], "the dispatch ran on a body that was never read"

    # A Content-Length that is not a number is the caller's mistake, not a
    # traceback in the one process the whole host shares.
    refused.clear()
    handler.headers = {"Content-Length": "not-a-number"}
    handler.do_POST()
    assert refused == [400]
    assert dispatched == []

    # What the platform's own agents send still goes through.
    refused.clear()
    handler.headers = {"Content-Length": "2"}
    handler.rfile = io.BytesIO(b"{}")
    handler.do_POST()
    assert refused == []
    assert dispatched == [("POST", b"{}")]


def test_a_grant_naming_something_unfetchable_is_refused(tmp_path):
    """A grant this proxy will not fetch is a misconfiguration of the
    installation, not a guest's mistake: the guest is told no, and the log
    says which upstream it was."""

    records = _gated_records(tmp_path, ME)
    handler = _handler_for(records, port=11020)
    handler.path = "/x"
    handler.headers = {}
    refused = []
    handler.send_error = lambda code, *a, **k: refused.append(code)

    handler._forward("/", "file:///etc", "/x", "GET", None)
    assert refused == [502]


def test_the_intercepts_claim_the_doors_ports(tmp_path):
    """A claimed port is a flow per guest, so the doors ride on the *port's*
    descriptor: a door added later has to re-hash the ports it serves, or it
    is listened on and never delivered to."""
    from gcl_sdk.agents.universal.drivers.evpn import flows

    assert flows._proxy_ports({"ports": [11020, "11021"]}) == {11020, 11021}
    # Defensive, not argumentative: the control plane validates these, and a
    # bad entry must not cost the guest the intercepts the good ones need.
    assert flows._proxy_ports({"ports": [0, 70000, None, "x", 11020]}) == {11020}
    assert flows._proxy_ports({}) == set()
    assert flows._proxy_ports(None) == set()


def test_a_doors_port_is_stolen_from_the_guest_like_the_platforms_own(tmp_path):
    """The listener is only half of it: a port nothing steals goes out over
    the fabric to an address in the space the egress guard drops, so the door
    would be open and unreachable."""
    from gcl_sdk.agents.universal.drivers.evpn import base
    from gcl_sdk.agents.universal.drivers.evpn import flows

    with mock.patch.object(flows.sh, "run_flow_bundle") as bundle:
        flows.ensure_port_nf_intercepts(
            {"proxy": {"ports": [11020]}},
            guest_ofport="7",
            nf_ofport=42,
            guest_mac=MAC,
            cookie="0x1",
            sources=["10.42.0.7"],
        )
    lines = bundle.call_args[0][0]
    claimed = {
        int(line.split("tp_dst=")[1].split(",")[0])
        for line in lines
        if "tp_dst=" in line and "tcp" in line
    }
    assert 11020 in claimed, "the door"
    assert set(base.PLATFORM_PORTS) <= claimed, "and the platform's own beside it"


def test_nf_intercepts_only_claim_the_responder_addresses():
    from gcl_sdk.agents.universal.drivers.evpn import flows

    destinations = ["169.254.169.254", "10.120.0.1"]
    with mock.patch.object(flows.sh, "run_flow_bundle") as bundle:
        flows.ensure_port_nf_intercepts(
            {"dhcp": {}, "dns": {"destinations": destinations},
             "proxy": {"ports": [33118], "destinations": destinations}},
            7, 42, MAC, "0x1", sources=["10.120.0.2"], zone=5,
        )
    intercepts = [line for line in bundle.call_args.args[0] if "tp_dst=" in line]
    for line in intercepts:
        if "tp_dst=67," in line:
            assert "nw_dst=" not in line
        else:
            assert any("nw_dst=" + address + "," in line for address in destinations)
            assert "ct(commit,zone=5)" in line
    assert not any("nw_dst=10.120.0.3," in line for line in intercepts)
    for port in [53, 11011, 11012, 11013, 33118]:
        assert any("tp_dst=" + str(port) + "," in line for line in intercepts)


@pytest.mark.parametrize(
    "destinations",
    [[], None, 123, "10.120.0.1", ["invalid", "0.0.0.0/0", "::1", 0, True]],
)
def test_explicit_invalid_nf_destinations_do_not_claim_arbitrary_peers(destinations):
    from gcl_sdk.agents.universal.drivers.evpn import flows

    with mock.patch.object(flows.sh, "run_flow_bundle") as bundle:
        flows.ensure_port_nf_intercepts(
            {"dns": {"destinations": destinations},
             "proxy": {"destinations": destinations}},
            7, 42, MAC, "0x1", sources=["10.120.0.2"],
        )
    intercepts = [line for line in bundle.call_args.args[0] if "tp_dst=" in line]
    assert len(intercepts) == 1
    assert "nw_dst=169.254.169.254," in intercepts[0]
    assert "tp_dst=80," in intercepts[0]


def test_a_silent_client_gives_its_proxy_worker_back(tmp_path):
    """The listener has a fixed pool of workers, and a connection that sent
    nothing held one for good: a handful of them and the VNI's metadata,
    boot and platform ports answered no one."""
    from gcl_sdk.agents.universal.drivers import evpn_proxy

    assert 0 < (evpn_proxy.ProxyHandler.timeout or 0) <= 30

    with mock.patch.object(evpn_proxy.ProxyHandler, "timeout", 0.2):
        server = evpn_proxy._VniHTTPServer(
            ("127.0.0.1", 0), evpn_proxy._handler(str(tmp_path), 80)
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = socket.create_connection(server.server_address)
            client.settimeout(5)
            try:
                # The server hangs up on its own; nothing was ever sent.
                assert client.recv(1) == b""
            finally:
                client.close()
        finally:
            server.shutdown()
            server.server_close()


def _same_tick(directory, write):
    """Change the directory without its mtime moving, as two renames inside
    one tick of the kernel's coarse clock do."""
    st = os.stat(directory)
    write()
    os.utime(directory, ns=(st.st_atime_ns, st.st_mtime_ns))


def test_a_record_renamed_in_within_the_same_tick_is_seen(tmp_path):
    """A refresh that loaded between two renames of one tick held a stamp
    the second rename did not move: its guest got no DHCP answer until the
    next write to the directory."""

    def _write(name, mac, ipv4):
        tmp = tmp_path / (name + ".tmp")
        tmp.write_text(json.dumps({"mac": mac, "ipv4": ipv4}))
        os.replace(tmp, tmp_path / name)

    _write("a.json", MAC, "10.42.0.7")
    cache = nf.RecordCache(str(tmp_path))
    cache.refresh()
    assert set(cache.by_ip) == {"10.42.0.7"}

    _same_tick(tmp_path, lambda: _write("b.json", "52:54:00:aa:bb:cd", "10.42.0.8"))
    cache.refresh()
    assert set(cache.by_ip) == {"10.42.0.7", "10.42.0.8"}

    # Once the stamp is old enough to trust, an unchanged directory costs no
    # reload at all.
    past = time.time_ns() - 10 * 10**9
    os.utime(tmp_path, ns=(past, past))
    cache.refresh()
    with mock.patch.object(nf, "_load_records") as load:
        cache.refresh()
    load.assert_not_called()


def test_the_proxy_sees_a_caller_renamed_in_within_the_same_tick(tmp_path):
    from gcl_sdk.agents.universal.drivers import evpn_proxy

    def _write(node):
        tmp = tmp_path / "port.json.tmp"
        tmp.write_text(json.dumps({"mac": MAC, "ipv4": "10.42.0.7", "node": node}))
        os.replace(tmp, tmp_path / "port.json")

    _write("one")
    assert evpn_proxy.caller_uuid(str(tmp_path), "10.42.0.7") == "one"
    _same_tick(tmp_path, lambda: _write("two"))
    assert evpn_proxy.caller_uuid(str(tmp_path), "10.42.0.7") == "two"


def test_boot_proxy_attests_caller_and_replaces_guest_delegation(tmp_path, monkeypatch):
    import base64
    import urllib.error

    from gcl_sdk.agents.universal.api import boot_identity
    from gcl_sdk.agents.universal.drivers import evpn_proxy

    records = _gated_records(tmp_path, ME)
    handler = _handler_for(records, port=11013)
    handler.path = "/v1/kind/config/resources/?hash=a"
    handler.headers = {
        header: "forged"
        for header in (
            boot_identity.HOST_HEADER,
            boot_identity.TIME_HEADER,
            boot_identity.NONCE_HEADER,
            boot_identity.SIGNATURE_HEADER,
        )
    }
    key_file = tmp_path / "private_key"
    key_file.write_text(base64.b64encode(b"k" * 32).decode())
    monkeypatch.setenv("EXORDOS_BOOT_PROXY_KEY_FILE", str(key_file))
    monkeypatch.setenv("EXORDOS_BOOT_PROXY_HOST", OTHER)
    seen = {}
    handler.send_error = lambda *a, **kw: None

    class Opener:
        def open(self, req, timeout):
            seen["request"] = req
            raise urllib.error.URLError("unit test")

    with mock.patch.object(evpn_proxy, "_OPENER", Opener()):
        handler._forward(
            "/", "http://core:11013", "/v1/kind/config/resources/", "POST", b"{}"
        )
    request = seen["request"]
    carried = {k.lower(): v for k, v in request.headers.items()}
    assert "forged" not in carried.values()
    assert carried[boot_identity.HOST_HEADER.lower()] == OTHER
    assert carried[evpn_proxy.NODE_UUID_HEADER.lower()] == ME
    assert carried[boot_identity.SIGNATURE_HEADER.lower()] == boot_identity.signature(
        b"k" * 32,
        OTHER,
        ME,
        carried[boot_identity.TIME_HEADER.lower()],
        carried[boot_identity.NONCE_HEADER.lower()],
        "POST",
        request.selector,
        b"{}",
    )
