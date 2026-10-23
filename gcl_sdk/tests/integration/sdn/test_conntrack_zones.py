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

"""One tenant's open connection must not be another tenant's way in.

The pipeline is stateful: what a guest started may come back, because the
tables admit `ct_state=+est+trk` outright. That is what makes ordinary
clients work, and it is written in terms of the connection's five-tuple --
so on a host where two tenants hold the same addresses, two different
conversations can have the *same* five-tuple, and "this is established"
stops being a statement about one of them.

Which is why every guest's rules compile with a conntrack `zone`. Nothing
had ever measured it. The neighbouring module proves the segments cannot
reach each other, which is the L2 half; this is the half where nothing
crosses the wire at all -- a tenant opens a permitted connection, and the
question is whether that fact, sitting in a table on the hypervisor, admits
an identical-looking connection that nobody permitted.

It is worth stating what an unzoned host would look like, because it would
not look broken: segment A's traffic never reaches segment B, no packet is
misdelivered, and the only symptom is that a port B never opened accepts a
connection -- but only while a guest of A happens to be talking. A firewall
that depends on somebody else's timing is the kind of hole that survives
every test that does not name it.

Not a supposition: compiling both segments' rules into one shared zone and
running the case below gets the knock *accepted*, and putting the zones
back gets it refused. So this file is worth its runtime -- the property it
holds up is one line of the flow compiler away from being gone.

OVN keeps per-datapath ct zones for exactly this and asserts them; Neutron
gives every port its own zone and asserts that two ports with overlapping
addresses do not share state.
"""

import time

import pytest


ECHO_PORT = 8099
CLOSED_PORT = 8100
SOURCE_PORT = 5555

# Both segments hold these, which is the whole point.
CLIENT_IP = "10.88.0.1"
SERVER_IP = "10.88.0.2"

LISTENER = (
    "import socket\n"
    "s = socket.socket()\n"
    "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
    's.bind(("0.0.0.0", %d))\n'
    "s.listen(16)\n"
    "while True:\n"
    "    c, _ = s.accept()\n"
    "    c.sendall(b'hi')\n"
    "    c.close()\n" % ECHO_PORT
)


def _holder(target):
    """Open a connection from a fixed source port and keep it open."""
    return (
        "import socket, time\n"
        "s = socket.socket()\n"
        "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "s.settimeout(5)\n"
        's.bind(("0.0.0.0", %d))\n'
        "s.connect((%r, %d))\n"
        "s.recv(8)\n"
        "time.sleep(30)\n" % (SOURCE_PORT, target, ECHO_PORT)
    )


def _knock(target):
    """Try the identical five-tuple from the other segment, and report."""
    return (
        "import socket, sys\n"
        "s = socket.socket()\n"
        "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "s.settimeout(3)\n"
        's.bind(("0.0.0.0", %d))\n'
        "try:\n"
        "    s.connect((%r, %d))\n"
        "except Exception as exc:\n"
        "    print('refused')\n"
        "    sys.exit(0)\n"
        "print('accepted')\n"
        "sys.exit(1)\n" % (SOURCE_PORT, target, ECHO_PORT)
    )


@pytest.fixture
def two_tenants(fabric):
    """The same addresses in two segments, one of which permits the port.

    Segment A's server allows the port its client is about to use. Segment
    B's server allows a different one, so the identical connection arriving
    there is permitted by nothing -- except, if the zones were shared, by
    the fact that A's guest is mid-conversation.
    """
    a_client = fabric.add_guest(
        "za", vlan=45, ip=CLIENT_IP + "/24", mac="52:54:00:88:45:01"
    )
    a_server = fabric.add_guest(
        "zb", vlan=45, ip=SERVER_IP + "/24", mac="52:54:00:88:45:02"
    )
    b_client = fabric.add_guest(
        "zc", vlan=46, ip=CLIENT_IP + "/24", mac="52:54:00:88:46:01"
    )
    b_server = fabric.add_guest(
        "zd", vlan=46, ip=SERVER_IP + "/24", mac="52:54:00:88:46:02"
    )
    for server in (a_server, b_server):
        server.popen("python3", "-c", LISTENER)
    time.sleep(0.6)

    fabric.apply_security_group(
        a_server,
        [{"direction": "ingress", "proto": "tcp", "port": ECHO_PORT}],
        cookie="0x2000000000aa01",
        delivery_from=a_client,
    )
    fabric.apply_security_group(
        b_server,
        [{"direction": "ingress", "proto": "tcp", "port": CLOSED_PORT}],
        cookie="0x2000000000bb01",
        delivery_from=b_client,
    )
    return a_client, a_server, b_client, b_server


def test_an_open_session_of_one_tenant_does_not_admit_another(two_tenants):
    """The identical five-tuple, one permitted and one not, at the same time.

    Segment A's client holds a permitted connection open. Segment B's client
    then makes the same connection, byte for byte -- same source address,
    same source port, same destination, same destination port -- to a server
    that permits no such thing. It has to be refused, and the refusal must
    not depend on A having finished.
    """
    a_client, _a_server, b_client, _b_server = two_tenants

    a_client.popen("python3", "-c", _holder(SERVER_IP))
    time.sleep(1.5)

    knocked = b_client.exec("python3", "-c", _knock(SERVER_IP), check=False)

    assert "refused" in knocked, (
        "a connection nobody permitted was accepted while an identical one "
        "was open in another segment: the two tenants share conntrack state, "
        "so one tenant's traffic decides when another tenant's firewall has "
        "a hole (%r)" % knocked.strip()
    )


def test_the_permitted_session_really_is_open(two_tenants):
    """The control: A's connection has to exist for the case above to mean
    anything.

    If A never connected, the refusal in B would prove only that B's rules
    work -- which is already asserted elsewhere -- and nothing at all about
    zones.
    """
    a_client, _a_server, _b_client, _b_server = two_tenants

    opened = a_client.exec("python3", "-c", _knock(SERVER_IP), check=False)

    assert "accepted" in opened, (
        "the segment that permits the port refused it, so there is no open "
        "session for the other segment to have inherited: %r" % opened.strip()
    )


def test_the_second_segment_admits_what_it_permits(fabric, two_tenants):
    """The other control, from B's side.

    "B refused it" is also what a guest with no working path produces. So
    the same client, to the same server, on the port B *does* permit, has to
    get through -- otherwise the case above is measuring a dead segment.
    """
    _a_client, _a_server, b_client, b_server = two_tenants
    fabric.apply_security_group(
        b_server,
        [{"direction": "ingress", "proto": "tcp", "port": ECHO_PORT}],
        cookie="0x2000000000bb01",
        delivery_from=b_client,
    )

    assert b_client.tcp_connect(SERVER_IP, ECHO_PORT), (
        "the second segment cannot reach its own server even when its rules "
        "allow it, so its refusal above said nothing about conntrack zones"
    )
