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

"""The OpenFlow half of a port on br-int: NF intercepts and the SG pipeline."""

import hashlib
import ipaddress
import json
import logging
import re
import subprocess
import typing as tp

from gcl_sdk.agents.universal.drivers.evpn import base
from gcl_sdk.agents.universal.drivers.evpn import sh

LOG = logging.getLogger(__name__)


# A port's identity is the low 16 bits of the skb mark, copied into VXLAN-GBP
# at the tunnel. It is a *set*: one bit per group, matched under a mask.
IDENTITY_MAX = 0xFFFF


def port_cookie(uuid: object, kind: str) -> str:
    """Stable 64-bit OpenFlow cookie for a port's ``kind`` (nf/sg) flows."""
    digest = hashlib.blake2b(
        ("%s:%s" % (kind, uuid)).encode(), digest_size=8
    ).hexdigest()
    return "0x" + digest


# Every `ct_state` match and `ct()` action needs an explicit `ip,` (or
# another dl_type): without one ovs-ofctl rejects the whole bundle.
_SG_TABLE = 10  # conntrack decision table for the guest's own traffic


_SG_INGRESS_TABLE = 11  # ... and for traffic delivered to it


# Above the SG entries (90/91), so a default-deny group never has to carve
# out an exception for the guest's own DHCP or resolver.
_NF_INTERCEPT_PRIORITY = 100


# Below the SG entries (90/91), which already carry the source match: the
# ARP a pinned guest may still send, and the drop of everything else.
_PS_ARP_PRIORITY = 89


_PS_DROP_PRIORITY = 88


def is_cidr(value: object) -> bool:
    """True for a literal address or prefix — nothing else reaches a flow."""
    try:
        ipaddress.ip_network(str(value), strict=False)
    except ValueError:
        return False
    return True


_MAC_RE = re.compile(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")


def is_mac(value: object) -> bool:
    """True for a well-formed MAC address.

    Defensive: a comma or newline in a flow spec would inject match fields
    or whole ``flow add``/``flow delete`` lines.
    """
    return isinstance(value, str) and bool(_MAC_RE.match(value))


def rule_direction(rule: dict) -> str:
    return "ingress" if str(rule.get("direction", "egress")) == "ingress" else "egress"


def compile_sg_match(rule: dict) -> str | None:
    """Compile one security-group rule to an OVS match fragment, or None.

    Rule: ``{direction: egress|ingress, proto: tcp|udp|icmp|any, port?: int,
    remote?: cidr}``; ``remote`` is the far end. Unparseable rules grant
    nothing (fail-closed).
    """
    proto = str(rule.get("proto", "any")).lower()
    if proto == "any":
        base = "ip"
    elif proto in ("tcp", "udp", "icmp"):
        base = proto
    else:
        return None
    parts = [base]
    group = rule.get("remote_identity")
    if group is not None:
        # The mark is the *sender's* identity, so only "from that group"
        # (ingress) can be expressed; an egress rule naming one grants nothing.
        if rule_direction(rule) != "ingress":
            LOG.error("evpn: remote_identity is meaningful on ingress only, skipping")
            return None
        try:
            gid = int(group)
        except (TypeError, ValueError):
            LOG.error(
                "evpn: security rule has a bad remote_identity %r, skipping", group
            )
            return None
        if not 0 < gid <= IDENTITY_MAX:
            LOG.error("evpn: remote_identity %r is out of range, skipping", group)
            return None
        # Own mask: the mark is a set of group bits.
        parts.append("pkt_mark=%d/%d" % (gid, gid))
    remote = rule.get("remote")
    if remote:
        # IPv4 only: `ip,nw_src=<v6>` makes ovs-ofctl refuse the whole
        # bundle. Lands verbatim in a flow spec, where a comma starts a field.
        if (
            not is_cidr(remote)
            or ipaddress.ip_network(str(remote), strict=False).version != 4
        ):
            LOG.error("evpn: security rule has a bad remote %r, skipping", remote)
            return None
        field = "nw_src" if rule_direction(rule) == "ingress" else "nw_dst"
        parts.append("%s=%s" % (field, remote))
    port = rule.get("port")
    if port is not None and proto in ("tcp", "udp"):
        parts.append("tp_dst=%d" % int(port))
    return ",".join(parts)


def _proxy_ports(config: dict) -> set:
    """The extra ports a proxy grant claims; bad entries are dropped, not fatal."""
    ports = set()
    for port in (config or {}).get("ports") or ():
        try:
            number = int(port)
        except (TypeError, ValueError):
            continue
        if 0 < number < 65536:
            ports.add(number)
    return ports


def _nf_destination_matches(config: dict) -> list[str]:
    # Older target resources did not carry an address selector.
    if "destinations" not in config:
        return [""]
    values = config.get("destinations")
    if not isinstance(values, (list, tuple)):
        return []
    addresses = set()
    for value in values:
        if not isinstance(value, str):
            continue
        try:
            address = ipaddress.ip_address(value)
        except (ValueError, TypeError):
            continue
        if address.version == 4:
            addresses.add(str(address))
    return ["nw_dst=%s," % address for address in sorted(addresses)]


def ensure_port_nf_intercepts(
    kinds: dict,
    guest_ofport: int,
    nf_ofport: int,
    guest_mac: str,
    cookie: str,
    sources: tp.Sequence[str] = (),
    port_security: bool = False,
    zone: int = 0,
) -> None:
    """One guest's NF path: what is stolen from it, and what comes back.

    Per guest, above the SG pipeline, one delete-and-re-add bundle (see
    exordos_core docs/network/realm-connectivity-sdn.md). These flows sit
    above the port-security drop, so they carry its source match themselves;
    DHCP is exempt, since a guest without a lease has no address yet.

    Intercepts are committed to the guest's conntrack ``zone``: a reply from
    the gateway address comes back over the fabric patch, and without a ct
    entry a default-deny group would drop it as new traffic.
    """
    lines = ["flow delete cookie=%s/-1" % cookie]
    # Not for DHCP: its offer is delivered by the return leg below.
    commit = "ct(commit,zone=%d)," % zone if zone else ""
    allowed = [source for source in sources or () if is_cidr(source)]
    # NF callers are identified by address; general anti-spoof cannot disable
    # this boundary. DHCP alone is exempt while a guest has no address.
    guards = ["dl_src=%s,nw_src=%s," % (guest_mac, source) for source in allowed]
    wanted = []
    unguarded = []
    if "dhcp" in kinds:
        unguarded.append(("udp", 67))
    if "dns" in kinds:
        wanted += [("udp", 53, kinds["dns"]), ("tcp", 53, kinds["dns"])]
    if "proxy" in kinds:
        # Only the responder's addresses are claimed: another guest can
        # serve these same ports (e.g. a nested realm's own control plane).
        # Door ports and destination selectors ride in the port's descriptor
        # so changing them triggers a reapply.
        wanted += [
            ("tcp", platform, kinds["proxy"])
            for platform in sorted(
                _proxy_ports(kinds.get("proxy") or {}) | set(base.PLATFORM_PORTS)
            )
        ]
        # Metadata HTTP: guests target the connector's ARP-proxy router MAC,
        # which the namespace kernel would otherwise drop.
        for guard in guards:
            lines.append(
                "flow add cookie=%s,priority=%d,%stcp,in_port=%s,nw_dst=%s,"
                "tp_dst=80,actions=%smod_dl_dst:%s,output:%d"
                % (
                    cookie,
                    _NF_INTERCEPT_PRIORITY + 10,
                    guard,
                    guest_ofport,
                    base.METADATA_IP,
                    commit,
                    base.METADATA_MAC,
                    nf_ofport,
                )
            )
    for proto, port in unguarded:
        lines.append(
            "flow add cookie=%s,priority=%d,%s,in_port=%s,tp_dst=%d,"
            "actions=mod_dl_dst:%s,output:%d"
            % (
                cookie,
                _NF_INTERCEPT_PRIORITY,
                proto,
                guest_ofport,
                port,
                base.METADATA_MAC,
                nf_ofport,
            )
        )
    for proto, port, config in wanted:
        # Rewrite dl_dst: otherwise a frame for the gateway MAC reaches a
        # namespace socket only if the interface is promiscuous.
        for guard in guards:
            for destination in _nf_destination_matches(config):
                lines.append(
                    "flow add cookie=%s,priority=%d,%s%s,in_port=%s,%stp_dst=%d,"
                    "actions=%smod_dl_dst:%s,output:%d"
                    % (
                        cookie,
                        _NF_INTERCEPT_PRIORITY,
                        guard,
                        proto,
                        guest_ofport,
                        destination,
                        port,
                        commit,
                        base.METADATA_MAC,
                        nf_ofport,
                    )
                )
    if wanted or unguarded or "proxy" in kinds:
        lines.append(
            "flow add cookie=%s,priority=%d,in_port=%d,dl_dst=%s,"
            "actions=output:%s"
            % (
                cookie,
                _NF_INTERCEPT_PRIORITY,
                nf_ofport,
                guest_mac,
                guest_ofport,
            )
        )
    sh.run_flow_bundle(lines)


_CT_PROTO = {"tcp": 6, "udp": 17, "icmp": 1}


def _canonical_rules(rules) -> list:
    """The allow-list in a form two applies can be compared by."""
    return sorted(
        (json.dumps(r, sort_keys=True, separators=(",", ":")) for r in rules or []),
    )


def _revocation_tuple(rule: dict, guest_ip: str | None) -> str | None:
    """The partial conntrack tuple a withdrawn rule was holding open, or None.

    The far end (a remote CIDR or group mark) is not expressible in the
    tuple, so a rule cuts by guest, protocol and port only.
    """
    fields = []
    if guest_ip:
        side = "ct_nw_src" if rule_direction(rule) == "egress" else "ct_nw_dst"
        fields.append("%s=%s" % (side, guest_ip))
    proto = str(rule.get("proto", "any")).lower()
    if proto in _CT_PROTO:
        fields.append("ct_nw_proto=%d" % _CT_PROTO[proto])
    port = rule.get("port")
    if port is not None and proto in ("tcp", "udp"):
        try:
            fields.append("ct_tp_dst=%d" % int(port))
        except (TypeError, ValueError):
            return None
    return ",".join(fields) or None


def cut_revoked_sessions(port_uuid, zone: int, rules, guest_ip=None) -> None:
    """Flush the conntrack sessions of rules that left this port's allow-list.

    `ct_state=+est+trk` is admitted outright, so without this a revoked rule
    would be honoured by connections opened while it held. The tuple cannot
    name the far end (NAT would defeat it anyway), so this may cut sessions
    another rule still permits; they reconnect. Nothing is cut on first apply.
    """
    wanted = _canonical_rules(rules)
    known = {}
    try:
        with open(base.SG_RULES_PATH) as fl:
            known = json.load(fl)
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        LOG.warning(
            "evpn: cannot read %s; this pass cannot tell which rules were "
            "withdrawn, so nothing it holds open is cut",
            base.SG_RULES_PATH,
        )
    key = str(port_uuid)
    previous = known.get(key)
    if previous == wanted:
        return
    wanted_set = set(wanted)
    revoked = [json.loads(r) for r in previous or [] if r not in wanted_set]
    for rule in revoked:
        ct_tuple = _revocation_tuple(rule, guest_ip)
        if ct_tuple is None:
            continue
        LOG.info(
            "evpn: %s no longer permits %s; cutting what it held open",
            key,
            ct_tuple,
        )
        try:
            sh.appctl("dpctl/flush-conntrack", "zone=%d" % int(zone), ct_tuple)
        except (subprocess.CalledProcessError, FileNotFoundError):
            LOG.warning(
                "evpn: could not cut the sessions %s was holding open; a "
                "revoked rule is still honoured by whatever was already open",
                ct_tuple,
            )
            raise
    known[key] = wanted
    sh.write_atomic(base.SG_RULES_PATH, json.dumps(known, sort_keys=True))


def forget_sg_rules(port_uuid) -> None:
    """Drop a deleted port's allow-list, so its uuid never grows stale."""
    try:
        with open(base.SG_RULES_PATH) as fl:
            known = json.load(fl)
    except (OSError, ValueError):
        return
    if known.pop(str(port_uuid), None) is not None:
        sh.write_atomic(base.SG_RULES_PATH, json.dumps(known, sort_keys=True))


def security_group_installs(
    rules: list,
    guest_mac: str,
    identity: int = 0,
    sources: tp.Sequence[str] = (),
    port_security: bool = False,
) -> bool:
    """Whether `ensure_security_group` leaves any flow under the port's cookie.

    Rules, an identity stamp and port security each install flows of their own.
    """
    if not is_mac(guest_mac):
        return False
    if rules or identity:
        return True
    return bool(port_security) and any(is_cidr(s) for s in sources or ())


def ensure_security_group(
    guest_ofport: int,
    vlan: int,
    rules: list,
    cookie: str,
    guest_mac: str,
    delivery_ofport: int | None = None,
    identity: int = 0,
    sources: tp.Sequence[str] = (),
    port_security: bool = False,
) -> None:
    """Install the per-guest stateful security group on br-int.

    An allow-list both ways (AWS-SG model); no group means unfiltered. One
    conntrack table per direction, scoped by ``in_port``/``dl_dst`` and by ct
    ``zone`` so overlapping realm CIDRs stay apart. Load-bearing: the ingress
    entry matches the delivery ofport, ``port_security`` pins every entry to
    the guest's addresses, and the set travels as one bundle.
    """
    if not is_mac(guest_mac):
        LOG.warning("evpn: refusing SG flows for invalid guest mac %r", guest_mac)
        return
    lines = ["flow delete cookie=%s/-1" % cookie]
    # Identity is stamped even on an unfiltered port: others may name it.
    mark = "set_field:%d->pkt_mark," % identity if identity else ""
    # Port security with no address would blackhole the guest: switch it off.
    allowed = [source for source in sources or () if is_cidr(source)]
    # A conntrack tuple takes an address, not a prefix.
    guest_ip = next(
        (a for a in allowed if "/" not in str(a)),
        None,
    )
    if port_security and not allowed:
        LOG.warning(
            "evpn: port security asked for on %s with no address to pin it to, "
            "not enforcing",
            guest_ofport,
        )
        port_security = False
    guards = (
        ["dl_src=%s,nw_src=%s," % (guest_mac, source) for source in allowed]
        if port_security
        else [""]
    )

    def _close_port_security() -> None:
        """ARP for what the guest really holds, and nothing else out of it."""
        if not port_security:
            return
        for source in allowed:
            # The guest still ARPs for its gateway, but only as itself.
            lines.append(
                "flow add cookie=%s,table=0,priority=%d,arp,in_port=%s,"
                "dl_src=%s,arp_spa=%s,actions=NORMAL"
                % (cookie, _PS_ARP_PRIORITY, guest_ofport, guest_mac, source)
            )
        # Also keeps what the allow-list cannot express (IPv6) from leaking.
        lines.append(
            "flow add cookie=%s,table=0,priority=%d,in_port=%s,actions=drop"
            % (cookie, _PS_DROP_PRIORITY, guest_ofport)
        )

    if not rules:
        if mark or port_security:
            for guard in guards:
                lines.append(
                    "flow add cookie=%s,table=0,priority=90,%sip,in_port=%s,"
                    "actions=%sNORMAL" % (cookie, guard, guest_ofport, mark)
                )
        _close_port_security()
        sh.run_flow_bundle(lines)
        cut_revoked_sessions(cookie, vlan, rules, guest_ip)
        return

    # --- egress: what the guest may initiate -----------------------------
    for guard in guards:
        lines.append(
            "flow add cookie=%s,table=0,priority=90,%sip,in_port=%s,"
            "ct_state=-trk,actions=%sct(table=%d,zone=%d)"
            % (cookie, guard, guest_ofport, mark, _SG_TABLE, vlan)
        )
    # NORMAL, not resubmit(,0): a packet recirculated into table 0 after
    # ct() is silently dropped.
    lines.append(
        "flow add cookie=%s,table=%d,priority=100,in_port=%s,"
        "ct_state=+est+trk,actions=NORMAL" % (cookie, _SG_TABLE, guest_ofport)
    )
    # Related ICMP errors (notably fragmentation-needed: without it a smaller
    # path MTU hangs connections after the handshake), not ICMP in general.
    lines.append(
        "flow add cookie=%s,table=%d,priority=100,in_port=%s,"
        "ct_state=+rel+trk,actions=NORMAL" % (cookie, _SG_TABLE, guest_ofport)
    )
    for rule in rules:
        if rule_direction(rule) != "egress":
            continue
        match = compile_sg_match(rule)
        if match is None:
            continue
        lines.append(
            "flow add cookie=%s,table=%d,priority=90,in_port=%s,"
            "ct_state=+new+trk,%s,actions=ct(commit,zone=%d),NORMAL"
            % (cookie, _SG_TABLE, guest_ofport, match, vlan)
        )
    lines.append(
        "flow add cookie=%s,table=%d,priority=10,in_port=%s,actions=drop"
        % (cookie, _SG_TABLE, guest_ofport)
    )

    # --- ingress: what may reach the guest -------------------------------
    # in_port = the delivery patch, so a local neighbour's packet stays subject
    # to *its* egress list instead of entering this guest's ingress table.
    entry_match = "ip,dl_dst=%s" % guest_mac
    if delivery_ofport is not None:
        entry_match = "ip,in_port=%s,dl_dst=%s" % (delivery_ofport, guest_mac)
    lines.append(
        "flow add cookie=%s,table=0,priority=91,%s,"
        "ct_state=-trk,actions=ct(table=%d,zone=%d)"
        % (cookie, entry_match, _SG_INGRESS_TABLE, vlan)
    )
    lines.append(
        "flow add cookie=%s,table=%d,priority=100,ip,dl_dst=%s,"
        "ct_state=+est+trk,actions=NORMAL" % (cookie, _SG_INGRESS_TABLE, guest_mac)
    )
    lines.append(
        "flow add cookie=%s,table=%d,priority=100,ip,dl_dst=%s,"
        "ct_state=+rel+trk,actions=NORMAL" % (cookie, _SG_INGRESS_TABLE, guest_mac)
    )
    for rule in rules:
        if rule_direction(rule) != "ingress":
            continue
        match = compile_sg_match(rule)
        if match is None:
            continue
        lines.append(
            "flow add cookie=%s,table=%d,priority=90,%s,dl_dst=%s,"
            "ct_state=+new+trk,actions=ct(commit,zone=%d),NORMAL"
            % (cookie, _SG_INGRESS_TABLE, match, guest_mac, vlan)
        )
    lines.append(
        "flow add cookie=%s,table=%d,priority=10,ip,dl_dst=%s,actions=drop"
        % (cookie, _SG_INGRESS_TABLE, guest_mac)
    )
    _close_port_security()
    sh.run_flow_bundle(lines)
    cut_revoked_sessions(cookie, vlan, rules, guest_ip)
