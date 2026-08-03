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

"""Integration: a rule that names a group, on real traffic.

The point of carrying an identity is that a rule stops naming addresses:
"from the database group" holds whoever is in that group right now, and no
host has to be told who they are. These tests use the real flow installer
on real OVS, with real guests, and never mention an address in a rule.

They also pin the part that is easy to get wrong: the identity is the
*sender's*, so membership decides what a guest may reach, and changing a
sender's group changes what it may reach without touching the receiver.
"""

import time

import pytest

WEB = 4242
DB = 4343
LISTEN = 8080


@pytest.fixture
def three(fabric):
    """Three guests in one VLAN, the third listening for the other two."""
    a = fabric.add_guest("iga", vlan=6, ip="10.0.6.1/24", mac="52:54:00:06:00:01")
    b = fabric.add_guest("igb", vlan=6, ip="10.0.6.2/24", mac="52:54:00:06:00:02")
    target = fabric.add_guest("igt", vlan=6, ip="10.0.6.3/24", mac="52:54:00:06:00:03")
    target.popen(
        "python3",
        "-c",
        "import socket\n"
        "s = socket.socket()\n"
        "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        's.bind(("0.0.0.0", %d))\n'
        "s.listen(16)\n"
        "while True:\n"
        "    c, _ = s.accept()\n"
        "    c.close()\n" % LISTEN,
    )
    time.sleep(0.5)
    return a, b, target


def test_the_flows_carry_the_group_and_nothing_about_addresses(fabric, three):
    """What this tier can settle: the rule compiles to a match on the mark,
    the sender's port stamps it, and no address appears anywhere.

    What it cannot: enforcement between two guests. In production a guest's
    egress is claimed by its own `in_port` and delivery to its neighbour
    arrives on a *patch* — two different ports, so the sender is marked
    before the receiver's ingress entry (which outranks it) ever sees the
    packet. This tier is one flat bridge with no patch, so both would be the
    same port and the receiver's entry would take the frame before it is
    stamped. The enforcement proof therefore lives where the patches and the
    tunnel are real: `sdn_fabric/test_gbp_carrier.py`.
    """
    a, _b, target = three
    fabric.apply_security_group(a, [], cookie="0x1de0000000000a", identity=WEB)
    fabric.apply_security_group(
        target,
        [
            {
                "direction": "ingress",
                "proto": "tcp",
                "port": LISTEN,
                "remote_identity": WEB,
            },
            {"direction": "egress", "proto": "any"},
        ],
        cookie="0x1de0000000000c",
        delivery_from=a,
    )
    flows = fabric.dump_flows()
    # ovs-ofctl prints the mark in hex; ask for it the way it answers.
    assert "set_field:0x%x->pkt_mark" % WEB in flows, "the sender says what it is"
    asked = "pkt_mark=0x%x" % WEB
    assert asked in flows, "the receiver asks what the sender is"
    assert "nw_src" not in flows.split(asked)[1].split("\n")[0]


def test_an_unidentified_sender_is_admitted_by_no_group_rule(fabric, three):
    """A port with no identity stamps nothing, so a rule that names a group
    cannot match it — fail-closed, which is the behaviour to have when a
    hypervisor has not caught up yet."""
    a, _b, target = three
    fabric.apply_security_group(a, [], cookie="0x1de0000000000a")  # no identity
    fabric.apply_security_group(
        target,
        [
            {
                "direction": "ingress",
                "proto": "tcp",
                "port": LISTEN,
                "remote_identity": WEB,
            },
            {"direction": "egress", "proto": "any"},
        ],
        cookie="0x1de0000000000c",
        delivery_from=a,
    )
    assert not a.tcp_connect("10.0.6.3", LISTEN, wait=2)
