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

"""What a rule being taken away reaches.

An allow-list that only governs new connections is a door that locks behind
whoever is already inside. The pipeline is stateful on purpose -- return
traffic passes because it is established, which is what makes ordinary
clients work -- and the same mechanism is what would let a revoked
permission go on being honoured indefinitely by a connection that was
opened while it held.

So a revocation reaches the sessions it was holding open: when a guest's
allow-list changes, its conntrack zone is flushed, and a connection that
outlived its rule stops. That is Neutron's semantics rather than Calico's,
and it was a decision, not an accident -- a revocation that governs only
what has not started yet is exactly the case nobody is worried about when
they take a rule away.

The cut is the guest's whole zone, so a still-permitted session reconnects
on an unrelated edit too. That cost is priced in: what a connection was
admitted *by* is recorded nowhere, so "which sessions did this rule hold
open" has no answer to read.
"""

import os
import subprocess
import time

import pytest

from gcl_sdk.tests.integration.sdn import harness

ECHO_PORT = 8090
OTHER_PORT = 8091


def _listener(port):
    return LISTENER_TEMPLATE % port


LISTENER_TEMPLATE = (
    "import socket\n"
    "s = socket.socket()\n"
    "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
    's.bind(("0.0.0.0", %d))\n'
    "s.listen(16)\n"
    "while True:\n"
    "    c, _ = s.accept()\n"
    "    while True:\n"
    "        data = c.recv(16)\n"
    "        if not data:\n"
    "            break\n"
    "        c.sendall(data)\n"
)


def _sender(path, target, port=ECHO_PORT):
    """Hold one connection and report, line by line, whether it still works.

    Writing to a socket whose traffic is being dropped does not fail -- the
    bytes sit in a buffer and the sender is none the wiser -- so the witness
    has to be the *reply*. Each line of the log is one round trip that
    completed, and the first one that does not says so and stops.
    """
    return (
        "import socket, time\n"
        "log = open(%r, 'w', buffering=1)\n"
        "s = socket.socket()\n"
        "s.settimeout(3)\n"
        "s.connect((%r, %d))\n"
        "for i in range(200):\n"
        "    try:\n"
        "        s.sendall(b'x')\n"
        "        if not s.recv(16):\n"
        "            raise OSError('closed')\n"
        "        log.write('ok\\n')\n"
        "    except Exception as exc:\n"
        "        log.write('cut %%s\\n' %% exc)\n"
        "        break\n"
        "    time.sleep(0.1)\n" % (path, target, port)
    )


def _round_trips(path):
    try:
        with open(path) as log:
            return log.read().splitlines()
    except OSError:
        return []


def _wait_for(path, predicate, timeout=10):
    lines = harness.wait_until(
        lambda: _round_trips(path), predicate, timeout, interval=0.2
    )
    return _round_trips(path) if lines is None else lines


@pytest.fixture
def talkers(fabric, tmp_path):
    """A client, an echo server, and the group that lets them talk."""
    client = fabric.add_guest("c1", vlan=7, ip="10.0.7.1/24", mac="52:54:00:07:00:01")
    server = fabric.add_guest("c2", vlan=7, ip="10.0.7.2/24", mac="52:54:00:07:00:02")
    server.popen("python3", "-c", _listener(ECHO_PORT))
    server.popen("python3", "-c", _listener(OTHER_PORT))
    time.sleep(0.5)
    fabric.apply_security_group(
        server,
        [
            {"direction": "ingress", "proto": "tcp", "port": ECHO_PORT},
            {"direction": "ingress", "proto": "tcp", "port": OTHER_PORT},
        ],
    )
    return client, server, os.path.join(str(tmp_path), "session.log")


def test_a_session_is_cut_when_its_rule_is_revoked(fabric, talkers):
    """The half that has to be measured, because it is measured as loss.

    A live session opened through a rule must stop when the rule goes. It
    is asserted on the *replies*: writing into a socket whose traffic is
    being dropped does not fail -- the bytes sit in a buffer and the sender
    is none the wiser -- so each line of the log is one round trip that
    completed, and the first one that does not is the cut.
    """
    client, server, log_path = talkers

    client.popen("python3", "-c", _sender(log_path, "10.0.7.2"))
    lines = _wait_for(log_path, lambda ls: len(ls) >= 3)
    assert lines and lines[0] == "ok", "the session never worked: %s" % lines
    survived = len(lines)

    # The permission is withdrawn -- a rule revoked, not the group detached,
    # since with no group at all the port would be open and prove nothing.
    fabric.apply_security_group(
        server, [{"direction": "ingress", "proto": "tcp", "port": OTHER_PORT}]
    )

    lines = _wait_for(log_path, lambda ls: any(ln.startswith("cut") for ln in ls))
    assert any(ln.startswith("cut") for ln in lines), (
        "the session went on working after its rule was revoked: a "
        "permission that has been taken away is still being honoured by "
        "whatever was already open (%s)" % lines[-3:]
    )
    assert survived >= 1, "nothing was ever carried, so nothing was cut"


def _zone_entries(zone, port):
    """The conntrack entries of one zone for one destination port."""
    out = subprocess.run(
        ["conntrack", "-L", "--zone", str(zone)],
        capture_output=True,
        text=True,
    ).stdout
    return {
        line.split("sport=")[1].split()[0]
        for line in out.splitlines()
        if "dport=%d " % port in line + " "
    }


def test_only_what_stopped_being_permitted_is_cut(fabric, talkers):
    """A revocation reaches what it withdrew and stops there.

    Asserted on the conntrack table rather than on traffic, and that is the
    whole point of the case: flushing an entry does not by itself end a
    permitted conversation — the next packet makes a new entry and the
    pipeline lets it through again — so a guest whose entire zone was
    flushed *looks* fine from the outside. What the blunt version really
    costs is every flow of that guest re-evaluated and its NAT state
    rebuilt, and on a host where conntrack does not accept mid-stream
    packets it costs the connections themselves. So what is measured here
    is which entries the flush actually took.
    """
    client, server, log_path = talkers
    other_log = log_path + ".other"
    zone = server.vlan

    client.popen("python3", "-c", _sender(log_path, "10.0.7.2", ECHO_PORT))
    client.popen("python3", "-c", _sender(other_log, "10.0.7.2", OTHER_PORT))
    assert _wait_for(log_path, lambda ls: len(ls) >= 3)[0] == "ok"
    assert _wait_for(other_log, lambda ls: len(ls) >= 3)[0] == "ok"

    revoked_before = _zone_entries(zone, ECHO_PORT)
    kept_before = _zone_entries(zone, OTHER_PORT)
    assert revoked_before and kept_before, (
        "both sessions must be tracked before the revocation, or this "
        "measures nothing: %s / %s" % (revoked_before, kept_before)
    )

    fabric.apply_security_group(
        server, [{"direction": "ingress", "proto": "tcp", "port": OTHER_PORT}]
    )

    kept_after = _zone_entries(zone, OTHER_PORT)
    assert kept_before & kept_after, (
        "the entry of a session nobody revoked was flushed as well: taking "
        "one rule away re-evaluates everything the guest was doing"
    )
    assert not (revoked_before & _zone_entries(zone, ECHO_PORT)), (
        "the entry the revoked rule was holding open survived"
    )


def test_a_new_connection_is_refused_once_the_rule_is_gone(fabric, talkers):
    """The half that is not in question.

    Whatever established traffic is allowed to do, a *new* connection after
    the revocation must be refused -- otherwise the rule was never enforced
    at all and the case above would be measuring nothing.
    """
    client, server, _ = talkers

    assert client.tcp_connect("10.0.7.2", ECHO_PORT), "the rule was not in force"

    fabric.apply_security_group(
        server, [{"direction": "ingress", "proto": "tcp", "port": ECHO_PORT + 1}]
    )

    assert not client.tcp_connect("10.0.7.2", ECHO_PORT), (
        "a new connection was accepted after its rule was taken away"
    )


def test_established_buys_the_return_leg_and_not_a_second_way_in(fabric, talkers):
    """What statefulness is for, and what it is not.

    A client's replies pass because its connection is established. That must
    not extend to a new connection from the same guest to a port nobody
    allowed -- otherwise one permitted session would open the host.
    """
    client, server, log_path = talkers

    client.popen("python3", "-c", _sender(log_path, "10.0.7.2"))
    lines = _wait_for(log_path, lambda ls: len(ls) >= 3)
    assert lines and lines[0] == "ok", "the session never worked: %s" % lines

    assert not client.tcp_connect("10.0.7.2", ECHO_PORT + 5), (
        "an established session on one port opened another"
    )
