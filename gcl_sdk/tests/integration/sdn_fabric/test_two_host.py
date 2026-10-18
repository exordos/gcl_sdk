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

"""Integration: real cross-host EVPN forwarding + fail-static, two hosts.

Two hosts, one RR, all real components. A guest on host A reaches a guest
on host B over VXLAN (routes reflected by the RR), and — the point of the
two-host tier — the data plane keeps forwarding after the RR is killed
with a BGP NOTIFICATION, which is only meaningful because host B's route
is a *remote*, reflected route that a non-fail-static connector would
flush.
"""

import time

from gcl_sdk.tests.integration.sdn import harness as nf_harness

VNI = 17000
CIDR = "10.170.0.0/24"


def _dhcp():
    # No gateway on purpose: cross-host guest<->guest is pure connector L3
    # (per-guest ARP proxy + /32 route over VXLAN), so no egress is needed.
    # Egress is per-VNI and its OVS/netns names (gw/upl/snat<vni>) would
    # collide between the two "hosts" sharing this one kernel — it is proven
    # by the single-host tier instead. Same-subnet guests reach each other
    # over their connected /24 route; no default route required.
    return {
        "cidr": CIDR,
        "routers": [],
        "dns_servers": [],
        "mtu": 1400,
    }


def _reachable(fab, guest, target, timeout=40):
    """Whether the guest can reach the target, given time to converge."""
    reached = nf_harness.wait_until(
        lambda: fab.ping(guest, target, count=2, wait=1), bool, timeout
    )
    return reached is not None


def test_cross_host_forwarding_and_fail_static(two_host_fabric):
    fab = two_host_fabric
    ha, hb = fab.hosts

    ga = fab.add_guest(ha, "ga", "10.170.0.5", VNI, _dhcp())
    gb = fab.add_guest(hb, "gb", "10.170.0.6", VNI, _dhcp())

    # Both host gobgpds peer the RR.
    fab.wait_rr_peers(2)

    # Each host learns the OTHER host's guest as a remote, reflected route.
    fab.wait_host_rib(ha, ["10.170.0.6"])
    fab.wait_host_rib(hb, ["10.170.0.5"])

    # Cross-host data plane: guest on host A <-> guest on host B over VXLAN.
    fab.wait_forwarding(ga, "10.170.0.6")
    fab.wait_forwarding(gb, "10.170.0.5")

    # Fail-static: kill the RR the way systemctl stop does (NOTIFICATION).
    # The remote routes must survive on both connectors (last-known-good).
    fab.wait_fail_static_ready(ga, "10.170.0.6")
    fab.kill_rr()
    time.sleep(12)
    assert fab.ping(ga, "10.170.0.6"), "cross-host must survive RR loss (A->B)"
    assert fab.ping(gb, "10.170.0.5"), "cross-host must survive RR loss (B->A)"


def test_a_guest_that_is_deleted_stops_being_reachable(two_host_fabric):
    """The other half of fail-static, and the half that has teeth.

    The case above is the fabric deciding that a control plane it cannot
    reach means "keep forwarding": routes are held rather than flushed,
    because a reflector dying must not take every tenant's traffic with it.
    That behaviour is only safe if the fabric can still tell "the reflector
    is gone" from "the guest is gone" -- and the second one has to reach the
    far host, or a fabric that never forgets anything is exactly as wrong as
    one that forgets everything.

    What a stale /32 costs is not unreachability, which somebody notices. It
    is that the address stays pointed at a VTEP where nothing holds it any
    more, and the day it is handed to a guest somewhere else, the traffic
    keeps going to the old host. So the withdrawal is read on the far host's
    own RIB and then in what its guest can reach.

    The guest's namespace is deliberately left in place: it is the *port*
    that is deleted, so a ping that stops has to stop because the fabric
    forgot the route and not because there was nobody left to answer.

    Host B keeps a second guest throughout, which is what makes the "and
    nothing else" half measurable at all. It also keeps the host importing
    the VNI's routes: a host whose last guest of a segment leaves has no
    business holding that segment's routes any more, so without a survivor
    there would be nothing left on B to compare against.
    """
    fab = two_host_fabric
    ha, hb = fab.hosts

    ga = fab.add_guest(ha, "wa", "10.170.0.7", VNI, _dhcp())
    doomed = fab.add_guest(hb, "wb", "10.170.0.8", VNI, _dhcp())
    survivor = fab.add_guest(hb, "wc", "10.170.0.9", VNI, _dhcp())

    fab.wait_rr_peers(2)
    fab.wait_host_rib(ha, ["10.170.0.8", "10.170.0.9"])
    fab.wait_host_rib(hb, ["10.170.0.7"])
    # A route in the RIB is not yet a flow on the bridge: the connector
    # compiles what it learns on its own schedule, so the starting state is
    # waited for rather than asserted at the first opportunity. Only the
    # starting state -- what has to *stop* working below is asserted at once,
    # since waiting for traffic to fail proves nothing about when it did.
    assert _reachable(fab, ga, "10.170.0.8"), (
        "the two guests must talk before this means anything"
    )
    assert _reachable(fab, ga, "10.170.0.9"), (
        "the survivor has to be reachable to begin with"
    )

    fab.delete_guest(doomed)

    # The far host is told, not merely this one: the route is withdrawn
    # through the reflector, which is the only path by which host A could
    # ever learn that a guest of host B is gone.
    fab.wait_host_rib_without(ha, ["10.170.0.8"])
    assert not fab.ping(ga, "10.170.0.8", count=2, wait=2), (
        "host A still forwards to a guest that was deleted on host B: the "
        "address stays pointed at that VTEP, and whoever is given it next "
        "inherits traffic meant for somebody else"
    )

    # ...and it reached exactly one port. A withdrawal that took the whole
    # host's segment with it would satisfy everything above and cost every
    # other tenant on that hypervisor their connectivity.
    assert "10.170.0.9" in fab.host_rib(ha), (
        "deleting one guest withdrew its neighbour's route as well"
    )
    assert _reachable(fab, ga, "10.170.0.9"), (
        "deleting one guest of host B cost the others their reachability"
    )
    assert survivor in hb.guests
