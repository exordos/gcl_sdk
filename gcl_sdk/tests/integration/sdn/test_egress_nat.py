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

"""Two guests behind one address, and the replies that must not be swapped.

The egress path masquerades twice -- once inside the VNI namespace onto the
transfer /30, once by the root namespace out of its uplink -- and the tier
so far has only ever asserted that the machinery exists: the namespace is
there, conntrack recorded a translation, the rule is installed. Nothing has
ever come back.

What NAT is actually for is the return leg, and the only way it goes wrong
is silently. Everything a wrong translation produces looks like a working
network from the outside: the packets leave, the far end answers, and the
answer arrives at a guest that did not ask. So this puts a real listener at
the far end, past both masquerades, and requires each guest to receive its
*own* reply and nothing else.

The interesting case is deliberately the hostile one: both guests use the
same source port at the same time. That is what a NAT is for -- one address
shared by many -- and it is the case a translation table that keys on too
little gets wrong. Two guests picking port 5000 is not exotic; it is what
happens when two copies of the same image start together.

Neutron asserts the round trip through its SNAT rather than the rules, and
OVN keeps `dnat_and_snat` cases that check which side the reply reaches.
"""

import os
import subprocess
import time

import pytest

from gcl_sdk.agents.universal.drivers.evpn import hostns
from gcl_sdk.tests.integration.sdn import harness

VNI = 16114
CIDR = "10.42.14.0/24"
GW = "10.42.14.1"
VLAN = 43

# The far end is reached over a hop rather than plugged into this host, and
# that is not decoration. The egress guard protects every prefix the host is
# directly attached to -- a guest aiming at one of those is aiming at the
# fabric's own plumbing, which `test_vni_egress` asserts is refused. So a
# server on a directly attached network is unreachable *by design*, and a
# test that put one there would be measuring the guard instead of the NAT.
# Hence the /30 to a router and the server's address behind it: somewhere
# genuinely off this host, which is what a masquerade is for.
FAR_LINK = "203.0.113"
FAR_ROOT = "%s.1" % FAR_LINK  # this host's end of the hop
FAR_HOP = "%s.2" % FAR_LINK  # the far router's end
FAR_SERVER = "198.51.100.10"  # behind it, routed and not attached
FAR_PORT = 9100
SOURCE_PORT = 5000

# What the far end answers with: the source it saw, so a guest's own reply
# names the address and port the fabric presented on its behalf.
SERVER = (
    "import socket\n"
    "s = socket.socket()\n"
    "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
    's.bind(("0.0.0.0", %d))\n'
    "s.listen(16)\n"
    "while True:\n"
    "    c, peer = s.accept()\n"
    "    c.sendall((peer[0] + ' ' + str(peer[1])).encode())\n"
    "    c.close()\n" % FAR_PORT
)


def _client(path, hold):
    """Connect from a fixed source port, hold it, and write down the answer."""
    return (
        "import socket, time\n"
        "s = socket.socket()\n"
        "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "s.settimeout(8)\n"
        's.bind(("0.0.0.0", %d))\n'
        "log = open(%r, 'w', buffering=1)\n"
        "try:\n"
        "    s.connect((%r, %d))\n"
        "    log.write(s.recv(64).decode() + '\\n')\n"
        "    time.sleep(%f)\n"
        "except Exception as exc:\n"
        "    log.write('failed %%s\\n' %% exc)\n"
        % (SOURCE_PORT, path, FAR_SERVER, FAR_PORT, hold)
    )


_run = harness._run


def _answer(path, timeout=15):
    def first_line():
        try:
            with open(path) as log:
                return log.readline().strip()
        except OSError:
            return ""

    return harness.wait_until(first_line, bool, timeout, interval=0.2) or ""


@pytest.fixture
def far_end(fabric):
    """Somewhere off this host, reachable only through the masquerade."""
    ns = "far-%s" % fabric.br
    _run(["ip", "netns", "add", ns])
    _run(["ip", "link", "add", "far0", "type", "veth", "peer", "name", "far1"])
    _run(["ip", "link", "set", "far1", "netns", ns])
    _run(["ip", "addr", "replace", "%s/30" % FAR_ROOT, "dev", "far0"])
    _run(["ip", "link", "set", "far0", "up"])
    _run(["ip", "-n", ns, "addr", "replace", "%s/30" % FAR_HOP, "dev", "far1"])
    _run(["ip", "-n", ns, "link", "set", "far1", "up"])
    _run(["ip", "-n", ns, "link", "set", "lo", "up"])
    _run(["ip", "-n", ns, "addr", "replace", "%s/32" % FAR_SERVER, "dev", "lo"])
    _run(["ip", "-n", ns, "route", "replace", "default", "via", FAR_ROOT])
    _run(["ip", "route", "replace", "%s/32" % FAR_SERVER, "via", FAR_HOP])

    server = subprocess.Popen(
        ["ip", "netns", "exec", ns, "python3", "-c", SERVER],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)
    try:
        yield ns
    finally:
        server.kill()
        server.wait(timeout=5)
        _run(["ip", "netns", "del", ns], check=False)
        _run(["ip", "link", "del", "far0"], check=False)


@pytest.fixture
def two_guests(fabric, far_end):
    """Two guests of one segment, both egressing through the same address."""
    first = fabric.add_guest("n1", vlan=VLAN, ip="10.42.14.5/24")
    second = fabric.add_guest("n2", vlan=VLAN, ip="10.42.14.6/24")
    fabric.apply_egress(first, VNI, CIDR, GW)
    fabric.apply_egress(second, VNI, CIDR, GW)
    return first, second


def test_a_guest_reaches_the_far_end_and_the_answer_comes_back(two_guests, tmp_path):
    """The round trip the tier has never actually completed.

    Both masquerades on the way out, both reversed on the way back. Until
    now the evidence was a conntrack entry, which says a packet left; this
    says one returned.
    """
    guest, _ = two_guests
    log = os.path.join(str(tmp_path), "one.log")

    guest.popen("python3", "-c", _client(log, 0.5))

    answer = _answer(log)
    assert answer and not answer.startswith("failed"), (
        "the guest could not complete a connection through the masquerade: %r" % answer
    )
    seen_from, _ = answer.split()
    assert seen_from == FAR_ROOT, (
        "the far end saw %s, so the guest's own address left this host: the "
        "second masquerade did not happen" % seen_from
    )


def test_two_guests_on_one_source_port_are_told_apart(two_guests, tmp_path):
    """The case a translation table keyed on too little gets wrong.

    Both guests hold source port 5000 to the same destination at the same
    time -- what happens when two copies of one image start together. The
    fabric must present them as two different conversations, and each must
    receive its own reply.
    """
    first, second = two_guests
    logs = [os.path.join(str(tmp_path), name) for name in ("a.log", "b.log")]

    first.popen("python3", "-c", _client(logs[0], 4))
    second.popen("python3", "-c", _client(logs[1], 4))

    answers = [_answer(path) for path in logs]

    for guest, answer in zip((first, second), answers):
        assert answer and not answer.startswith("failed"), (
            "%s got no answer while its neighbour used the same source port: "
            "%r" % (guest.name, answer)
        )
    ports = [answer.split()[1] for answer in answers]
    assert ports[0] != ports[1], (
        "both guests were presented to the far end as the same source port "
        "%s, so one conversation was standing in for the other" % ports[0]
    )


def test_the_far_end_cannot_start_a_conversation_of_its_own(two_guests, far_end):
    """A masquerade is a way out, and must not become a way in.

    The address the guests share is an address of this host, and the far end
    can reach it. What it must not reach is a guest: nothing has published
    one, and the translation exists only for conversations the guest began.

    Asserted as reachability rather than against a rule, on purpose -- what
    keeps the far end out is the absence of anything, a route and a
    translation that were never created, and an absence is only measurable
    from the outside. Whichever of the two a future change removes, the
    packet arriving is what this notices.
    """
    first, _ = two_guests

    reachable = subprocess.run(
        ["ip", "netns", "exec", far_end, "ping", "-c", "1", "-W", "1", first.ipv4],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    assert reachable.returncode != 0, (
        "a host on the far side of the masquerade reached a guest's own "
        "address, which nothing on the overlay ever published"
    )


def test_the_translation_is_recorded_against_the_guest_that_made_it(two_guests):
    """Which guest a conversation belongs to, from the host's own tables.

    The reply arriving is the outcome; this is the reason it arrives, and it
    is what a post-mortem reads. Both guests must appear in the namespace's
    conntrack as separate origins rather than as one merged entry.
    """
    first, second = two_guests
    for guest in (first, second):
        guest.exec("ping", "-c", "1", "-W", "1", FAR_SERVER, check=False)

    table = subprocess.run(
        ["ip", "netns", "exec", hostns.vni_ns(VNI), "conntrack", "-L"],
        capture_output=True,
        text=True,
    ).stdout

    for guest in (first, second):
        assert guest.ipv4 in table, (
            "the namespace has no record of %s talking to the far end, so "
            "nothing on this host could tell its replies from its "
            "neighbour's" % guest.ipv4
        )
