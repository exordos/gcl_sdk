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

"""Integration: the real security-group conntrack pipeline enforces on
real traffic between two guests (same VLAN, NORMAL forwarding).

Uses the exact ``flows.ensure_security_group`` flow installer — the same
code the agent runs — so this also covers the reconcile path (reapplying
rules clears the old cookie set first) and teardown.
"""

import subprocess
import time
from unittest import mock

import pytest


@pytest.fixture
def pair(fabric):
    """Two guests in one VLAN + a TCP listener on the second one."""
    a = fabric.add_guest("sga", vlan=5, ip="10.0.5.1/24", mac="52:54:00:05:00:01")
    b = fabric.add_guest("sgb", vlan=5, ip="10.0.5.2/24", mac="52:54:00:05:00:02")
    b.popen(
        "python3",
        "-c",
        "import socket\n"
        "s = socket.socket()\n"
        "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        's.bind(("0.0.0.0", 8080))\n'
        "s.listen(16)\n"
        "while True:\n"
        "    c, _ = s.accept()\n"
        "    c.close()\n",
    )
    time.sleep(0.5)
    return a, b


def test_default_open_without_rules(pair):
    a, b = pair
    assert a.ping("10.0.5.2")
    assert a.tcp_connect("10.0.5.2", 8080)


def test_deny_all_sentinel_blocks_both_directions_and_rules_open_selectively(
    fabric, pair
):
    a, b = pair
    deny_all = [{"direction": "egress", "proto": "none"}]
    fabric.apply_security_group(a, deny_all)
    assert not a.ping("10.0.5.2")
    assert not b.ping("10.0.5.1")
    assert not a.tcp_connect("10.0.5.2", 8080)

    fabric.apply_security_group(a, [{"direction": "ingress", "proto": "icmp"}])
    assert b.ping("10.0.5.1")
    assert not a.tcp_connect("10.0.5.2", 8080)

    fabric.apply_security_group(a, deny_all)
    assert not b.ping("10.0.5.1")
    fabric.apply_security_group(a, [])
    assert a.tcp_connect("10.0.5.2", 8080)


def test_allowlist_enforced_and_reconciled(fabric, pair):
    a, b = pair
    # allow only ICMP -> ping passes, TCP dropped
    fabric.apply_security_group(a, [{"proto": "icmp"}])
    assert a.ping("10.0.5.2")
    assert not a.tcp_connect("10.0.5.2", 8080)

    # reconcile: add tcp/8080 -> both pass; other ports still dropped
    fabric.apply_security_group(a, [{"proto": "icmp"}, {"proto": "tcp", "port": 8080}])
    assert a.ping("10.0.5.2")
    assert a.tcp_connect("10.0.5.2", 8080)
    assert not a.tcp_connect("10.0.5.2", 9999, wait=2)

    # the SG is scoped to guest A: guest B's own traffic is unfiltered.
    # (A's group is default-deny inbound too, so let A accept ICMP for this
    # probe — otherwise it would be measuring A's ingress, not B's egress.)
    fabric.apply_security_group(
        a,
        [
            {"proto": "icmp"},
            {"proto": "tcp", "port": 8080},
            {"direction": "ingress", "proto": "icmp"},
        ],
    )
    assert b.ping("10.0.5.1")

    # teardown: empty rules -> allow-all again
    fabric.apply_security_group(a, [])
    assert a.tcp_connect("10.0.5.2", 8080)


def test_established_return_traffic_allowed(fabric, pair):
    # B initiates towards A while A's egress SG allows nothing but icmp:
    # A's replies are established traffic and must pass the SG
    a, b = pair
    a.popen(
        "python3",
        "-c",
        "import socket\n"
        "s = socket.socket()\n"
        "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        's.bind(("0.0.0.0", 9090))\n'
        "s.listen(16)\n"
        "while True:\n"
        "    c, _ = s.accept()\n"
        "    c.close()\n",
    )
    time.sleep(0.5)
    # A permits that connection inbound, but its egress allow-list has only
    # ICMP: the SYN-ACK and the rest of A's side of the session pass solely
    # because they are established.
    fabric.apply_security_group(
        a,
        [
            {"proto": "icmp"},
            {"direction": "ingress", "proto": "tcp", "port": 9090},
        ],
    )
    assert b.tcp_connect("10.0.5.1", 9090)


def test_reconcile_never_drops_a_packet(fabric, pair):
    """Regression for the del-flows-then-N-separate-add-flow window in
    ``_ensure_security_group``: between the delete and the entry flow
    landing back, a guest's egress matched no SG flow and fell through to
    br-int's default NORMAL action, so reconciling under live traffic
    could glitch. It's now one atomic OpenFlow bundle transaction (see
    ``_run_flow_bundle``), so a flood of pings kept running across many
    back-to-back reconciles of the same guest must show zero loss.
    """
    a, b = pair
    fabric.apply_security_group(a, [{"proto": "icmp"}])

    flood = subprocess.Popen(
        ["ip", "netns", "exec", a.ns, "ping", "-i", "0.02", "-c", "200", "10.0.5.2"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        for i in range(20):
            # Alternates rule sets so every reconcile actually rewrites the
            # cookie group (not a no-op re-add of identical flows).
            rules = [{"proto": "icmp"}]
            if i % 2:
                rules.append({"proto": "tcp", "port": 8080})
            fabric.apply_security_group(a, rules)
        out, _ = flood.communicate(timeout=20)
    finally:
        if flood.poll() is None:
            flood.kill()
            flood.communicate()
    assert "0% packet loss" in out, out


def test_a_group_denies_inbound_that_no_rule_permits(fabric, pair):
    """A security group is an allow-list in both directions: attaching one
    to B blocks A's connections to it even though B may talk out freely."""
    a, b = pair
    fabric.apply_security_group(
        b, [{"direction": "egress", "proto": "any"}], cookie="0x51c000000000bb"
    )
    assert not a.tcp_connect("10.0.5.2", 8080, wait=2)
    assert not a.ping("10.0.5.2")

    # permitting exactly that connection inbound lets it through, and only it
    fabric.apply_security_group(
        b,
        [
            {"direction": "egress", "proto": "any"},
            {"direction": "ingress", "proto": "tcp", "port": 8080},
        ],
        cookie="0x51c000000000bb",
    )
    assert a.tcp_connect("10.0.5.2", 8080)
    assert not a.ping("10.0.5.2")
    fabric.clear_security_group(cookie="0x51c000000000bb")


def test_an_ingress_only_group_still_denies_egress(fabric, pair):
    """The direction a ruleset does not mention is denied, not ignored."""
    a, b = pair
    fabric.apply_security_group(a, [{"direction": "ingress", "proto": "icmp"}])
    assert not a.ping("10.0.5.2")
    assert not a.tcp_connect("10.0.5.2", 8080, wait=2)


def test_flows_removed_by_hand_are_reported_missing(fabric, pair):
    """The point of reconciliation: what the host actually holds is what is
    compared. Wipe a guest's filter off the bridge and the driver must say
    so — reading the meta file back would call it healthy and nothing would
    ever reinstall it."""
    from gcl_sdk.agents.universal.drivers.evpn import state

    a, _ = pair
    cookie = "0x51c00000000ddd"
    fabric.apply_security_group(a, [{"proto": "icmp"}], cookie=cookie)
    assert not a.tcp_connect("10.0.5.2", 8080, wait=2)

    with mock.patch.object(state, "_DP_SNAPSHOT", state.snapshot_dataplane()):
        assert state.has_cookie(cookie), "the filter is installed"

    subprocess.run(
        ["ovs-ofctl", "del-flows", fabric.br, "cookie=%s/-1" % cookie],
        check=True,
    )
    with mock.patch.object(state, "_DP_SNAPSHOT", state.snapshot_dataplane()):
        assert not state.has_cookie(cookie), "and its absence is observable"

    # the guest is unfiltered again — which is exactly why the driver has to
    # notice and reinstall rather than trust its own bookkeeping
    assert a.tcp_connect("10.0.5.2", 8080)
    fabric.apply_security_group(a, [{"proto": "icmp"}], cookie=cookie)
    assert not a.tcp_connect("10.0.5.2", 8080, wait=2)
    fabric.clear_security_group(cookie=cookie)


def test_a_group_that_keeps_being_rewritten_never_lets_a_packet_through(fabric, pair):
    """The other half of "reconcile never drops a packet".

    That case says an allow-list under churn must not lose traffic it
    permits. This one says the same churn must not, for one packet, admit
    traffic it forbids -- and the two are not the same promise. A rewrite
    that dropped the allow flows for an instant loses a ping and is caught
    above; a rewrite that dropped the *drop* flow for an instant delivers a
    packet nobody allowed, and nothing above would notice, because the
    guest it reaches has no reason to complain.

    Neutron keeps this one as `test_preventing_firewall_blink` and asserts
    both halves at once: packets were sent, and none arrived. Asserting only
    the second would pass just as well against a flood that never started.

    The group stays attached throughout. Detaching it is not churn but a
    decision -- a port with no group is open here, which the first case in
    this file states -- so a test that detached and reattached would be
    measuring the gap it opened itself.
    """
    a, b = pair
    # b admits one TCP port and nothing else, so a's pings are forbidden
    # for the whole life of this test.
    fabric.apply_security_group(b, [{"proto": "tcp", "port": 8080}])

    flood = subprocess.Popen(
        ["ip", "netns", "exec", a.ns, "ping", "-i", "0.02", "-c", "200", "10.0.5.2"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        for i in range(20):
            # The rule set changes on every pass, so every apply really
            # rewrites the cookie's flows rather than re-adding identical
            # ones. What never changes is that the group is attached and
            # that nothing in it permits ICMP.
            fabric.apply_security_group(
                b,
                [
                    {"proto": "tcp", "port": 8080},
                    {"proto": "tcp", "port": 9000 + i},
                ],
            )
        out, _ = flood.communicate(timeout=20)
    finally:
        if flood.poll() is None:
            flood.kill()
            flood.communicate()

    assert "200 packets transmitted" in out, "the flood never ran: %s" % out
    assert "100% packet loss" in out, "the rewrite let a forbidden packet in: %s" % out
