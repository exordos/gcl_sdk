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

"""Integration: reaching a guest of an overlay from the segment outside it.

The case this exists for: the installation has one public address, on the
node that runs its load balancer, and the hypervisors are separate boxes
sharing one L2 segment with it. A realm's guest lives on an overlay whose
addressing every other realm reuses, so there is nothing to route to —
what makes it reachable is a floating address of the *segment*, unique per
guest, that its hypervisor answers for and translates inward.

Two namespaces stand in for two boxes on that segment: one plays the node
asking (the load balancer's), one is this host, which is the hypervisor.
The floating address is configured on neither — it exists only as the NAT
the driver programs — so unless the hypervisor answers ARP for it, the
asking node cannot even build a frame.
"""

import subprocess
import time
import uuid as sys_uuid

import pytest

from gcl_sdk.agents.universal.drivers.evpn import hostns
from gcl_sdk.tests.integration.sdn import harness

VNI = 16222
CIDR = "10.42.6.0/24"
GW = "10.42.6.1"
GUEST_IP = "10.42.6.5"
# The stand-in management segment and the floating address out of it. Both
# are unroutable documentation space so a real interface of the test host
# can never be on the same segment and answer first.
SEGMENT = "198.51.100.0/24"
PEER_IP = "198.51.100.7"
HOST_IP = "198.51.100.8"
FIP = "198.51.100.50"


_run = harness._run


class Segment:
    """A shared L2 between this host and one other box on it."""

    def __init__(self, tag):
        self.ns = "fipseg-%s" % tag
        self.host_if = "fipseg-h%s" % tag
        self.peer_if = "fipseg-p%s" % tag

    def build(self):
        _run(["ip", "netns", "add", self.ns])
        _run(
            [
                "ip",
                "link",
                "add",
                self.host_if,
                "type",
                "veth",
                "peer",
                "name",
                self.peer_if,
            ]
        )
        _run(["ip", "link", "set", self.peer_if, "netns", self.ns])
        _run(["ip", "addr", "add", "%s/24" % HOST_IP, "dev", self.host_if])
        _run(["ip", "link", "set", self.host_if, "up"])
        _run(
            ["ip", "-n", self.ns, "addr", "add", "%s/24" % PEER_IP, "dev", self.peer_if]
        )
        _run(["ip", "-n", self.ns, "link", "set", self.peer_if, "up"])
        _run(["ip", "-n", self.ns, "link", "set", "lo", "up"])

    def asks(self, ip, count=2, wait=2):
        """True when the other box can reach an address over the segment."""
        res = subprocess.run(
            [
                "ip",
                "netns",
                "exec",
                self.ns,
                "ping",
                "-c",
                str(count),
                "-W",
                str(wait),
                ip,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return res.returncode == 0

    def neighbour(self, ip):
        """What the other box learned about an address, as one line."""
        _run(["ip", "-n", self.ns, "neigh", "flush", "all"], check=False)
        self.asks(ip, count=1, wait=1)
        return _run(["ip", "-n", self.ns, "neigh", "show", ip], check=False).strip()

    def destroy(self):
        _run(["ip", "netns", "del", self.ns], check=False)
        _run(["ip", "link", "del", self.host_if], check=False)


@pytest.fixture
def segment(requires_sdn_env):
    seg = Segment(sys_uuid.uuid4().hex[:6])
    seg.build()
    try:
        yield seg
    finally:
        seg.destroy()


@pytest.fixture
def guest_with_fip(fabric, segment):
    """A guest of an overlay, with a floating address of the segment."""
    guest = fabric.add_guest("fip", vlan=41, ip="%s/24" % GUEST_IP)
    # The agent asserts the guard once per VNI per *iteration* and forgets
    # between them; a test process has no iteration boundary, so a test that
    # tore this VNI's egress down would leave the next one's namespace
    # unguarded and quietly measuring nothing. Forgetting here is what the
    # start of an iteration does.
    hostns._GUARDED_VNIS.discard(VNI)
    fabric.apply_egress(guest, VNI, CIDR, GW)
    tag = hostns.fip_tag(sys_uuid.uuid4())
    hostns.ensure_fip_nat(GUEST_IP, [{"public": FIP}], tag, VNI)
    try:
        yield guest, tag
    finally:
        hostns.delete_fip_rules(tag, VNI)


def test_the_hypervisor_answers_for_the_floating_address(guest_with_fip, segment):
    """Without an answer the asking box cannot build a frame at all.

    The floating address is on no interface of this host — it is a NAT rule
    inside a namespace — so the segment's ARP goes unanswered and the
    neighbour entry never leaves INCOMPLETE. That is not a routing problem
    and no route fixes it.
    """
    _guest, _tag = guest_with_fip
    assert hostns.proxied_fips().get(FIP) == segment.host_if
    assert "INCOMPLETE" not in segment.neighbour(FIP)


def test_the_segment_reaches_a_guest_of_the_overlay(guest_with_fip, segment):
    """End to end: the other box pings an address the guest never heard of.

    The guest holds 10.42.6.5 on an overlay every other realm reuses; the
    box on the segment addresses 198.51.100.50 and the hypervisor makes
    them the same conversation.
    """
    _guest, _tag = guest_with_fip
    assert segment.asks(FIP), _run(["ip", "neigh", "show", "proxy"], check=False)


def test_the_guard_refuses_what_the_guest_starts_and_not_the_way_in(
    guest_with_fip, segment
):
    """Both halves at once, because they live in the same chain.

    A floating address is translated inside the VNI's namespace, so the way
    in arrives at the guest with an address of the overlay — inside the
    private space the fabric guard blocks. Written as a destination alone,
    the guard therefore refused the ingress the address exists for, and the
    only trace was a counter on `daddr 10.0.0.0/8`. Scoped to the leg the
    guests are on, it refuses what a guest *starts* toward the installation
    and nothing about what is delivered to it.
    """
    guest, _tag = guest_with_fip

    assert segment.asks(FIP), "the way in is not the guard's to refuse"
    assert not guest.ping(HOST_IP, count=1, wait=1), (
        "what the guest starts toward the segment still is"
    )


def test_the_answer_goes_with_the_address(guest_with_fip, segment):
    """A hypervisor that no longer serves the guest must stop answering.

    Left behind, it keeps telling the segment to send this address here —
    and after the guest has moved, here is where it stops. The teardown is
    what makes moving a guest between boxes possible at all.
    """
    _guest, tag = guest_with_fip
    hostns.delete_fip_rules(tag, VNI)
    assert FIP not in hostns.proxied_fips()
    assert not segment.asks(FIP, count=1, wait=1)


def test_an_answer_orphaned_by_a_gone_namespace_is_swept(guest_with_fip, segment):
    """The rules are read out of the namespace, so a gone one hides them.

    The host route has this covered already — swept by the transfer next
    hop, which outlives the namespace — and the answer has to be swept with
    it. Otherwise the segment is told to send the address to a host with
    nothing left to deliver it to, which is worse than telling it nothing.
    """
    _guest, tag = guest_with_fip
    hostns.gc_vni_egress(VNI)
    hostns.delete_fip_rules(tag, VNI)
    assert FIP not in hostns.proxied_fips()


def test_the_segment_is_told_when_the_address_arrives(guest_with_fip, segment):
    """A floating address moves when its guest does, and everything being
    right at that moment is not enough: whoever was talking to it has the
    old host's MAC cached and keeps sending there until it ages out.

    Here the other box is made to believe the address is somewhere else,
    and the announcement has to correct it without being asked.
    """
    _guest, _tag = guest_with_fip
    _run(
        [
            "ip",
            "-n",
            segment.ns,
            "neigh",
            "replace",
            FIP,
            "lladdr",
            "02:00:00:00:de:ad",
            "dev",
            segment.peer_if,
            "nud",
            "stale",
        ]
    )
    stale = _run(["ip", "-n", segment.ns, "neigh", "show", FIP], check=False)
    assert "02:00:00:00:de:ad" in stale, stale

    hostns.announce_fip(FIP)
    time.sleep(1)

    now = _run(["ip", "-n", segment.ns, "neigh", "show", FIP], check=False)
    assert "02:00:00:00:de:ad" not in now, now


def test_announcing_an_address_of_no_segment_does_nothing(guest_with_fip):
    """Nothing to announce it on, and nothing to raise about it either."""
    hostns.announce_fip("203.0.113.201")


def test_a_floating_address_of_no_segment_is_not_answered_for(guest_with_fip):
    """Answering is for addresses on a segment this host is on.

    One routed to the host by somebody upstream has already been decided
    for, and there is no interface on which the question is even asked. It
    also must not be reported as missing, or every iteration would report
    an answer the apply then declines to add.
    """
    assert hostns.fip_link("203.0.113.200") is None
