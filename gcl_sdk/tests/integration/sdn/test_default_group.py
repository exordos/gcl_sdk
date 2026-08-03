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

"""Integration: the shape every port now arrives with.

The control plane seeds each subnet a default group — everything out, and
in only from the subnet itself — and gives it to every port that names no
group of its own. A default that silently broke a guest's own DHCP,
resolver or netboot would be worse than no default at all, so this drives
the exact rule set the seed produces and checks the platform's own
services still reach the guest, that a neighbour on the subnet does, and
that the filter is genuinely on.
"""

import shutil
import uuid as sys_uuid

import pytest

from gcl_sdk.agents.universal.drivers.evpn import base

CIDR = "10.42.0.0/24"
GW = "10.42.0.254"
GW_MAC = "12:34:56:78:90:ab"

# The subnet's own group, as the control plane allocates one.
DEFAULT_IDENTITY = 1

# What `_default_group_rules` seeds, in the shape the agent receives it: in
# from the subnet's group by *membership*, which is what travels on the wire
# (`remote_group` is resolved to this number before it leaves the core).
DEFAULT_RULES = [
    {"direction": "egress", "proto": "any"},
    {"direction": "ingress", "proto": "any", "remote_identity": DEFAULT_IDENTITY},
]


@pytest.mark.skipif(not shutil.which("dhclient"), reason="dhclient not installed")
def test_the_default_group_leaves_the_platforms_services_reachable(fabric):
    """DHCP is taken above the filter, so a default-deny group cannot cost a
    guest the lease it needs before it can do anything at all."""
    mac = "52:54:00:d0:f0:01"
    fabric.write_record(
        str(sys_uuid.uuid4()),
        mac,
        "10.42.0.41",
        vni=16041,
        cidr=CIDR,
        mtu=1450,
        routers=[{"to": "0.0.0.0/0", "via": GW}],
    )
    fabric.start_nf()
    guest = fabric.add_guest("dg1", vlan=41, mac=mac, nfs=("dhcp", "dns"))
    fabric.apply_security_group(guest, DEFAULT_RULES, identity=DEFAULT_IDENTITY)

    assert "bound to 10.42.0.41" in guest.dhclient(wait=8)


@pytest.mark.skipif(not shutil.which("dig"), reason="dig not installed")
def test_the_default_group_leaves_the_resolver_reachable(fabric):
    mac = "52:54:00:d0:f0:02"
    fabric.write_record(
        str(sys_uuid.uuid4()),
        mac,
        "10.42.0.42",
        vni=16042,
        cidr=CIDR,
        name="behind-default",
    )
    fabric.start_nf()
    guest = fabric.add_guest(
        "dg2", vlan=42, ip="10.42.0.42/24", mac=mac, nfs=("dhcp", "dns")
    )
    guest.exec("ip", "neigh", "replace", GW, "lladdr", GW_MAC, "dev", guest.dev)
    fabric.apply_security_group(guest, DEFAULT_RULES, identity=DEFAULT_IDENTITY)

    answer = guest.exec(
        "dig",
        "+time=2",
        "+tries=1",
        "+short",
        "@" + GW,
        "behind-default.internal",
        check=False,
        timeout=12,
    ).strip()
    assert answer == "10.42.0.42"


def test_the_default_group_leaves_netboot_reachable_over_the_routed_leg(fabric):
    """The answer does not have to come back the way the question went.

    A guest with no disk runs iPXE, which asks the address DHCP gave it — the
    subnet's gateway — on the boot API's port. That address lives on the
    VNI's routed leg, so the proxy answers *from* it and the reply reaches
    the guest over the fabric, not over the port the request was stolen into.
    Only the metadata address has a rule that sends its answers back over
    that port (`ensure_guest_neighbour`), so this leg is the one the default
    group used to drop — a realm node netbooted for ever and nothing said
    why.
    """
    mac = "52:54:00:d0:f0:05"
    vni = 16045
    fabric.write_record(
        str(sys_uuid.uuid4()),
        mac,
        "10.42.0.45",
        vni=vni,
        cidr=CIDR,
        mtu=1450,
        routers=[{"to": "0.0.0.0/0", "via": GW}],
        dhcp_enabled=False,
        dns_enabled=False,
    )
    fabric.apply_nf_config(vni, "proxy", forwards=[])
    fabric.start_nf()
    guest = fabric.add_guest(
        "dg5", vlan=45, ip="10.42.0.45/24", mac=mac, nfs=("proxy",), vni=vni
    )
    # The gateway leg: what holds the address the guest asks, and what its
    # answers come back over.
    fabric.apply_egress(guest, vni, CIDR, GW)
    fabric.apply_security_group(
        guest,
        DEFAULT_RULES,
        identity=DEFAULT_IDENTITY,
        delivery_ofport=fabric.ofport_of("gw%d-br" % vni),
    )

    boot_api = base.PLATFORM_PORTS[-1]
    assert fabric.wait_for_listener(vni, boot_api), "the proxy never bound the port"

    assert guest.tcp_connect(GW, boot_api, wait=5), (
        "the boot API's answer must reach a guest behind the default group"
    )


def test_a_default_shaped_group_admits_its_subnet_and_nothing_else(fabric):
    """In from its own subnet, and the filter is really on: a neighbour
    reaches it, and a source outside the range does not.

    Stated as an address, not as the membership the seed actually uses: this
    tier is one flat bridge, so a sender's port and the port its frames are
    delivered over are the same one, and the receiver's ingress entry takes
    a frame before its sender's egress flow can stamp it (the reason is in
    ``test_identity_group``, which owns that half). What is being pinned
    here is the *shape* — an allow-list with one way in — on real traffic.
    """
    subnet_rules = [
        {"direction": "egress", "proto": "any"},
        {"direction": "ingress", "proto": "any", "remote": CIDR},
    ]
    mac_a = "52:54:00:d0:f0:03"
    mac_b = "52:54:00:d0:f0:04"
    fabric.start_nf()
    guest = fabric.add_guest("dg3", vlan=43, ip="10.42.0.43/24", mac=mac_a)
    neighbour = fabric.add_guest("dg4", vlan=43, ip="10.42.0.44/24", mac=mac_b)
    fabric.apply_security_group(guest, subnet_rules, delivery_from=neighbour)

    assert neighbour.ping("10.42.0.43"), "a neighbour on the subnet must get in"

    # ... the guest may start anything outward (the egress half)...
    assert guest.ping("10.42.0.44")

    # ... and a source the group does not name is dropped, even when it sits
    # on the same segment: the rule is the subnet's range, not "whoever is
    # nearby". The source is forced, or the kernel would pick the address
    # that matches the destination and prove nothing.
    neighbour.exec("ip", "addr", "add", "10.99.0.7/24", "dev", neighbour.dev)
    out = neighbour.exec(
        "ping",
        "-c",
        "1",
        "-W",
        "1",
        "-I",
        "10.99.0.7",
        "10.42.0.43",
        check=False,
        timeout=10,
    )
    assert "1 received" not in out, out
