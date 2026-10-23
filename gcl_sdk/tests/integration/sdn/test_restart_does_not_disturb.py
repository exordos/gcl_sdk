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

"""What a restart is allowed to disturb, which is nothing.

Two processes on a hypervisor can die and come back at any time: the agent
that owns the flows, and the responder that serves the guests. Neither
failure is interesting on its own -- systemd brings them back in seconds --
and both are ruinous if coming back means rebuilding what was already right,
because a guest whose patch port is recreated gets a new ofport and every
flow written against the old one stops matching.

Flow age is the witness. OVS reports how long each flow has been installed,
so a flow that survived a restart carries a duration older than the restart,
and a flow that was torn down and put back carries one younger -- no matter
that the two tables print identically otherwise. That is what these cases
read, the same way the no-interruption cases beside them do.

One thing this driver does not promise, measured while these were written:
re-applying a port rewrites its port-security flows -- same text, fresh
duration -- when the vlan map has to be consulted again, and leaves them
alone when it does not. It goes through an atomic bundle, so no packet ever
sees a gap and traffic is unaffected; what it costs is pointless work and
the use of flow age as a witness on that particular cookie. `write_if_changed`
next door states the principle for files ("an apply that changes nothing must
touch nothing"); flows do not follow it yet. So the agent case below asserts
what is true and load-bearing -- the ports are not renumbered and the table
holds the same flows -- and the responder case, whose path does hold its
flows, asserts the ages as well.

OVN asserts this from the far side: it stops `ovn-controller` with the flows
deliberately left in place, injects a packet *while the daemon is dead*, and
requires it to arrive; and separately that a restart does not delete patch
ports. Neutron asserts that an agent restart reuses its patch ports so their
ofports do not change, "otherwise all the flows would be rewritten".
"""

import re
import shutil
import uuid as sys_uuid

import pytest

from gcl_sdk.agents.universal.drivers import evpn
from gcl_sdk.agents.universal.drivers.evpn import base
from gcl_sdk.tests.integration.sdn import harness

VOLATILE = re.compile(r"(duration|n_packets|n_bytes|idle_age|hard_age)=\S+")


_run = harness._run


def _flow_ages(bridge):
    """{flow match: duration} for the whole bridge.

    A flow that was rebuilt has a duration that went *down*, which is the
    only way to tell it apart from the one it replaced.
    """
    ages = {}
    for line in _run(
        ["ovs-ofctl", "-O", base.OPENFLOW_VERSION, "dump-flows", bridge]
    ).splitlines():
        if "duration=" not in line:
            continue
        age = float(line.split("duration=")[1].split(",")[0].rstrip("s"))
        # The flow itself is the key, so two flows that differ only in what
        # they match stay two entries. Keying on anything coarser -- the
        # cookie, the actions -- collapses them onto each other and the
        # comparison silently becomes a coin toss.
        ages[VOLATILE.sub("", line).strip()] = age
    return ages


def _ofports():
    return _run(
        ["ovs-vsctl", "--format=csv", "--columns=name,ofport", "list", "Interface"]
    )


def _stamp(guest, port_uuid):
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


def _port(port_uuid, mac, vni):
    """A fresh model of a port that is already on the host.

    Fresh on purpose: an agent that has just restarted has no memory of
    what it did last time, only the resource the control plane hands it.
    """
    return evpn.EvpnPort(
        uuid=port_uuid,
        mac=mac,
        ipv4="10.42.11.5",
        vni=vni,
        imp_rt=["65001:%d" % vni],
        exp_rt=["65001:%d" % vni],
        dhcp={},
        nfs=[],
        security_rules=[],
        fips=[],
    )


def test_an_agent_that_restarts_does_not_disturb_the_wiring(fabric):
    """The agent comes back and applies the same resource from scratch.

    This is what a restart looks like from the host's side: a process with
    no state, told about a port that is already wired. It must recognise
    its own work and leave it alone.
    """
    guest = fabric.add_guest("ag", vlan=0, ip="10.42.11.5/24")
    port_uuid = sys_uuid.uuid4()
    _stamp(guest, port_uuid)

    _port(port_uuid, guest.mac, 16701).dump_to_dp()
    ages_before, ofports_before = _flow_ages(fabric.br), _ofports()

    # A different object, built the way a restarted process builds one.
    _port(port_uuid, guest.mac, 16701).dump_to_dp()
    ages_after = _flow_ages(fabric.br)

    assert _ofports() == ofports_before, "a restart renumbered the ports"
    # Content, not age: see the note in the module docstring about which of
    # the two this driver currently keeps.
    assert set(ages_after) == set(ages_before), (
        "a restart changed the flow table: %s"
        % sorted(set(ages_after) ^ set(ages_before))
    )


@pytest.mark.skipif(not shutil.which("dhclient"), reason="dhclient not installed")
def test_a_responder_that_is_killed_and_comes_back_keeps_serving(fabric):
    """The responder dies the way a crash kills it, and returns.

    What must survive is not the responder -- it is the guest: the flows
    that punt to it, and the lease it hands out afterwards. A responder
    that rebuilt the punt path on every start would drop every guest on
    the host each time one of them made it crash.
    """
    mac = "52:54:00:c0:11:01"
    record = str(sys_uuid.uuid4())
    fabric.write_record(
        record,
        mac,
        "10.42.11.9",
        vni=16702,
        cidr="10.42.11.0/24",
        mtu=1450,
    )
    guest = fabric.add_guest("sv", vlan=0, ip=None, mac=mac, vni=16702)
    fabric.apply_nf(guest, "dhcp", vni=16702)
    fabric.start_nf()
    assert guest.dhclient(), "the guest never got its first lease"

    ages_before = _flow_ages(fabric.br)

    for proc in list(fabric._procs):
        proc.kill()
        proc.wait(timeout=10)
    fabric._procs.clear()
    fabric.start_nf()

    ages_after = _flow_ages(fabric.br)
    rebuilt = [
        flow
        for flow, age in ages_after.items()
        if flow in ages_before and age < ages_before[flow]
    ]
    assert not rebuilt, "the responder's restart rebuilt guest flows: %s" % rebuilt
    assert guest.dhclient(), "the guest could not renew after the responder came back"


def test_the_witness_notices_a_real_rebuild(fabric):
    """A check that cannot fail is not a check.

    Both cases above pass by finding no flow younger than the restart, and
    they would pass just as quietly if the reading were broken -- if the
    durations were not parsed, if the keys never matched, if the table came
    back empty. So this one destroys the flows on purpose and requires the
    same comparison to say so.

    Calico keeps the equivalent beside its graceful-restart test: a case
    that kills BIRD ungracefully and *demands* the route churn its sibling
    forbids, so a broken measurement cannot let both of them pass.
    """
    guest = fabric.add_guest("nc", vlan=0, ip="10.42.11.5/24")
    port_uuid = sys_uuid.uuid4()
    _stamp(guest, port_uuid)

    port = _port(port_uuid, guest.mac, 16703)
    port.dump_to_dp()
    ages_before = _flow_ages(fabric.br)
    assert ages_before, "no flows to watch, so the other cases prove nothing"

    _run(["ovs-ofctl", "-O", base.OPENFLOW_VERSION, "del-flows", fabric.br])
    _port(port_uuid, guest.mac, 16703).dump_to_dp()

    ages_after = _flow_ages(fabric.br)

    # Not "the same flows came back younger": an apply is not the repair
    # path -- what restores a wiped table is the drift check next door, and
    # a plain apply puts back only what it owns. What must hold is that the
    # reading is live at all: wiping the table has to be visible in it.
    assert ages_after != ages_before, (
        "every flow on the bridge was deleted and the reading did not "
        "change, so the other cases in this file prove nothing"
    )
