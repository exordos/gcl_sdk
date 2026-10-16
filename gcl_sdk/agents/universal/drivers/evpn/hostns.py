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

"""The per-VNI namespace on the host, and everything inside it.

One namespace per VNI is the tenant boundary on a hypervisor: the NF
port, guest neighbours, DVR egress and its fabric guard, floating-address
NAT, and their collection. Also the host-local VLAN map.
"""

import collections
import ipaddress
import json
import logging
import os
import re
import shlex
import socket
import struct
import subprocess
import zlib

from gcl_sdk.agents.universal.drivers import evpn_records
from gcl_sdk.agents.universal.drivers import exceptions as driver_exc
from gcl_sdk.agents.universal.drivers.evpn import base
from gcl_sdk.agents.universal.drivers.evpn import flows
from gcl_sdk.agents.universal.drivers.evpn import sh

LOG = logging.getLogger(__name__)

# VNIs whose egress guard was asserted this iteration: the guard is per
# VNI, so re-checking it per guest only costs subprocesses.
_GUARDED_VNIS: set = set()
_EGRESS_DEMANDS: dict | None = None
_NF_PORTS: dict | None = None
_CLIENTS: dict | None = None


def iptables(*args: str, ns: str | None = None, table: str = "nat") -> str:
    """One `iptables` call, here or inside a namespace.

    `-w`: the xtables lock is host-wide and libvirt may briefly hold it.
    """
    return sh.run(iptables_cmd(*args, ns=ns, table=table))


def iptables_cmd(*args: str, ns: str | None = None, table: str = "nat") -> list:
    argv = [base.IPTABLES_BIN, "-w", "5", "-t", table, *args]
    return [base.IP_BIN, "netns", "exec", ns, *argv] if ns else argv


def ensure_rule(chain: str, *rule: str, ns=None, table="nat", head=False) -> None:
    """Install a rule unless it is already there (`head`: insert at top)."""
    try:
        iptables("-C", chain, *rule, ns=ns, table=table)
    except subprocess.CalledProcessError:
        iptables("-I" if head else "-A", chain, *rule, ns=ns, table=table)


# The last VLAN handed out. Allocation goes on from it, so a released
# number (its `pe<vlan>` name, the ofport OVS keeps for that name, its ct
# zone) comes back only after every other one: the connector's fail-static
# flows still output to a released guest's patch while the RIB is untrusted.
_VLAN_CURSOR_KEY = "#last"


def _vlan_map() -> dict:
    """Port uuid -> local VLAN, as assigned so far; empty when unreadable."""
    try:
        with open(base.VLAN_MAP_PATH) as fl:
            return json.load(fl)
    except (OSError, ValueError):
        return {}


def local_vlan_for_port(port_uuid: object) -> int:
    """Return a stable per-host local VLAN for the port, assigning one.

    Per guest port, not per VNI: evpn_connector rejects two clients sharing
    an ofport, so each guest needs its own patch and VLAN.
    """
    key = str(port_uuid)
    vlan_map = _vlan_map()
    if key in vlan_map:
        return int(vlan_map[key])
    # OVS is the truth: a lost/unreadable map must not hand out a VLAN (and
    # ct zone) already wired on br-int.
    in_use = _vlans_in_use()
    if in_use is None:
        LOG.warning("evpn: br-int VLANs could not be read, deferring %s", key)
        raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": key})
    last = int(vlan_map.pop(_VLAN_CURSOR_KEY, 0) or 0)
    used = {int(v) for v in vlan_map.values()} | in_use
    for step in range(1, 4095):
        vlan = (last + step - 1) % 4094 + 1
        if vlan not in used:
            vlan_map[key] = vlan
            vlan_map[_VLAN_CURSOR_KEY] = vlan
            sh.write_atomic(base.VLAN_MAP_PATH, json.dumps(vlan_map, sort_keys=True))
            return vlan
    # Retriable driver error, so it is reported against this resource rather
    # than failing the whole iteration.
    LOG.error(
        "evpn: br-int has no free local VLAN left (4094 guests and NF ports "
        "on this host); %s cannot be wired",
        key,
    )
    raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": key})


def _vlans_in_use() -> set | None:
    """Every access VLAN a port of this host's OVS carries; None if unread."""
    out = sh.try_run([base.OVSVSCTL_BIN, "--bare", "--columns=tag", "list", "Port"])
    if out is None:
        return None
    return {int(tag) for tag in out.split() if tag.isdigit()}


def lookup_local_vlan(port_uuid: object) -> int | None:
    """Return the port's assigned local VLAN without assigning one, or None."""
    value = _vlan_map().get(str(port_uuid))
    return int(value) if value is not None else None


def release_local_vlan(port_uuid: object) -> None:
    """Free the port's local VLAN so the number can be reused."""
    vlan_map = _vlan_map()
    if vlan_map.pop(str(port_uuid), None) is not None:
        sh.write_atomic(base.VLAN_MAP_PATH, json.dumps(vlan_map, sort_keys=True))


vni_ns = evpn_records.vni_ns

nf_port = evpn_records.nf_port


def ensure_vni_ns(vni: int) -> str:
    """Ensure this VNI's namespace exists on the host; return its name."""
    ns = vni_ns(vni)
    if ns not in netns_names():
        sh.ip("netns", "add", ns)
        sh.ns_ip(ns, "link", "set", "lo", "up")
    return ns


def netns_names() -> set:
    """The set of network-namespace names.

    Exact names, not a substring test: ``vni100`` must not match ``vni1000``.
    """
    out = sh.ip("netns", "list")
    return {line.split()[0] for line in out.splitlines() if line.strip()}


def link_in_ns(ns: str, name: str) -> dict | None:
    """This interface as the namespace holds it, or None when it is not there.

    Lets callers skip re-setting the address, which flushes neighbours.
    """
    try:
        out = sh.ns_ip(ns, "-json", "link", "show", name)
        links = json.loads(out)
        first = links[0] if links else None
    except (
        subprocess.CalledProcessError,
        FileNotFoundError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
    ):
        return None
    return first if isinstance(first, dict) else None


def nf_vlan_key(vni: int) -> str:
    """VLAN-map key for this VNI's NF port.

    Own VLAN per VNI: a shared one lets namespaces flood each other via NORMAL.
    """
    return "nf:%d" % vni


# Comment tag on the NF port's forwarding drop.
NF_GUARD_TAG = "evpn-nf-guard"


def nf_guard_rule(vni: int) -> list[str]:
    """The drop of anything the NF port would have the namespace route on.

    Intercepts match the port alone, whatever the address, so a frame
    punted here for someone else would otherwise be forwarded past the
    fabric guard (which sees only the gateway leg) and every group. In
    ``mangle``: ahead of any filter accept, whatever order those land in.
    """
    return ["-i", nf_port(vni), "-m", "comment", "--comment", NF_GUARD_TAG]


def ensure_nf_guard(vni: int) -> None:
    """Let the NF port only reach the namespace itself, never through it."""
    ensure_rule(
        "FORWARD", *nf_guard_rule(vni), "-j", "DROP", ns=vni_ns(vni), table="mangle"
    )
    # Listeners share the gateway namespace. A spoofed packet that misses
    # the intercept must not reach them through the fabric's gateway leg.
    ensure_rule(
        "INPUT",
        "-i",
        "gw%d" % vni,
        "-p",
        "tcp",
        "-m",
        "comment",
        "--comment",
        NF_GUARD_TAG,
        "-j",
        "DROP",
        ns=vni_ns(vni),
        table="mangle",
    )


def nf_guarded(ns: str) -> bool | None:
    """Whether this namespace holds its NF drop; None if it cannot be read."""
    saved = _iptables_save(ns, "mangle")
    if saved is None:
        return None
    forwarding = any(
        line.startswith("-A FORWARD ") and NF_GUARD_TAG in line and "DROP" in line
        for line in saved.splitlines()
    )
    gateway = "-i gw%d " % evpn_records.ns_vni(ns)
    listeners = any(
        line.startswith("-A INPUT ")
        and gateway in line
        and "-p tcp " in line
        and NF_GUARD_TAG in line
        and "-j DROP" in line
        for line in saved.splitlines()
    )
    return forwarding and listeners


def ensure_vni_nf_port(vni: int) -> int:
    """This VNI's port inside its namespace; return its ofport on br-int.

    An OVS internal port moved into the namespace, holding the metadata
    address behind a fixed MAC. Idempotent per command: ``ip link set …
    address`` bounces the link even when unchanged, flushing the neighbours
    other guests programmed.
    """
    if _NF_PORTS is not None and vni in _NF_PORTS:
        return _NF_PORTS[vni]
    ns = ensure_vni_ns(vni)
    # Before the port exists: nothing may be routed in from it, ever.
    ensure_nf_guard(vni)
    port = nf_port(vni)
    sh.ovs("--may-exist", "add-br", base.BR_INT)
    # Tagged: an untagged OVS port is a trunk and would receive every
    # tenant's broadcast.
    sh.ovs(
        "--may-exist",
        "add-port",
        base.BR_INT,
        port,
        "--",
        "set",
        "port",
        port,
        "tag=%d" % local_vlan_for_port(nf_vlan_key(vni)),
        "--",
        "set",
        "interface",
        port,
        "type=internal",
    )
    link = link_in_ns(ns, port)
    if link is None:
        sh.ip("link", "set", port, "netns", ns)
        link = {}
    if (link.get("address") or "").lower() != base.METADATA_MAC.lower():
        sh.ns_ip(ns, "link", "set", port, "address", base.METADATA_MAC)
    if "UP" not in (link.get("flags") or []):
        sh.ns_ip(ns, "link", "set", port, "up")
    sh.run(
        [base.IP_BIN, "netns", "exec", ns, base.IP_BIN, "addr", "replace"]
        + ["%s/32" % base.METADATA_IP, "dev", port]
    )
    ofport = sh.ofport(port)
    if ofport is None:
        # Not assigned yet; retriable on the next iteration.
        raise driver_exc.InvalidDataPlaneObjectError(obj={"port": port})
    if _NF_PORTS is not None:
        ensure_metadata_route_rule(ns)
        _NF_PORTS[vni] = ofport
    return ofport


# Host routes to guests for the responder only. In the main table they would
# also capture replies to guest-opened connections, which then skip ingress
# conntrack and are dropped as invalid.
GUEST_ROUTE_TABLE = "100"


GUEST_ROUTE_RULE_PRIORITY = "100"


def ensure_metadata_route_rule(ns: str) -> None:
    """Send only the responder's own traffic through `GUEST_ROUTE_TABLE`."""
    rules = sh.ns_ip(ns, "rule", "show") or ""
    if "lookup %s" % GUEST_ROUTE_TABLE in rules:
        return
    sh.ns_ip(
        ns,
        "rule",
        "add",
        "from",
        base.METADATA_IP,
        "lookup",
        GUEST_ROUTE_TABLE,
        "priority",
        GUEST_ROUTE_RULE_PRIORITY,
    )


def ensure_guest_neighbour(vni: int, ipv4: str, mac: str) -> None:
    """Teach this VNI's namespace where the guest is.

    Programmed from the CP rather than ARPed: the namespace cannot ARP past
    the OpenFlow pipeline, and a guest cannot poison a static entry.
    """
    ns = vni_ns(vni)
    port = nf_port(vni)
    if _NF_PORTS is None or vni not in _NF_PORTS:
        ensure_metadata_route_rule(ns)
    sh.ns_ip(
        ns, "route", "replace", "%s/32" % ipv4, "dev", port, "table", GUEST_ROUTE_TABLE
    )
    sh.ns_ip(ns, "neigh", "replace", ipv4, "lladdr", mac, "dev", port)


def _inet_addresses() -> list | None:
    """(ifname, local, prefixlen) of every IPv4 address this host holds, or
    None when it cannot be asked.
    """
    try:
        links = json.loads(sh.ip("-json", "addr", "show"))
        if not isinstance(links, list):
            raise ValueError("expected a list of links")
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        return None
    found = []
    for link in links:
        if not isinstance(link, dict):
            continue
        for info in link.get("addr_info") or []:
            if isinstance(info, dict) and info.get("family") == "inet":
                if info.get("local"):
                    found.append(
                        (link.get("ifname"), info["local"], info.get("prefixlen"))
                    )
    return found


def host_addresses() -> set | None:
    """Every IPv4 address this host holds, or None when it cannot be asked."""
    found = _inet_addresses()
    if found is None:
        return None
    return {str(local) for _ifname, local, _plen in found}


# Comment tag on egress-guard rules, so the set can be reconciled.
FABRIC_GUARD_TAG = "evpn-fabric-guard"


# Root FORWARD accepts that make the namespace routable.
TRANSFER_FORWARD_TAG = "evpn-transfer-forward"


# Accepts for published addresses. A tag separate from the guard's:
# `guard_key` ignores the target, so shared tags would sweep each other.
PUBLISHED_TAG = "evpn-published"


def published_addresses() -> list:
    """What this host was last told the installation publishes."""
    try:
        with open(base.PUBLISHED_FILE) as fl:
            return [str(a) for a in json.load(fl)]
    except (OSError, ValueError, TypeError):
        return []


def set_published_addresses(addresses) -> bool:
    """Record them; say whether that changed anything."""
    wanted = sorted({str(a) for a in addresses or []})
    if wanted == published_addresses():
        return False
    sh.write_atomic(base.PUBLISHED_FILE, json.dumps(wanted, indent=1, sort_keys=True))
    return True


def published_spec(entry: str, gw: str) -> list | None:
    """One published entry as the match that lets it through.

    ``ADDRESS`` opens the whole address (a floating IP); ``ADDRESS:PORT``
    opens one TCP service, since a host address alone would expose all of
    ``0.0.0.0`` (sshd included).
    """
    address, _, port = str(entry).strip().rpartition(":")
    if not address:
        address, port = str(entry).strip(), ""
    if not flows.is_cidr(address):
        return None
    spec = ["-i", gw, "-d", address]
    if port:
        if not port.isdigit() or not 0 < int(port) < 65536:
            return None
        spec += ["-p", "tcp", "--dport", port]
    return spec


def ensure_published_reachable(vni: int) -> None:
    """Let this VNI's guests reach the addresses the installation published.

    Installed above the guard's drops; stale grants removed after install.
    """
    ns = vni_ns(vni)
    gw = "gw%d" % vni
    keys = set()
    for entry in published_addresses():
        spec = published_spec(entry, gw)
        if spec is None:
            continue
        keys.add(guard_key(" ".join(spec)))
        ensure_rule(
            "FORWARD",
            *spec,
            "-m",
            "comment",
            "--comment",
            PUBLISHED_TAG,
            "-j",
            "ACCEPT",
            ns=ns,
            table="filter",
            head=True,
        )
    _drop_stale_forward(ns, PUBLISHED_TAG, keys, "published grant")


def _tagged_forward(ns: str, tag: str) -> list:
    """The namespace's FORWARD rules carrying ``tag``, as iptables-save says."""
    return [
        line
        for line in saved_rules(ns, "filter").splitlines()
        if tag in line and line.startswith("-A FORWARD ")
    ]


def _drop_stale_forward(ns: str, tag: str, keys: set, what: str) -> None:
    """Remove ``tag``'s FORWARD rules whose `guard_key` is not in ``keys``."""
    for line in _tagged_forward(ns, tag):
        if guard_key(line) in keys:
            continue
        rule = line[len("-A FORWARD ") :].split()
        cmd = iptables_cmd("-D", "FORWARD", *rule, ns=ns, table="filter")
        if sh.try_run(cmd) is None:
            LOG.warning("evpn: could not drop a stale %s: %s", what, line)


def vnis_on_host() -> list:
    """The VNIs this host currently holds a namespace for."""
    found = map(evpn_records.ns_vni, netns_names())
    return sorted(vni for vni in found if vni is not None)


def published_is_installed(vni: int) -> bool:
    """Whether this VNI's namespace lets through everything it should.

    Read live: anything on the host may flush a chain.
    """
    gw = "gw%d" % vni
    specs = [published_spec(entry, gw) for entry in published_addresses()]
    wanted = {guard_key(" ".join(spec)) for spec in specs if spec is not None}
    if not wanted:
        return True
    have = {guard_key(line) for line in _tagged_forward(vni_ns(vni), PUBLISHED_TAG)}
    return wanted <= have


# Destinations a guest's egress may not reach: all non-globally-routable
# space (decision 6). Deliberately blunt; installation services are reached
# via the `proxy` function (namespace-local traffic, not transit).
INTERNAL_SPACE = (
    "10.0.0.0/8",  # RFC1918
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",  # RFC6598, carrier-grade NAT
    "169.254.0.0/16",  # link-local, which is where the metadata address is
    "127.0.0.0/8",
    "0.0.0.0/8",
    "224.0.0.0/4",  # multicast
    "240.0.0.0/4",  # reserved
)

# Fabric protocols too, for publicly addressed underlays. VXLAN matters
# most: tunnels are classified by `tun_id` alone, so reaching 4789 means
# injecting into any VNI.
FABRIC_PORTS = (("tcp", "179"), ("tcp", "50051"), ("udp", str(base.VXLAN_UDP_PORT)))


_GUARD_DST_RE = re.compile(r"-d (\S+)")


_GUARD_PROTO_RE = re.compile(r"-p (\S+)")


_GUARD_DPORT_RE = re.compile(r"--dport (\S+)")


# Part of the key: an unscoped drop is a different rule and gets replaced.
_GUARD_IN_RE = re.compile(r"-i (\S+)")


def normalized_prefix(value: str) -> str:
    """A prefix as both iptables and this driver spell it, or as it came.

    ``ip route`` prints a bare address, iptables-save a ``/32``; comparing
    raw strings would reinstall the rule every iteration.
    """
    try:
        return str(ipaddress.ip_network(value, strict=False))
    except ValueError:
        return value


def guard_key(text: str) -> tuple:
    """What a guard rule is about: (dst, proto, dport, in-iface).

    iptables-save adds its own match modules, so compare fields, not text.
    """
    dst = _GUARD_DST_RE.search(text)
    proto = _GUARD_PROTO_RE.search(text)
    dport = _GUARD_DPORT_RE.search(text)
    leg = _GUARD_IN_RE.search(text)
    return (
        normalized_prefix(dst.group(1)) if dst else "",
        proto.group(1) if proto else "",
        dport.group(1) if dport else "",
        leg.group(1) if leg else "",
    )


def transfer_net() -> str:
    """The whole range the per-VNI transfer /30s are carved out of.

    Must be guarded: a transfer address belongs to the root namespace, so
    reaching it reaches everything bound to ``0.0.0.0`` on the host. Guarded
    whole so rules do not churn with the VNIs served.
    """
    return "%d.%d.0.0/16" % base.EGRESS_TRANSFER_BASE


def underlay_prefixes() -> list[str]:
    """Every network this host is directly attached to (the underlay).

    Excludes the transfer range (see `transfer_net`). Empty when unreadable:
    the protocol rules stand alone rather than a guard being torn down.
    """
    try:
        routes = json.loads(sh.ip("-json", "route", "show", "scope", "link"))
        if not isinstance(routes, list):
            raise ValueError("expected a list of routes, got %s" % type(routes))
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError) as e:
        LOG.error(
            "evpn: cannot read this host's own networks (%s); the egress "
            "guard covers the fabric's protocols only",
            e,
        )
        return []
    transfer = ipaddress.ip_network(transfer_net())
    prefixes = set()
    for route in routes:
        if not isinstance(route, dict):
            continue
        dst = route.get("dst")
        if not dst or dst == "default" or not flows.is_cidr(dst):
            continue
        network = ipaddress.ip_network(dst, strict=False)
        if network.version == transfer.version and network.subnet_of(transfer):
            continue
        prefixes.add(str(network))
    return sorted(prefixes)


def internal_destinations() -> list[str]:
    """`INTERNAL_SPACE`, the transfer range, on-link networks outside it and
    this host's own addresses (a /32 on ``lo``, e.g. a router id, has no
    on-link route, yet is the host's every service).
    """
    space = [ipaddress.ip_network(p) for p in INTERNAL_SPACE]
    prefixes = list(INTERNAL_SPACE) + [transfer_net()]
    own = ["%s/32" % address for address in sorted(host_addresses() or ())]
    for prefix in underlay_prefixes() + own:
        network = ipaddress.ip_network(prefix, strict=False)
        if any(
            network.version == known.version and network.subnet_of(known)
            for known in space
        ):
            continue
        prefixes.append(prefix)
        # An address on a network already listed is covered by it.
        space.append(network)
    return prefixes


def ensure_transfer_forwarding(vni: int) -> None:
    """Let this VNI's namespace be routed to and from, in the root's chain.

    libvirt puts a blanket REJECT in its FORWARD chains (re-added whenever a
    network is recreated), which cuts the namespace off from e.g. the boot
    API. So both directions are accepted at the head of ``FORWARD``, on
    every reconcile.
    """
    upl = "upl%d" % vni
    for spec in (["-i", upl], ["-o", upl]):
        ensure_rule(
            "FORWARD",
            *spec,
            "-m",
            "comment",
            "--comment",
            TRANSFER_FORWARD_TAG,
            "-j",
            "ACCEPT",
            table="filter",
            head=True,
        )


def ensure_fabric_guard(vni: int) -> None:
    """Keep this VNI's guests out of the transport their overlay rides on.

    Drops `internal_destinations` in the egress namespace's FORWARD chain
    (the proxy/responder use OUTPUT, so they are unaffected). Decision 6 in
    exordos_core's docs/network/realm-connectivity-sdn.md. Drops match only
    the guests' leg, with ESTABLISHED accepted above them: FIP ingress and
    its replies cross this chain too. Install before remove.
    """
    ns = vni_ns(vni)
    gw = "gw%d" % vni
    wanted, keys = [], set()
    established = [
        "-m",
        "conntrack",
        "--ctstate",
        "ESTABLISHED,RELATED",
        "-m",
        "comment",
        "--comment",
        FABRIC_GUARD_TAG,
        "-j",
        "ACCEPT",
    ]
    for prefix in internal_destinations():
        wanted.append(["-i", gw, "-d", prefix])
    for proto, port in FABRIC_PORTS:
        wanted.append(["-i", gw, "-p", proto, "--dport", port])
    for spec in wanted:
        keys.add(guard_key(" ".join(spec)))
        rule = spec + ["-m", "comment", "--comment", FABRIC_GUARD_TAG, "-j", "DROP"]
        ensure_rule("FORWARD", *rule, ns=ns, table="filter", head=True)
    # Each head-insert lands above the previous: published grants above the
    # drops, then ESTABLISHED above all.
    ensure_published_reachable(vni)
    keys.add(guard_key(" ".join(established[:4])))
    ensure_rule("FORWARD", *established, ns=ns, table="filter", head=True)
    # Stale rules go only after the current ones are in place.
    _drop_stale_forward(ns, FABRIC_GUARD_TAG, keys, "egress guard")


def egress_leg(vni: int) -> str:
    """Root-side end of the gateway veth, plugged into the EVPN bridge."""
    return "gw%d-br" % vni


def client_conf(mac, routes, vni, ofport, imp_rt, exp_rt) -> str:
    """One evpn_connector client: an L3 endpoint on ``ofport`` of the EVPN
    bridge, announcing ``routes``.
    """
    return json.dumps(
        {
            "cfg_type": "l3",
            "mac": mac,
            "routes": routes,
            "vni": vni,
            "ofport": ofport,
            "type": "flat",
            "tag": 0,
            "imp_rt": list(imp_rt),
            "exp_rt": list(exp_rt),
        },
        indent=1,
        sort_keys=True,
    )


def egress_conf_path(vni: int) -> str:
    return os.path.join(base.CLIENT_CONF_DIR, "gw%d.json" % vni)


def egress_leg_plugged(vni: int) -> bool:
    """True while the gateway leg is on the EVPN bridge with a real ofport."""
    try:
        return sh.ofport(egress_leg(vni)) is not None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def egress_transfer(vni: int) -> tuple[str, str, str]:
    """Per-VNI transfer /30 between the SNAT namespace and the root ns.

    Returns ``(cidr, root_ip, ns_ip)``. Keep base.EGRESS_TRANSFER_BASE
    clear of the host's real underlay ranges.
    """
    off = (vni % 16384) * 4
    hi, lo = off >> 8, off & 0xFF
    first, second = base.EGRESS_TRANSFER_BASE
    prefix = "%d.%d.%d" % (first, second, hi)
    return (
        "%s.%d/30" % (prefix, lo),
        "%s.%d" % (prefix, lo + 1),
        "%s.%d" % (prefix, lo + 2),
    )


def egress_exp_rt(as_number: str, vni: int) -> str:
    """Host-unique, imported-by-nobody export RT for the egress leg.

    Otherwise local and remote copies of the prefix form an ECMP group and
    egress hairpins through a foreign host (decision 6). Built from the low
    two octets of the underlay address: unique only within a /16.
    """
    try:
        out = sh.ip("-json", "route", "get", base.DEFAULT_ROUTE_PROBE_IP)
        octets = json.loads(out)[0]["prefsrc"].split(".")
        host = (int(octets[2]) << 8) | int(octets[3])
        if int(octets[0]) or int(octets[1]):
            LOG.debug(
                "evpn: egress RT identifies this host by %s.%s only; hosts of "
                "this fabric outside the same /16 can collide",
                octets[2],
                octets[3],
            )
    except (
        subprocess.CalledProcessError,
        FileNotFoundError,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
    ):
        host = zlib.crc32(socket.gethostname().encode()) & 0xFFFF
    return "%s:%d" % (as_number, 0x80000000 | (host << 15) | (vni & 0x7FFF))


def egress_port_path(vni: int, port_uuid: object) -> str:
    """A port's gateway declaration, separate from the connector's clients."""
    return os.path.join(base.EGRESS_RECORDS_DIR, str(vni), str(port_uuid) + ".json")


class _EgressDemand:
    """Gateway and MTU contributions of this VNI's ports during one cycle."""

    def __init__(self, settings, nf_ports):
        self._ports = {}
        self._nf_ports = set()
        self._gateways = collections.Counter()
        self._mtus = collections.Counter()
        self.applied = None
        for name, spec in settings.items():
            self.replace(name, spec, name in nf_ports)

    @staticmethod
    def _spec(dhcp):
        cidr = dhcp.get("cidr")
        gateway = evpn_records.default_gateway(dhcp.get("routers"))
        if not cidr or not gateway:
            return None
        interface = ipaddress.IPv4Interface("%s/%s" % (gateway, cidr.split("/", 1)[1]))
        return str(interface), int(dhcp.get("mtu") or 1450)

    def replace(self, name, spec, nf_record):
        previous = self._ports.get(name)
        current = self._spec(spec or {})
        if nf_record:
            self._nf_ports.add(name)
        else:
            self._nf_ports.discard(name)
        if name in self._ports and previous == current:
            return
        for counter, old, new in (
            (
                self._gateways,
                previous[0] if previous else None,
                current[0] if current else None,
            ),
            (
                self._mtus,
                previous[1] if previous else None,
                current[1] if current else None,
            ),
        ):
            if old is not None:
                counter[old] -= 1
                if not counter[old]:
                    del counter[old]
            if new is not None:
                counter[new] += 1
        self._ports[name] = current

    def forget(self, name):
        self.replace(name, {}, False)
        self._ports.pop(name)

    def render(self, dhcp):
        gateways, contested = {}, set()
        for address in self._gateways:
            ip = address.split("/")[0]
            if ip in gateways:
                contested.add(ip)
            gateways[ip] = address
        current = self._spec(dhcp or {})
        mtus = self._mtus.keys()
        if current:
            ip = current[0].split("/")[0]
            gateways[ip] = current[0]
            contested.discard(ip)
            mtus = (*mtus, current[1])
        if contested:
            # Preserve file order while old and new prefixes coexist.
            for name in sorted(
                self._ports, key=lambda name: (name not in self._nf_ports, name)
            ):
                spec = self._ports[name]
                if spec and spec[0].split("/")[0] in contested:
                    gateways[spec[0].split("/")[0]] = spec[0]
        return gateways, str(min(mtus, default=1450))


def _egress_demand(vni: int) -> _EgressDemand:
    if _EGRESS_DEMANDS is not None and vni in _EGRESS_DEMANDS:
        return _EGRESS_DEMANDS[vni]
    settings = {}
    nf_dir = os.path.join(base.NF_RECORDS_DIR, str(vni))
    for name in sorted(os.listdir(nf_dir)) if os.path.isdir(nf_dir) else []:
        if not name.endswith(".json") or name.startswith(evpn_records.NF_CONFIG_PREFIX):
            continue
        with open(os.path.join(nf_dir, name)) as stream:
            settings[name] = json.load(stream)
    nf_ports = set(settings)
    directory = os.path.dirname(egress_port_path(vni, "unused"))
    for name in sorted(os.listdir(directory)) if os.path.isdir(directory) else []:
        if name.endswith(".json"):
            with open(os.path.join(directory, name)) as stream:
                settings[name] = json.load(stream)
    demand = _EgressDemand(settings, nf_ports)
    if _EGRESS_DEMANDS is not None:
        _EGRESS_DEMANDS[vni] = demand
    return demand


def egress_gateways(vni: int, dhcp: dict) -> tuple[dict[str, str], str]:
    """Aggregate declarations, falling back to NF records during upgrade."""
    return _egress_demand(vni).render(dhcp)


def forget_egress_port(vni: int, port_uuid: object) -> None:
    """Remove a detached port's gateway demand; retain gateways used by peers."""
    sh.remove(egress_port_path(vni, port_uuid))
    if _EGRESS_DEMANDS is not None and vni in _EGRESS_DEMANDS:
        demand = _EGRESS_DEMANDS[vni]
        name = str(port_uuid) + ".json"
        demand.forget(name)
        nf_record = os.path.join(base.NF_RECORDS_DIR, str(vni), name)
        if os.path.exists(nf_record):
            with open(nf_record) as stream:
                demand.replace(name, json.load(stream), True)
    if os.path.exists(egress_conf_path(vni)):
        with open(egress_conf_path(vni)) as stream:
            conf = json.load(stream)
        # No current port: the remaining records own the shared gateway.
        ensure_vni_egress(vni, {}, conf.get("imp_rt") or [])


def _sync_egress_gateways(vni: int, gateways: dict, mtu: str, imp_rt: list) -> None:
    """Change gateway addresses without deleting shared links, NAT or FIPs."""
    path = egress_conf_path(vni)
    with open(path) as stream:
        conf = json.load(stream)
    ns = vni_ns(vni)
    gateway_link = "gw%d" % vni
    desired = ["%s/32" % ip for ip in sorted(gateways)] + ["0.0.0.0/0"]
    old_ips = {
        route.split("/")[0] for route in conf.get("routes", []) if route.endswith("/32")
    }
    for address in gateways.values():
        sh.ns_ip(ns, "addr", "replace", address, "dev", gateway_link)
    stale = old_ips - gateways.keys()
    if stale:
        addresses = json.loads(
            sh.ns_ip(ns, "-json", "addr", "show", "dev", gateway_link)
        )
        for link in addresses:
            for address in link.get("addr_info", []):
                if address.get("family") == "inet" and address.get("local") in stale:
                    sh.ns_ip(
                        ns,
                        "addr",
                        "del",
                        "%s/%s" % (address["local"], address["prefixlen"]),
                        "dev",
                        gateway_link,
                    )
    sh.ns_ip(ns, "link", "set", gateway_link, "mtu", mtu)
    conf["routes"] = desired
    conf["imp_rt"] = list(imp_rt)
    sh.write_if_changed(path, json.dumps(conf, indent=1, sort_keys=True))


def ensure_vni_egress(
    vni: int, dhcp: dict, imp_rt: list[str], port_uuid: object | None = None
) -> None:
    """Anycast gateway + DVR egress for a VNI (design decision 6).

    The namespace holds the gateway on a veth leg plugged into the EVPN
    bridge; egress is masqueraded in the namespace and again in the root.
    Skipped when the CP subnet defines no gateway.
    """
    if port_uuid is not None:
        sh.write_if_changed(
            egress_port_path(vni, port_uuid), json.dumps(dhcp or {}, sort_keys=True)
        )
    demand = _egress_demand(vni)
    if port_uuid is not None:
        name = str(port_uuid) + ".json"
        demand.replace(
            name,
            dhcp,
            os.path.exists(os.path.join(base.NF_RECORDS_DIR, str(vni), name)),
        )
    gateways, mtu = demand.render(dhcp)
    if not imp_rt:
        return
    if not gateways:
        # A remaining peer may still own FIPs. Whole egress collection belongs
        # to the last guest on the VNI, not the last explicit gateway demand.
        return
    desired = (tuple(sorted(gateways.items())), mtu, tuple(imp_rt))
    if demand.applied == desired and vni in _GUARDED_VNIS:
        return
    demand.applied = None
    gw_ip = sorted(gateways)[0]
    ns = ensure_vni_ns(vni)
    # Above the early return: an existing egress needs the guard and the
    # forwarding re-asserted too (libvirt re-adds its REJECT).
    if vni not in _GUARDED_VNIS:
        ensure_fabric_guard(vni)
        ensure_transfer_forwarding(vni)
        _GUARDED_VNIS.add(vni)
    conf = egress_conf_path(vni)
    if os.path.exists(conf):
        if egress_leg_plugged(vni) and link_in_ns(ns, "gw%d" % vni) is not None:
            _sync_egress_gateways(vni, gateways, mtu, imp_rt)
            demand.applied = desired
            return
        LOG.info("evpn: egress for vni %d is stale, rebuilding", vni)
        gc_vni_egress(vni)
    else:
        # Config is written last: a half-failed build leaves veths that
        # would make `link add` fail forever.
        remove_egress_links(vni)

    gw = "gw%d" % vni
    gw_br = egress_leg(vni)
    upl = "upl%d" % vni
    upl_ns = "%s-ns" % upl
    tr_cidr, tr_root, tr_ns = egress_transfer(vni)

    sh.ip("link", "add", gw, "type", "veth", "peer", "name", gw_br)
    sh.ip("link", "set", gw, "netns", ns)
    sh.ns_ip(ns, "link", "set", gw, "mtu", mtu)
    sh.ns_ip(ns, "link", "set", gw, "up")
    for address in gateways.values():
        sh.ns_ip(ns, "addr", "replace", address, "dev", gw)
    sh.ip("link", "set", gw_br, "up")
    # High per-VNI ofport: evpn_connector silently drops a client whose
    # ofport collides with a guest patch's.
    sh.ovs(
        "--may-exist",
        "add-port",
        base.EVPN_BRIDGE,
        gw_br,
        "--",
        "set",
        "interface",
        gw_br,
        "ofport_request=%d" % (base.EGRESS_OFPORT_BASE + vni % 10000),
    )
    # Transfer leg to the root namespace + double NAT
    sh.ip("link", "add", upl, "type", "veth", "peer", "name", upl_ns)
    sh.ip("link", "set", upl_ns, "netns", ns)
    sh.ip("addr", "replace", "%s/30" % tr_root, "dev", upl)
    sh.ip("link", "set", upl, "up")
    sh.ns_ip(ns, "addr", "replace", "%s/30" % tr_ns, "dev", upl_ns)
    sh.ns_ip(ns, "link", "set", upl_ns, "up")
    sh.ns_ip(ns, "link", "set", "lo", "up")
    sh.ns_ip(ns, "route", "replace", "default", "via", tr_root)
    sh.ns_run(ns, "sysctl", "-qw", "net.ipv4.ip_forward=1")
    ensure_rule("POSTROUTING", "-o", upl_ns, "-j", "MASQUERADE", ns=ns)
    ensure_rule("POSTROUTING", "-s", tr_cidr, "-j", "MASQUERADE")

    ofport = sh.ofport(gw_br)
    if ofport is None:
        raise driver_exc.InvalidDataPlaneObjectError(obj={"port": gw_br})
    mac = sh.ns_run(ns, "cat", "/sys/class/net/%s/address" % gw).strip()
    as_number = str(imp_rt[0]).split(":", 1)[0]
    sh.write_atomic(
        conf,
        client_conf(
            mac,
            ["%s/32" % ip for ip in sorted(gateways)] + ["0.0.0.0/0"],
            vni,
            ofport,
            imp_rt,
            [egress_exp_rt(as_number, vni)],
        ),
    )
    demand.applied = desired
    LOG.info("evpn: egress gateway for vni %d ready (%s)", vni, gw_ip)


def egress_matches(conf_path: str, gw_ip: str, dhcp: dict) -> bool:
    """True when the rendered egress config still matches gateway and MTU."""
    try:
        with open(conf_path) as fl:
            conf = json.load(fl)
    except (OSError, ValueError):
        return False
    if "%s/32" % gw_ip not in (conf.get("routes") or []):
        return False
    try:
        mtu = sh.ns_run(
            vni_ns(int(conf["vni"])),
            "cat",
            "/sys/class/net/gw%d/mtu" % int(conf["vni"]),
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, KeyError, ValueError):
        return False
    return mtu == str((dhcp or {}).get("mtu") or 1450)


def vni_in_use(vni: int, skip_uuid: object) -> bool:
    """True while any other guest client config still carries this VNI."""
    if _CLIENTS is not None:
        if "by_uuid" not in _CLIENTS:
            by_uuid, by_vni = {}, collections.defaultdict(set)
            try:
                names = os.listdir(base.CLIENT_CONF_DIR)
            except FileNotFoundError:
                names = []
            for name in names:
                if not name.endswith(".json") or re.fullmatch(r"gw\d+\.json", name):
                    continue
                try:
                    with open(os.path.join(base.CLIENT_CONF_DIR, name)) as stream:
                        client_vni = json.load(stream).get("vni")
                    if not isinstance(client_vni, int):
                        continue
                    by_uuid[name] = client_vni
                    by_vni[client_vni].add(name)
                except (OSError, ValueError):
                    continue
            _CLIENTS.update(by_uuid=by_uuid, by_vni=by_vni)
        names = _CLIENTS["by_vni"].get(vni, set())
        return len(names) > int("%s.json" % skip_uuid in names)
    try:
        names = os.listdir(base.CLIENT_CONF_DIR)
    except FileNotFoundError:
        return False
    for name in names:
        if not name.endswith(".json") or name == "gw%d.json" % vni:
            continue
        if name == "%s.json" % skip_uuid:
            continue
        try:
            with open(os.path.join(base.CLIENT_CONF_DIR, name)) as fl:
                if json.load(fl).get("vni") == vni:
                    return True
        except (OSError, ValueError):
            continue
    return False


def record_client(port_uuid: object, vni: int | None) -> None:
    """Update the cycle's occupancy index after a successful file mutation."""
    if _CLIENTS is None or "by_uuid" not in _CLIENTS:
        return
    name = "%s.json" % port_uuid
    previous = _CLIENTS["by_uuid"].pop(name, None)
    if previous in _CLIENTS["by_vni"]:
        _CLIENTS["by_vni"][previous].discard(name)
    if vni is not None:
        _CLIENTS["by_uuid"][name] = vni
        _CLIENTS["by_vni"][vni].add(name)


def fip_tag(uuid: object) -> str:
    return "evpn-fip-%s" % uuid


def fip_link(public: str) -> str | None:
    """The interface a floating address is on-link on, if it is on one.

    Where (and whether) to proxy-ARP for it; routed-in addresses need none.
    Read from interface addresses, not `ip route get`, which returns our own
    host route and yields a false "missing" that reinstalls forever.
    """
    try:
        target = ipaddress.ip_address(str(public))
    except (ValueError, TypeError):
        return None
    for ifname, local, plen in _inet_addresses() or ():
        if ifname == "lo" or plen is None:
            continue
        try:
            segment = ipaddress.ip_network("%s/%s" % (local, plen), strict=False)
        except ValueError:
            continue
        if target in segment:
            return ifname
    return None


def ensure_fip_arp(public: str) -> None:
    """Answer for a floating address on the segment it belongs to.

    A per-address proxy entry, never the `proxy_arp` sysctl (which answers
    for every routed prefix), and never `ip addr add` (which would expose
    the host's own services on it).
    """
    link = fip_link(public)
    if not link:
        return
    cmd = [base.IP_BIN, "neigh", "replace", "proxy", public, "dev", link]
    if sh.try_run(cmd) is None:
        LOG.warning("evpn: could not answer for floating address %s", public)


# Ethernet and ARP, enough to say "this address is here now" and no more.
_ETH_P_ARP = 0x0806
_ARP_REQUEST = 1
_BROADCAST = b"\xff" * 6


def _link_mac(link: str) -> str | None:
    try:
        info = json.loads(sh.ip("-json", "link", "show", link))[0]
    except (
        subprocess.CalledProcessError,
        FileNotFoundError,
        ValueError,
        IndexError,
        KeyError,
    ):
        return None
    return info.get("address")


def announce_fip(public: str) -> None:
    """Send a gratuitous ARP for a floating address that just moved here.

    Otherwise peers keep the old host's MAC until the cache ages out.
    Best-effort: failure only costs that wait.
    """
    link = fip_link(public)
    if not link:
        return
    mac = _link_mac(link)
    if not mac:
        return
    try:
        hw = bytes.fromhex(mac.replace(":", ""))
        addr = socket.inet_aton(public)
    except (ValueError, OSError):
        return
    arp = struct.pack("!HHBBH", 1, 0x0800, 6, 4, _ARP_REQUEST)
    arp += hw + addr + b"\x00" * 6 + addr
    frame = _BROADCAST + hw + struct.pack("!H", _ETH_P_ARP) + arp
    try:
        with socket.socket(
            socket.AF_PACKET, socket.SOCK_RAW, socket.htons(_ETH_P_ARP)
        ) as sock:
            sock.bind((link, 0))
            sock.send(frame)
    except OSError as e:
        LOG.info("evpn: could not announce %s on %s: %s", public, link, e)


def proxied_fips() -> dict[str, str]:
    """Floating addresses this host answers for, and where."""
    out = sh.try_run([base.IP_BIN, "neigh", "show", "proxy"])
    if out is None:
        return {}
    proxied = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and "dev" in parts:
            proxied[parts[0]] = parts[parts.index("dev") + 1]
    return proxied


def delete_fip_arp(public: str) -> None:
    """Stop answering for an address, wherever this host was answering."""
    link = proxied_fips().get(public)
    if link:
        sh.try_run([base.IP_BIN, "neigh", "del", "proxy", public, "dev", link])


def saved_rules(ns: str, table: str) -> str:
    """One table of a namespace's firewall; empty if the namespace is absent."""
    return _iptables_save(ns, table) or ""


def _iptables_save(ns: str, table: str) -> str | None:
    cmd = [base.IP_BIN, "netns", "exec", ns, base.IPTABLES_SAVE_BIN, "-t", table]
    return sh.try_run(cmd)


def fip_rules(vni: int) -> str:
    """The nat rules of this VRF's egress namespace."""
    return saved_rules(vni_ns(vni), "nat")


_UPL_RE = re.compile(r"-[io] upl(\d+)\b")


def forwarded_vnis() -> set | None:
    """VNIs the root chain currently routes both to and from.

    One read per host. ``None`` when unreadable (never reported as drift);
    a VNI counts only with both legs.
    """
    saved = sh.try_run([base.IPTABLES_SAVE_BIN, "-t", "filter"])
    if saved is None:
        return None
    legs: dict = {}
    for line in saved.splitlines():
        if not line.startswith("-A FORWARD ") or TRANSFER_FORWARD_TAG not in line:
            continue
        found = _UPL_RE.search(line)
        if found:
            legs.setdefault(int(found.group(1)), set()).add(line.split()[2])
    return {vni for vni, seen in legs.items() if seen == {"-i", "-o"}}


#: `-A PREROUTING -d <public>/32 ... -j DNAT --to-destination <private>`
_FIP_DNAT_RE = re.compile(
    r"-d (\d+\.\d+\.\d+\.\d+)\S*\s.*--to-destination (\d+\.\d+\.\d+\.\d+)"
)


def fip_mappings(tag: str, vni: int) -> set:
    """The (public, private) pairs this port's rules already program."""
    pairs = set()
    for line in fip_rules(vni).splitlines():
        if tag not in line or not line.startswith("-A "):
            continue
        found = _FIP_DNAT_RE.search(line)
        if found:
            pairs.add((found.group(1), found.group(2)))
    return pairs


def delete_fip_rules(tag: str, vni: int) -> None:
    """Best-effort delete of this port's fip rules (by comment)."""
    ns = vni_ns(vni)
    for line in fip_rules(vni).splitlines():
        if tag in line and line.startswith("-A "):
            sh.try_run(iptables_cmd("-D", *line[len("-A ") :].split(), ns=ns))
    release_unclaimed_fips(vni)


def release_unclaimed_fips(vni: int) -> None:
    """Drop root-side routes/ARP of this VNI's floating addresses no DNAT claims.

    Only routes via this VNI's transfer are considered, so an address that
    moved to another guest or VNI keeps its root half.
    """
    claimed = {
        found.group(1)
        for found in map(_FIP_DNAT_RE.search, fip_rules(vni).splitlines())
        if found
    }
    release_fips(vni, keep=claimed)


def release_fips(vni: int, keep: set) -> None:
    """Drop the root halves of this VNI's floating addresses but ``keep``."""
    _, _tr_root, tr_ns = egress_transfer(vni)
    routes = sh.try_run([base.IP_BIN, "route", "show", "via", tr_ns]) or ""
    for public in re.findall(r"^(\d+\.\d+\.\d+\.\d+)(?:/32)? ", routes, re.M):
        if public in keep:
            continue
        sh.try_run([base.IP_BIN, "route", "del", "%s/32" % public, "via", tr_ns])
        # Keep the ARP answer while another VNI here still routes it.
        still_routed = sh.try_run([base.IP_BIN, "route", "show", "%s/32" % public])
        if not (still_routed or "").strip():
            delete_fip_arp(public)


def ensure_fip_root(public: str, tr_ns: str) -> None:
    """Root half of a floating address: ARP answer and host route (replaces)."""
    ensure_fip_arp(public)
    sh.ip("route", "replace", "%s/32" % public, "via", tr_ns)


def ensure_fip_nat(private_ip: str, fips: list, tag: str, vni: int) -> None:
    """Program 1:1 NAT for each floating IP bound to a guest.

    DNAT/SNAT inside the VRF's egress namespace (tenant CIDRs overlap), plus
    a root host route into it. Rules carry the port's ``tag`` comment.
    """
    wanted = {
        (str(fip["public"]), private_ip) for fip in fips or [] if fip.get("public")
    }
    _, _tr_root, tr_ns = egress_transfer(vni)
    if wanted and fip_mappings(tag, vni) == wanted:
        # Unchanged: rebuilding would open a DNAT gap. Re-assert the root
        # half, or a lost ARP answer/route stays "missing" forever.
        for public, _private in sorted(wanted):
            ensure_fip_root(public, tr_ns)
        return
    delete_fip_rules(tag, vni)
    if not fips:
        return
    ns = vni_ns(vni)
    for fip in fips or []:
        public = fip.get("public")
        if not public:
            continue
        ensure_fip_root(public, tr_ns)
        announce_fip(public)
        comment = ("-m", "comment", "--comment", tag)
        iptables(
            "-I",
            "PREROUTING",
            "-d",
            public,
            *comment,
            "-j",
            "DNAT",
            "--to-destination",
            private_ip,
            ns=ns,
        )
        # At the head, above the namespace's blanket masquerade.
        iptables(
            "-I",
            "POSTROUTING",
            "-s",
            private_ip,
            *comment,
            "-j",
            "SNAT",
            "--to-source",
            public,
            ns=ns,
        )


def gc_vni_egress(vni: int) -> None:
    """Remove this VNI's egress wiring (best-effort), keeping its namespace.

    The namespace goes with the VNI's last guest (`gc_vni`).
    """
    if _EGRESS_DEMANDS is not None and vni in _EGRESS_DEMANDS:
        _EGRESS_DEMANDS[vni].applied = None
    tr_cidr, _, _ = egress_transfer(vni)
    sh.remove(egress_conf_path(vni))
    # Before the links: the kernel drops their routes with them, and those
    # routes are how this VNI's floating addresses are found.
    release_fips(vni, keep=set())
    remove_egress_links(vni)
    sh.try_run(iptables_cmd("-D", "POSTROUTING", "-s", tr_cidr, "-j", "MASQUERADE"))


def remove_egress_links(vni: int) -> None:
    """The veth pairs and the bridge port of a VNI's egress, if any exist."""
    ns = vni_ns(vni)
    for cmd in (
        [
            base.OVSVSCTL_BIN,
            "--if-exists",
            "del-port",
            base.EVPN_BRIDGE,
            egress_leg(vni),
        ],
        [base.IP_BIN, "link", "del", egress_leg(vni)],
        [base.IP_BIN, "link", "del", "upl%d" % vni],
        # The gateway leg lives inside the namespace, which stays.
        [base.IP_BIN, "netns", "exec", ns, base.IP_BIN, "link", "del", "gw%d" % vni],
    ):
        sh.try_run(cmd)


def gc_vni(vni: int) -> None:
    """Collect a VNI's whole footprint on this host: its last guest is gone."""
    _GUARDED_VNIS.discard(vni)
    if _NF_PORTS is not None:
        _NF_PORTS.pop(vni, None)
    gc_vni_egress(vni)
    if _EGRESS_DEMANDS is not None:
        _EGRESS_DEMANDS.pop(vni, None)
    sh.try_run(
        [base.OVSVSCTL_BIN, "--if-exists", "del-port", base.BR_INT, nf_port(vni)]
    )
    # Or the VLAN map leaks an entry per VNI ever served.
    release_local_vlan(nf_vlan_key(vni))
    sh.try_run([base.IP_BIN, "netns", "del", vni_ns(vni)])


def gc_guest(port_uuid: object) -> None:
    """Tear down a guest's patch pair and VLAN (best-effort); see `gc_vni`."""
    vlan = lookup_local_vlan(port_uuid)
    if vlan is None:
        return
    for cmd in (
        [base.OVSVSCTL_BIN, "--if-exists", "del-port", base.BR_INT, "pi%d" % vlan],
        [base.OVSVSCTL_BIN, "--if-exists", "del-port", base.EVPN_BRIDGE, "pe%d" % vlan],
    ):
        if sh.try_run(cmd) is None:
            return
    release_local_vlan(port_uuid)


def fip_withdrawal_confirmed(publics: list[str], vni: int, port_uuid: object) -> bool:
    """Require readable, absent NAT/routes/proxy ARP before acknowledging."""
    if not publics:
        return True
    namespaces = sh.try_run([base.IP_BIN, "netns", "list"])
    routes = sh.try_run([base.IP_BIN, "route", "show"])
    neighbours = sh.try_run([base.IP_BIN, "neigh", "show", "proxy"])
    if namespaces is None or routes is None or neighbours is None:
        return False
    ns = vni_ns(vni)
    if ns in {line.split()[0] for line in namespaces.splitlines() if line.split()}:
        rules = _iptables_save(ns, "nat")
        if rules is None:
            return False
        for line in rules.splitlines():
            try:
                args = shlex.split(line)
            except ValueError:
                return False
            if fip_tag(port_uuid) not in args:
                continue
            for index, arg in enumerate(args[:-1]):
                if arg in ("-d", "--destination", "--to-source"):
                    address = args[index + 1].split("/")[0]
                    if address in publics:
                        return False
    route_ips = {
        line.split()[0].split("/")[0] for line in routes.splitlines() if line.split()
    }
    proxy_ips = {line.split()[0] for line in neighbours.splitlines() if line.split()}
    return not set(publics) & (route_ips | proxy_ips)
