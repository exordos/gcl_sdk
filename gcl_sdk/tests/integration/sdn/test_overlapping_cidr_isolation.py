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

"""Two tenants may hold the same address, and must not meet.

The point of a private overlay is that a tenant's addressing is their own,
which is worth nothing unless two tenants can choose the same one. So this
puts the identical address in two segments on one host and asks the ordinary
question a guest asks -- who is 10.99.0.6? -- and requires the answer to come
from the asker's own segment, not from whoever answered first.

That is a stronger claim than "the ping worked". A leak between segments with
disjoint addressing shows up as unreachability, which any test notices; a
leak between segments with the *same* addressing shows up as a reply from
the wrong guest, and the packet flow looks perfectly healthy while a tenant
talks to a stranger. So the assertion is on the MAC behind the address, not
on whether the ping came back.

ovn-kubernetes keeps this as `"isolates overlapping CIDRs"` in its network
segmentation suite. OVN itself does not test it -- the isolation is
structural there, in the tunnel key -- which is exactly the reasoning that
makes it worth checking here, where our structure is a VLAN tag on a shared
bridge and a mistake in it is invisible.
"""

import subprocess

import pytest


ADDRESS = "10.99.0.6"
MAC_HOME = "52:54:00:99:00:16"
MAC_STRANGER = "52:54:00:99:00:26"


def _neigh_mac(guest, address):
    """The MAC the guest ended up with for an address, as its kernel has it."""
    out = subprocess.run(
        ["ip", "netns", "exec", guest.ns, "ip", "neigh", "show", address],
        capture_output=True,
        text=True,
    ).stdout
    for token in out.split():
        if ":" in token and len(token) == 17:
            return token
    return None


@pytest.fixture
def two_segments(fabric):
    """The same address in two segments, and a neighbour for one of them.

    VLAN 31 is one tenant's segment and 32 is another's; the harness tags a
    guest's port the way the driver tags it for its VNI, so this is the
    separation the data plane actually has on a shared bridge.
    """
    asker = fabric.add_guest("h1", vlan=31, ip="10.99.0.5/24", mac="52:54:00:99:00:15")
    home = fabric.add_guest("h2", vlan=31, ip=ADDRESS + "/24", mac=MAC_HOME)
    stranger = fabric.add_guest("s2", vlan=32, ip=ADDRESS + "/24", mac=MAC_STRANGER)
    # Somebody to ask the same question from the other side, so "nobody
    # answered" and "the right one answered" can be told apart.
    other = fabric.add_guest("s1", vlan=32, ip="10.99.0.7/24", mac="52:54:00:99:00:27")
    return asker, home, stranger, other


def test_the_same_address_in_two_segments_answers_from_its_own(two_segments):
    """The question a guest asks every day, with two possible answers."""
    asker, home, stranger, _ = two_segments

    assert asker.ping(ADDRESS), "the guest could not reach its own neighbour"

    learned = _neigh_mac(asker, ADDRESS)
    assert learned == MAC_HOME, (
        "%s answered for %s: a guest of another segment replied to an ARP "
        "that was never theirs" % (learned, ADDRESS)
    )
    assert learned != MAC_STRANGER


def test_a_guest_cannot_reach_an_address_its_segment_does_not_hold(two_segments):
    """The other direction, where the address exists but not here.

    10.99.0.5 is a real guest on the first segment. From the second one it
    must be nothing at all -- not a slow reply, not an intermittent one.
    """
    _, _, stranger, _ = two_segments

    assert not stranger.ping("10.99.0.5"), (
        "a guest reached an address that only exists in another tenant's segment"
    )


def test_the_other_segment_answers_for_the_same_address_with_its_own_holder(
    two_segments,
):
    """The control that keeps the case above honest.

    "The stranger did not answer" is also what a dead port looks like, and
    a suite full of that passes while proving nothing. So the same question
    is asked from inside the second segment, where the same address is held
    by the other guest, and there the other guest must be the one to answer.
    """
    _, _, stranger, other = two_segments

    assert other.ping(ADDRESS), "the second segment's holder is not answering at all"
    assert _neigh_mac(other, ADDRESS) == MAC_STRANGER
