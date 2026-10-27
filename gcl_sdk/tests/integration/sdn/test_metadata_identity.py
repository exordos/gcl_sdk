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

"""Who the metadata address thinks is asking.

Everything a machine is told about itself comes through one address that
every guest of every tenant reaches identically, and the only thing
separating one machine's answers from another's is the proxy's judgement
about who is on the other end of the socket. That judgement is made from
the source address, resolved against the records of the VNI the request
was punted into.

The gate itself is thoroughly unit-tested -- a path naming another machine
is refused, a uuid hidden in a nested segment does not count as asking
about yourself, an unidentifiable caller gets nothing. What none of that
can reach is the step before it: *which* records were consulted. A gate
that judges perfectly against the wrong VNI's records is a gate that hands
every guest somebody else's machine, and it passes every unit test there
is.

So this asks the question where it can actually go wrong: two guests, in
two segments, holding the **same address**, each recorded as a different
machine. Overlapping tenant addressing is the whole premise of a private
overlay, which makes "resolve the caller by address" ambiguous by
construction -- and the ambiguity is resolved, or not, by which directory
the responder reads.

What is read back is the gate's verdict rather than a body: a refusal is
403, while a request that passes is relayed upstream -- and the upstream
here is deliberately somewhere nothing listens, so passing shows up as a
502. That distinction is the whole measurement, and it keeps the case
about the gate rather than about a relay this tier has no route for.
"""

import uuid as sys_uuid

import pytest


CIDR = "10.77.0.0/24"
SHARED_IP = "10.77.0.9"  # held in both segments, which is the point
# Somewhere nothing listens: a request the gate lets through fails at the
# relay, and failing at the relay is how "let through" is observed.
UPSTREAM = "http://127.0.0.1:1"
# The platform granted at the root, which is how the metadata listener has
# it: that is what makes a path naming a machine machine-*scoped*. Granting
# `/v1/boots` as a subtree instead would be a published artifact prefix --
# static, identical for every guest, and deliberately not judged, since
# there is nothing in it that belongs to anybody. Getting this wrong is how
# the first draft of this file measured nothing while looking right.
FORWARDS = ("/=%s" % UPSTREAM,)


def _seat(fabric, guest, vni):
    """Record this guest as a machine of its own, at the shared address."""
    node = str(sys_uuid.uuid4())
    fabric.apply_metadata(guest, CIDR, vni=vni, forwards=FORWARDS)
    # apply_metadata writes the record the responder needs; the machine it
    # belongs to is what makes the caller identifiable, so it is written
    # again with that added (same path, so this replaces rather than adds).
    fabric.write_record(
        guest.name,
        guest.mac,
        guest.ipv4,
        vni,
        cidr=CIDR,
        dhcp_enabled=False,
        dns_enabled=False,
        node=node,
    )
    return node


@pytest.fixture
def neighbours(fabric):
    """Two machines at one address, one in each segment."""
    fabric.start_nf()
    first = fabric.add_guest(
        "m1", vlan=25, ip=SHARED_IP + "/24", mac="52:54:00:77:00:01"
    )
    second = fabric.add_guest(
        "m2", vlan=26, ip=SHARED_IP + "/24", mac="52:54:00:77:00:02"
    )
    first_node = _seat(fabric, first, fabric.vni_of(first))
    second_node = _seat(fabric, second, fabric.vni_of(second))
    return (first, first_node), (second, second_node)


def _ask(guest, node):
    status, _ = guest.http_get("http://169.254.169.254/v1/boots/%s" % node)
    return status


def test_a_guest_is_told_about_itself(neighbours):
    """The control, and the half that has to work for the other to mean
    anything.

    Asking about its own machine must reach the gate's far side. Nothing is
    listening there, so what comes back is the relay failing -- which is the
    point: it is not a 403, and only the gate produces a 403.
    """
    (guest, node), _ = neighbours

    status = _ask(guest, node)

    assert status != 403, (
        "a guest was refused its own machine, so the metadata path is closed "
        "to everyone and the refusal in the next case means nothing"
    )
    assert status == 502, (
        "expected the relay to be attempted and fail (nothing listens "
        "upstream); got %s, which is neither the gate refusing nor the relay "
        "trying" % status
    )


def test_a_guest_is_not_told_about_its_neighbour(neighbours):
    """The gate, over the real punt path, between two real guests."""
    (guest, _node), (_other, other_node) = neighbours

    assert _ask(guest, other_node) == 403, (
        "a guest was handed another machine's boot record: everything the "
        "platform tells a machine about itself is available to whoever asks "
        "for it by name"
    )


def test_the_same_address_in_the_other_segment_is_a_different_machine(neighbours):
    """The step no unit test can reach: which records were consulted.

    Both guests answer to the same address, so if the responder resolved a
    caller against a host-wide view -- or against the first VNI that
    happened to hold that address -- each would be judged as the other. Then
    one of the two cases above would pass by accident and the other would
    fail for a reason that looks like a gate bug.

    So it is asked from the other side as well: the second guest must be
    told about *its* machine and refused the first's, with every address in
    the request identical to the one before.
    """
    (_first, first_node), (second, second_node) = neighbours

    assert _ask(second, second_node) == 502, (
        "the second segment's guest was not recognised as its own machine, "
        "so callers are being resolved against something other than their "
        "own segment's records"
    )
    assert _ask(second, first_node) == 403, (
        "the second segment's guest was handed the first's machine: two "
        "tenants at one address are one machine to this proxy"
    )
