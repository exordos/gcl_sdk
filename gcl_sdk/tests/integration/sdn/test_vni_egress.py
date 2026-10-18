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

"""Integration: the real per-VNI DVR egress (SNAT namespace, double NAT).

Drives ``_ensure_vni_egress`` end-to-end with real namespaces, veths and
iptables: a guest reaches off-subnet targets through the anycast gateway
leg, the first masquerade rewrites it onto the transfer /30, and the
teardown (``_gc_vni_egress``) removes everything.
"""

import json
import os
import subprocess

from gcl_sdk.agents.universal.drivers.evpn import hostns

# A VNI no real deployment on this host would use (keeps the vni<n>
# namespace and transfer /30 clear of any coexisting stand).
VNI = 16111
CIDR = "10.42.7.0/24"
GW = "10.42.7.1"


def _ns_exists(ns):
    out = subprocess.run(
        ["ip", "netns", "list"], stdout=subprocess.PIPE, text=True
    ).stdout
    return ns in out


def test_guest_egress_through_the_vni_namespace(fabric):
    guest = fabric.add_guest("eg", vlan=31, ip="10.42.7.5/24")
    fabric.apply_egress(guest, VNI, CIDR, GW)

    ns = hostns.vni_ns(VNI)
    assert _ns_exists(ns)

    # The gateway client config for evpn_connector: gateway /32 + default
    # route, the VRF import RT, and a host-unique non-imported export RT.
    conf_path = os.path.join(fabric.client_conf_dir, "gw%d.json" % VNI)
    with open(conf_path) as fl:
        conf = json.load(fl)
    assert conf["routes"] == ["%s/32" % GW, "0.0.0.0/0"]
    assert conf["imp_rt"] == ["65001:%d" % VNI]
    assert conf["exp_rt"] != conf["imp_rt"]

    # Guest -> gateway (the leg answers ARP itself, no proxy needed here).
    assert guest.ping(GW)

    tr_cidr, tr_root, tr_ns = hostns.egress_transfer(VNI)
    # The transfer link is the fabric's plumbing and its root-side address
    # is an address *of this host*: the egress guard drops a guest that aims
    # at it, or the guard could be walked around by aiming one hop short of
    # everything it protects.
    assert not guest.ping(tr_root, count=1, wait=1)

    # Guest -> off-subnet: forwarded by the namespace and masqueraded onto
    # the transfer /30 (the DVR first NAT). Nothing answers a documentation
    # address and nothing needs to — what is being tested is the path out,
    # and conntrack records the translation on the way.
    guest.ping("192.0.2.1", count=1, wait=1)
    # The masquerade happens inside the per-VNI namespace, so its conntrack
    # table carries the translation (guest src -> transfer ns address).
    conntrack = subprocess.run(
        [
            "ip",
            "netns",
            "exec",
            ns,
            "conntrack",
            "-L",
            "-p",
            "icmp",
            "--src",
            "10.42.7.5",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    ).stdout
    assert tr_ns in conntrack, "guest traffic must be masqueraded onto the transfer net"

    # The second (root) masquerade for the transfer net is installed.
    subprocess.run(
        [
            "iptables",
            "-t",
            "nat",
            "-C",
            "POSTROUTING",
            "-s",
            tr_cidr,
            "-j",
            "MASQUERADE",
        ],
        check=True,
    )

    # Idempotent re-apply must not duplicate anything.
    fabric.apply_egress(guest, VNI, CIDR, GW)
    rules = subprocess.run(
        ["ip", "netns", "exec", ns, "iptables", "-t", "nat", "-S"],
        stdout=subprocess.PIPE,
        text=True,
    ).stdout
    assert rules.count("MASQUERADE") == 1


def _transfer_rule(vni, leg, action):
    return subprocess.run(
        [
            "iptables",
            "-t",
            "filter",
            action,
            "FORWARD",
            leg,
            "upl%d" % vni,
            "-m",
            "comment",
            "--comment",
            hostns.TRANSFER_FORWARD_TAG,
            "-j",
            "ACCEPT",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode


def test_an_egress_already_in_place_keeps_its_transfer_forwarding(fabric):
    """The rule belongs to the host, and the host changes underneath it.

    libvirt re-adds a blanket REJECT for its own networks whenever one is
    recreated, so what makes the namespace routable has to be re-asserted
    on every iteration — not once, when the namespace happens to be built.
    Installed inside the build it sat past the early return, so a namespace
    that already existed never got it again; on a hypervisor whose agent
    predated the rule it was never installed at all, and a realm netbooting
    over the overlay looped in iPXE while its boot API answered fine one
    hop away.
    """
    guest = fabric.add_guest("tf", vlan=32, ip="10.42.7.6/24")
    fabric.apply_egress(guest, VNI, CIDR, GW)

    for leg in ("-i", "-o"):
        assert _transfer_rule(VNI, leg, "-D") == 0, "the build must install it"

    # The namespace, its config and its leg are untouched, so this apply
    # takes the early return — which is the whole point.
    fabric.apply_egress(guest, VNI, CIDR, GW)

    for leg in ("-i", "-o"):
        assert _transfer_rule(VNI, leg, "-C") == 0, (
            "an egress already in place must be routable again after a "
            "reconcile, not only when it is first built"
        )


def _rule_order(vni):
    """Where the published grants sit relative to the drops they except."""
    out = subprocess.run(
        ["ip", "netns", "exec", hostns.vni_ns(vni), "iptables-save", "-t", "filter"],
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.splitlines()
    lines = [ln for ln in out if ln.startswith("-A FORWARD ")]
    grants = [i for i, ln in enumerate(lines) if hostns.PUBLISHED_TAG in ln]
    drops = [i for i, ln in enumerate(lines) if hostns.FABRIC_GUARD_TAG in ln]
    return grants, drops


def test_a_published_address_is_reachable_through_the_guard(fabric):
    """What the installation publishes is not what the guard is about.

    The guard blocks by address class because on an installation whose
    services are public that class says "the installation's own insides".
    A floating address does not: it is private by circumstance — one public
    address, terminated on the core — and a published front door by role.
    Left to the class, a realm cannot reach the door of another realm that
    exists to be reached.

    The transfer address stands in for such a door here: it is an address
    the guard demonstrably drops (the test above asserts exactly that) and
    something answers on it, so letting it through is measurable as traffic
    rather than as the presence of a rule.
    """
    guest = fabric.add_guest("pub", vlan=33, ip="10.42.7.7/24")
    fabric.apply_egress(guest, VNI, CIDR, GW)
    _, tr_root, _ = hostns.egress_transfer(VNI)

    try:
        assert not guest.ping(tr_root, count=1, wait=1), (
            "the guard must block it while nothing publishes it"
        )

        hostns.set_published_addresses([tr_root])
        fabric.apply_egress(guest, VNI, CIDR, GW)

        grants, drops = _rule_order(VNI)
        assert grants, "the grant must be installed"
        assert min(grants) < min(drops), (
            "a grant below the drop it excepts is a rule nobody reaches"
        )
        assert guest.ping(tr_root, count=1, wait=1), (
            "a published address must be reachable from the overlay"
        )

        # Naming a port opens that service and nothing else on the host.
        # An address alone would not: a machine answers on every address it
        # holds, so everything bound to 0.0.0.0 — sshd first — would come
        # with it, and giving the service its own address changes nothing.
        hostns.set_published_addresses(["%s:9" % tr_root])
        fabric.apply_egress(guest, VNI, CIDR, GW)

        rules = subprocess.run(
            [
                "ip",
                "netns",
                "exec",
                hostns.vni_ns(VNI),
                "iptables-save",
                "-t",
                "filter",
            ],
            stdout=subprocess.PIPE,
            text=True,
        ).stdout
        assert "--dport 9" in rules, "the port has to reach the rule"
        assert not guest.ping(tr_root, count=1, wait=1), (
            "a grant for one tcp port must not open the address"
        )

        # ...and stops being reachable the moment it stops being published:
        # a door left open outlives the realm that had it.
        hostns.set_published_addresses([])
        fabric.apply_egress(guest, VNI, CIDR, GW)

        grants, _ = _rule_order(VNI)
        assert not grants, "an address no longer published keeps no grant"
        assert not guest.ping(tr_root, count=1, wait=1)
    finally:
        hostns.set_published_addresses([])


def test_egress_gc_removes_everything(fabric):
    """Collecting the egress leaves the VNI's namespace alone — it belongs
    to the VNI, and a subnet that loses its gateway keeps its guests and
    the functions serving them. The namespace goes with the last guest."""
    guest = fabric.add_guest("eg2", vlan=32, ip="10.42.7.6/24")
    fabric.apply_egress(guest, VNI, CIDR, GW)
    ns = hostns.vni_ns(VNI)
    assert _ns_exists(ns)

    hostns.gc_vni_egress(VNI)

    assert _ns_exists(ns)
    assert (
        "gw%d" % VNI
        not in subprocess.run(
            ["ip", "netns", "exec", ns, "ip", "-br", "link"],
            stdout=subprocess.PIPE,
            text=True,
        ).stdout
    )
    assert not os.path.exists(os.path.join(fabric.client_conf_dir, "gw%d.json" % VNI))
    tr_cidr, _, _ = hostns.egress_transfer(VNI)
    rc = subprocess.run(
        [
            "iptables",
            "-t",
            "nat",
            "-C",
            "POSTROUTING",
            "-s",
            tr_cidr,
            "-j",
            "MASQUERADE",
        ],
        stderr=subprocess.DEVNULL,
    ).returncode
    assert rc != 0, "root masquerade for the transfer net must be gone"


def test_the_namespace_goes_with_the_last_guest(fabric):
    guest = fabric.add_guest("eg3", vlan=33, ip="10.42.7.7/24")
    fabric.apply_egress(guest, VNI, CIDR, GW)
    assert _ns_exists(hostns.vni_ns(VNI))

    hostns.gc_vni(VNI)

    assert not _ns_exists(hostns.vni_ns(VNI))


def test_gateway_cycle_preserves_peers_and_updates_mtu(fabric, monkeypatch):
    monkeypatch.setattr(hostns, "_EGRESS_DEMANDS", {})
    guest = fabric.add_guest("egc", vlan=34, ip="10.42.7.8/24")
    fabric.apply_egress(guest, VNI, CIDR, GW, port_uuid="first")
    second = {
        "cidr": "10.43.7.0/24",
        "routers": [{"to": "default", "via": "10.43.7.1"}],
        "mtu": 1400,
    }
    ns = hostns.vni_ns(VNI)

    def gateway_state():
        return json.loads(
            subprocess.check_output(
                [
                    "ip",
                    "netns",
                    "exec",
                    ns,
                    "ip",
                    "-json",
                    "addr",
                    "show",
                    "dev",
                    "gw%d" % VNI,
                ],
                text=True,
            )
        )[0]

    hostns.ensure_vni_egress(VNI, second, ["65001:%d" % VNI], "second")
    installed = gateway_state()
    assert {
        addr["local"] for addr in installed["addr_info"] if addr["family"] == "inet"
    } == {GW, "10.43.7.1"}
    assert installed["mtu"] == 1400
    assert guest.ping(GW)

    hostns.forget_egress_port(VNI, "second")
    installed = gateway_state()
    assert {
        addr["local"] for addr in installed["addr_info"] if addr["family"] == "inet"
    } == {GW}
    assert installed["mtu"] == 1450
    assert guest.ping(GW)

    hostns.forget_egress_port(VNI, "first")
    # The harness moves this leg to br-int for guest pings; return it
    # to the fabric bridge before exercising VNI collection.
    subprocess.check_call(
        [
            "ovs-vsctl",
            "--if-exists",
            "del-port",
            fabric.br,
            hostns.egress_leg(VNI),
            "--",
            "--may-exist",
            "add-port",
            fabric.evpn_br,
            hostns.egress_leg(VNI),
        ]
    )
    hostns.gc_vni(VNI)
    assert VNI not in hostns._EGRESS_DEMANDS
    hostns.ensure_vni_egress(VNI, second, ["65001:%d" % VNI], "second")
    installed = gateway_state()
    assert {
        addr["local"] for addr in installed["addr_info"] if addr["family"] == "inet"
    } == {"10.43.7.1"}


def test_the_namespace_holds_the_default_routes_gateway(fabric):
    """The address the guest is handed is the address the host puts up.

    The guest's gateway comes out of the default route; the namespace used
    to come out of whichever route was written first. A subnet with a
    specific route ahead of its default therefore told the guest one
    address and built the other — and the guest's traffic left for an
    address nothing on the host held, which looks from inside exactly like
    an overlay with no egress at all.
    """
    vni = VNI + 1
    guest = fabric.add_guest("egd", vlan=32, ip="10.42.9.5/24")
    hostns.ensure_vni_egress(
        vni,
        {
            "cidr": "10.42.9.0/24",
            "routers": [
                {"to": "10.0.0.0/8", "via": "10.42.9.9"},
                {"to": "0.0.0.0/0", "via": "10.42.9.1"},
            ],
            "mtu": 1450,
        },
        ["65001:%d" % vni],
    )
    fabric._egress_vnis.add(vni)

    addrs = subprocess.run(
        ["ip", "netns", "exec", hostns.vni_ns(vni), "ip", "-4", "addr", "show"],
        stdout=subprocess.PIPE,
        text=True,
    ).stdout
    assert "10.42.9.1/24" in addrs, addrs
    assert "10.42.9.9" not in addrs, addrs
    assert guest is not None
