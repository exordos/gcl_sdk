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

"""Applying what is already applied changes nothing on the host.

The reconcile loop applies a resource whenever the control plane's hash and
the agent's disagree, and a disagreement it cannot settle is a loop that
never ends: the same apply, every iteration, for as long as the resource
exists. The cost is not the wasted work -- it is that every apply is a write
to the data plane, so a guest's flows, patch ports and namespace are torn
down and rebuilt under live traffic for no reason at all.

Nothing in the driver's own result says whether that happened. It reports
success either way, and a converged installation and a spinning one look
identical from the control plane's side except in the log. So the property
is asserted against the host: take the bridge as it is, apply the same
resource again, and require the host to be byte-for-byte what it was.

Every SDN that has been bitten by this checks it: OVN forces
`ovn-controller recompute` after its system tests and diffs `dump-flows`
against the pre-recompute dump; Calico asserts that a no-op `Apply()` does
not even *read* the dataplane back, because re-reading is O(all rules).
This is the same check at the level our driver works on.
"""

import re
import uuid as sys_uuid

from gcl_sdk.agents.universal.drivers import evpn
from gcl_sdk.agents.universal.drivers.evpn import base
from gcl_sdk.tests.integration.sdn import harness

VOLATILE = re.compile(r"(duration|n_packets|n_bytes|idle_age|hard_age)=\S+")


_run = harness._run


def _flows(bridge):
    """The flow table without the counters, which move on their own."""
    return sorted(
        VOLATILE.sub("", line).strip()
        for line in _run(
            ["ovs-ofctl", "-O", base.OPENFLOW_VERSION, "dump-flows", bridge]
        ).splitlines()
        if "cookie=" in line
    )


def _host_state(fabric):
    """Everything an apply could disturb, as the host reports it.

    Port and interface rows carry their ovsdb uuids, so a port that is
    deleted and recreated with the same name is a difference here even
    though `list-ports` alone would not notice -- and that recreation is
    exactly what breaks a guest, because the new port gets a new ofport
    and every flow that named the old one is left pointing at nothing.
    """
    return {
        "ports": _run(
            ["ovs-vsctl", "--format=csv", "--columns=_uuid,name,tag", "list", "Port"]
        ),
        "interfaces": _run(
            [
                "ovs-vsctl",
                "--format=csv",
                "--columns=_uuid,name,ofport,external_ids",
                "list",
                "Interface",
            ]
        ),
        "flows_br": _flows(fabric.br),
        "flows_evpn": _flows(fabric.evpn_br),
        "netns": sorted(_run(["ip", "netns", "list"]).split()),
    }


def _stamp(guest, port_uuid):
    """Claim the guest's port the way libvirt claims it.

    The driver finds a guest by what libvirt wrote on its interface -- the
    port uuid as `iface-id`, the address as `attached-mac`. Without it there
    is no port to apply to, which is a different failure from the one these
    cases are about.
    """
    _run(
        [
            "ovs-vsctl",
            "set",
            "interface",
            guest.host_veth,
            'external_ids:iface-id="%s"' % port_uuid,
            "--",
            "set",
            "interface",
            guest.host_veth,
            'external_ids:attached-mac="%s"' % guest.mac,
        ]
    )


def _port(port_uuid, mac, vni=16601):
    return evpn.EvpnPort(
        uuid=port_uuid,
        mac=mac,
        ipv4="10.42.9.5",
        vni=vni,
        imp_rt=["65001:%d" % vni],
        exp_rt=["65001:%d" % vni],
        dhcp={},
        nfs=[],
        security_rules=[],
        fips=[],
    )


def test_applying_a_port_twice_leaves_the_host_untouched(fabric):
    """The loop's worst case, made harmless.

    A control plane that cannot agree with this agent about a port applies
    it again every few seconds. That is tolerable only if the second apply
    is a no-op; if it is not, the guest loses its wiring on a timer.
    """
    guest = fabric.add_guest("idem", vlan=0, ip="10.42.9.5/24")
    port_uuid = sys_uuid.uuid4()
    _stamp(guest, port_uuid)
    port = _port(port_uuid, guest.mac)

    port.dump_to_dp()
    before = _host_state(fabric)

    port.dump_to_dp()
    after = _host_state(fabric)

    for key in before:
        assert after[key] == before[key], "second apply changed %s" % key


def test_applying_a_port_ten_times_leaves_the_host_untouched(fabric):
    """Once could be luck -- a cache warm, a file written on the first pass.

    Ten applies is what a stuck reconcile does in half a minute, and it is
    the shape the failure took when it was found: one apply every three
    seconds, for days, against a guest whose networking was correct.
    """
    guest = fabric.add_guest("idem10", vlan=0, ip="10.42.9.6/24")
    port_uuid = sys_uuid.uuid4()
    _stamp(guest, port_uuid)
    port = _port(port_uuid, guest.mac, vni=16602)

    port.dump_to_dp()
    before = _host_state(fabric)

    for _ in range(10):
        port.dump_to_dp()

    assert _host_state(fabric) == before


def test_a_second_port_does_not_rewrite_the_first(fabric):
    """Neighbours are not collateral.

    Applying one port rebuilds what that port needs; a port that shares the
    VNI's namespace, responder and egress with it must come out of that
    with the same ofports and the same flows, or every new guest is an
    outage for the ones already there.
    """
    first = fabric.add_guest("first", vlan=0, ip="10.42.9.7/24")
    first_uuid = sys_uuid.uuid4()
    _stamp(first, first_uuid)
    port_one = _port(first_uuid, first.mac, vni=16603)
    port_one.dump_to_dp()
    before = _host_state(fabric)

    second = fabric.add_guest("second", vlan=0, ip="10.42.9.8/24")
    second_uuid = sys_uuid.uuid4()
    _stamp(second, second_uuid)
    port_two = _port(second_uuid, second.mac, vni=16603)
    port_two.dump_to_dp()
    after = _host_state(fabric)

    # The newcomer adds rows; it must not renumber or rewrite the ones that
    # were there, so every line of the first snapshot survives verbatim.
    for line in before["flows_br"]:
        assert line in after["flows_br"], "applying a second port rewrote %r" % line
    assert set(before["netns"]) <= set(after["netns"])
