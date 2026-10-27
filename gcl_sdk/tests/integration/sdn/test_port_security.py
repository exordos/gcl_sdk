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

"""A guest may only be itself.

A security group answers "what may this guest talk to". Port security
answers the question underneath it -- *which* guest is talking -- and
without it the first answer is worth nothing: a neighbour that can claim
your address inherits every permission granted to you, and a neighbour that
can answer ARP for it collects your traffic without needing any permission
at all. Both attacks come from inside the segment, from a guest that is
supposed to be there, which is exactly the position a tenant's own compute
is in.

The flows for this are unit-tested as text, and until now that was the whole
of it: no packet had ever been sent from an address a guest does not hold.
Flow text is the wrong witness for this particular thing, because every one
of these attacks is a packet that *looks* ordinary -- correct segment,
plausible addresses, nothing malformed -- and what decides it is the
interaction of several flows at different priorities. The only honest reading
is whether the packet arrives.

So each case here is an attack carried out for real by a guest of the
segment, and the control beside them is the same guest behaving, because
"the neighbour heard nothing" is also what a broken port looks like.

OVN keeps a `port_security` suite over exactly this ground, and Neutron
asserts anti-spoofing both ways round.
"""

import subprocess
import time

import pytest


VLAN = 44
VICTIM_IP = "10.42.15.5"
SPOOFER_IP = "10.42.15.6"
ASKER_IP = "10.42.15.7"
VICTIM_MAC = "52:54:00:f0:00:05"
SPOOFER_MAC = "52:54:00:f0:00:06"
ASKER_MAC = "52:54:00:f0:00:07"


# The attack itself, since no tool for it is installed and building the
# frame by hand is the more honest version anyway: an unsolicited ARP reply
# claiming somebody else's address. Linux updates an entry it already holds
# from one of these, so the asker has to have learned the real holder first
# -- which is why the case below pings before it poisons.
POISON = (
    "import socket, struct, sys\n"
    "dev, claimed_ip, own_mac, target_ip, target_mac = sys.argv[1:6]\n"
    'def m(x): return bytes.fromhex(x.replace(":", ""))\n'
    "def i(x): return socket.inet_aton(x)\n"
    "s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)\n"
    "s.bind((dev, 0))\n"
    'eth = m(target_mac) + m(own_mac) + b"\\x08\\x06"\n'
    'arp = (struct.pack("!HHBBH", 1, 0x0800, 6, 4, 2) + m(own_mac) + i(claimed_ip)\n'
    "       + m(target_mac) + i(target_ip))\n"
    "for _ in range(3):\n"
    "    s.send(eth + arp)\n"
)


def _neigh_mac(guest, address):
    """The MAC a guest ended up believing for an address."""
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
def segment(fabric):
    """Three guests of one segment, one of them held to its own address.

    Port security is applied with no rules at all, which is the interesting
    shape: the guest is *unfiltered* as to what it may reach, so anything
    that fails below fails because of who it claimed to be and not because
    of where it was going.
    """
    victim = fabric.add_guest("v", vlan=VLAN, ip=VICTIM_IP + "/24", mac=VICTIM_MAC)
    spoofer = fabric.add_guest("s", vlan=VLAN, ip=SPOOFER_IP + "/24", mac=SPOOFER_MAC)
    asker = fabric.add_guest("a", vlan=VLAN, ip=ASKER_IP + "/24", mac=ASKER_MAC)
    # Long enough for duplicate-address detection to finish on the addresses
    # nobody configured: a guest's IPv6 link-local is tentative for about a
    # second after the link comes up, and traffic from a tentative address
    # does not leave at all. Sleep too little and the IPv6 case below passes
    # against a guest that was never able to send in the first place.
    time.sleep(2)
    fabric.apply_security_group(
        spoofer, [], sources=[SPOOFER_IP + "/32"], port_security=True
    )
    return victim, spoofer, asker


def test_a_guest_held_to_its_address_still_works(segment):
    """The control, and the reason the rest of the file means anything.

    Everything below is measured as silence, and silence is also what a
    guest that was simply cut off produces. So first: pinned to its own
    address and permitted nothing in particular, this guest talks to its
    neighbours exactly as before.
    """
    _, spoofer, asker = segment

    assert spoofer.ping(ASKER_IP), (
        "port security cut the guest off from its own segment while it was "
        "using its own address -- every other case here would now pass for "
        "the wrong reason"
    )


def test_a_guest_cannot_send_from_a_neighbours_address(segment):
    """The attack that inherits somebody else's permissions.

    A security group elsewhere may name this address; a firewall beyond the
    fabric may trust it. The address is the whole of the identity, so
    holding it is the whole of the attack, and the guest needs nothing but
    an `ip addr add` to try.
    """
    _, spoofer, asker = segment
    spoofer.exec("ip", "addr", "add", VICTIM_IP + "/32", "dev", spoofer.dev)

    reached = spoofer.exec(
        "ping", "-c", "2", "-W", "1", "-I", VICTIM_IP, ASKER_IP, check=False
    )

    assert "0 received" in reached or "100% packet loss" in reached, (
        "a guest sent traffic as its neighbour and was answered: everything "
        "the fabric grants to %s is available to whoever asks for it: %s"
        % (VICTIM_IP, reached.strip())
    )


def test_a_guest_cannot_send_from_a_neighbours_mac(segment):
    """The same attack one layer down, where the address is not involved.

    Below IP there is no allow-list to fool -- what a stolen MAC buys is
    delivery: the fabric learns the address on the wrong port and the
    neighbour's traffic follows. So the MAC is checked with the same
    strictness as the address, and this guest keeps its own address while
    wearing somebody else's MAC.
    """
    _, spoofer, asker = segment
    spoofer.exec("ip", "link", "set", spoofer.dev, "address", VICTIM_MAC)
    # The control plane keeps saying what it issued: a guest changing its own
    # MAC does not change the port's.
    time.sleep(0.3)

    assert not spoofer.ping(ASKER_IP, count=2, wait=1), (
        "a guest wearing its neighbour's MAC was carried by the segment, so "
        "the fabric can be made to deliver that neighbour's traffic to it"
    )


def test_a_guest_cannot_answer_for_an_address_it_does_not_hold(segment):
    """The attack that needs no permissions at all -- it collects them.

    Holding the address is only half of it; the other half is answering the
    question "who has it". A guest that replies first becomes the neighbour
    for everyone who asked, and the traffic arrives without any rule
    anywhere being broken. So the asker must end up with the real holder's
    MAC, and this is asserted on the MAC rather than on whether the ping
    worked -- a poisoned neighbour table is a *working* ping to the wrong
    guest, which no reachability check would notice.

    The claim is made by hand rather than by holding the address and racing
    the real owner to answer, because a race is not a measurement: whoever
    replies first wins it, and a test that sometimes catches the attacker is
    a test that sometimes catches nothing. An unsolicited reply is also the
    attack as it is actually carried out.
    """
    victim, spoofer, asker = segment
    # The asker learns the truth first -- Linux updates an entry it holds
    # from an unsolicited reply, and refuses to create one. So this is both
    # the precondition of the attack and the thing being overwritten.
    assert asker.ping(VICTIM_IP), "the real holder is not answering at all"
    assert _neigh_mac(asker, VICTIM_IP) == VICTIM_MAC

    spoofer.exec(
        "python3",
        "-c",
        POISON,
        spoofer.dev,
        VICTIM_IP,
        SPOOFER_MAC,
        ASKER_IP,
        ASKER_MAC,
        check=False,
    )
    time.sleep(1)
    learned = _neigh_mac(asker, VICTIM_IP)

    assert learned == VICTIM_MAC, (
        "%s answered for %s: a guest of this segment collects its "
        "neighbour's traffic by claiming to be it, and every party to it "
        "believes the conversation is ordinary" % (learned, VICTIM_IP)
    )
    assert learned != SPOOFER_MAC


def test_a_family_the_allow_list_cannot_express_does_not_slip_past(segment):
    """What port security has to do about traffic it has no words for.

    The addresses it pins are IPv4, so an IPv6 packet matches none of the
    entries that permit and none that deny by address -- it simply is not
    described. A port that fell through to the bridge's ordinary forwarding
    for anything undescribed would be open to it, and IPv6 is configured on
    every guest without anyone asking, which makes "undescribed" the default
    state of an entire protocol family rather than a corner case.
    """
    _, spoofer, asker = segment
    out = asker.exec("ip", "-6", "addr", "show", asker.dev)
    lladdr = next(
        (t.split("/")[0] for t in out.split() if t.startswith("fe80::")), None
    )
    if not lladdr or "tentative" in out:
        pytest.skip("no settled IPv6 link-local address on the guest")

    reached = spoofer.exec(
        "ping",
        "-6",
        "-c",
        "2",
        "-W",
        "1",
        "%s%%%s" % (lladdr, spoofer.dev),
        check=False,
    )

    assert "0 received" in reached or "100% packet loss" in reached, (
        "a guest whose port is pinned to IPv4 addresses reached its "
        "neighbour over IPv6, which the allow-list never described: %s"
        % reached.strip()
    )
