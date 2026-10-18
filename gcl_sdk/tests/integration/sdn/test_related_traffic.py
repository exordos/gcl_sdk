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

"""The errors a permitted conversation generates are part of it.

A conntrack pipeline knows three things about a packet: it is new, it
belongs to a tracked connection, or it is an *error about* one -- ICMP
destination-unreachable, port-unreachable, fragmentation-needed. The third
is what `+rel` names, and it is easy to leave out, because everything looks
fine without it until the day something needs to say "that did not work".

Fragmentation-needed is the one that matters most: a guest whose path MTU
drops somewhere and who never hears about it does not get an error, it gets
a connection that completes its handshake, carries small packets, and then
hangs forever on the first large one. Nothing in the allow-list looks wrong
while that happens.

Neutron keeps `test_related_connection` for exactly this, and OVN has
`ACL all drop and allow related` plus a dedicated ICMP-unreachable case.
Both admit related traffic alongside established.
"""

import time

import pytest


CLOSED_PORT = 9999


def _probe(target):
    """Send a datagram to a closed port and report what came back.

    A datagram socket only surfaces the ICMP error on the *next* operation:
    the first send goes out, the error comes back, and the second send is
    what raises. Hence two sends with a pause between them.
    """
    return (
        "import socket, sys, time\n"
        "s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        "s.settimeout(3)\n"
        "s.connect((%r, %d))\n"
        "s.send(b'hello')\n"
        "time.sleep(0.4)\n"
        "try:\n"
        "    s.send(b'hello')\n"
        "    s.recv(64)\n"
        "except ConnectionRefusedError:\n"
        "    print('refused')\n"
        "    sys.exit(0)\n"
        "except socket.timeout:\n"
        "    print('silence')\n"
        "    sys.exit(1)\n"
        "print('answered')\n"
        "sys.exit(2)\n" % (target, CLOSED_PORT)
    )


@pytest.fixture
def pair(fabric):
    """Two guests, and an allow-list on the sender that permits UDP only."""
    sender = fabric.add_guest("r1", vlan=9, ip="10.0.9.1/24", mac="52:54:00:09:00:01")
    peer = fabric.add_guest("r2", vlan=9, ip="10.0.9.2/24", mac="52:54:00:09:00:02")
    time.sleep(0.3)
    # UDP out, and nothing about ICMP in either direction: the error the
    # peer's kernel is about to send is admissible only as related traffic.
    fabric.apply_security_group(sender, [{"proto": "udp", "port": CLOSED_PORT}])
    return sender, peer


def test_an_icmp_error_about_a_permitted_flow_reaches_its_sender(pair):
    """The guest sends UDP to a closed port and must be told so."""
    sender, _ = pair

    out = sender.exec("python3", "-c", _probe("10.0.9.2"), check=False)

    assert "refused" in out, (
        "the guest never learned its datagram was refused (%r). The pipeline "
        "admits +est but not +rel, so an ICMP error about a permitted flow "
        "is dropped -- and the same gap silently breaks path MTU discovery, "
        "where the error is the only thing that would tell a sender to use "
        "smaller packets." % out.strip()
    )
