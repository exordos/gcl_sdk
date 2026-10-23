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

"""A hypervisor is not a demo, and its numbers run out.

Every guest port on a host takes one of 4094 local VLANs -- its own, because
two guests sharing one would flap the router MAC across sibling patches --
and the number names its patch pair. That is a finite pool with a name
attached, which makes three things worth asserting that a two-guest test can
never see.

The numbers must not collide, or two guests share a broadcast domain that
was supposed to separate them. They must come *back* when a guest leaves,
because a hypervisor that leaks one number per departed guest works
perfectly for months and then refuses to start anything, at an hour nobody
chose, with the failure landing on whichever guest happened to be next. And
when the pool is genuinely full the driver has to say so against the port it
could not wire, rather than handing out a number somebody is already using
-- a silent collision is far worse than a refusal, because the refusal is
retried and the collision is debugged.

The last of these is checked from the failure side deliberately: exhaustion
is unreachable in any ordinary run, so the only way to know the code path
works is to put the host in that state on purpose. OVN keeps a scale suite
for the same reason, and Neutron asserts its own allocator both hands out
distinct numbers and reuses freed ones.
"""

import json
import uuid as sys_uuid

import pytest

from gcl_sdk.agents.universal.drivers import evpn
from gcl_sdk.agents.universal.drivers import exceptions as driver_exc
from gcl_sdk.agents.universal.drivers.evpn import base
from gcl_sdk.agents.universal.drivers.evpn import hostns
from gcl_sdk.tests.integration.sdn import harness

CEILING = 4094


_run = harness._run


@pytest.fixture
def vlan_map(tmp_path, monkeypatch):
    """An empty pool of this host's numbers, isolated from the real one."""
    path = str(tmp_path / "vlan_map.json")
    monkeypatch.setattr(base, "VLAN_MAP_PATH", path)
    return path


def _seed(path, count):
    """Put the host in the state a long-lived one reaches by itself."""
    with open(path, "w") as fl:
        json.dump({"seed-%d" % n: n for n in range(1, count + 1)}, fl)


def test_no_two_ports_are_given_the_same_number(vlan_map):
    """Two hundred ports, two hundred numbers, and each one stable."""
    ports = [sys_uuid.uuid4() for _ in range(200)]

    numbers = [hostns.local_vlan_for_port(p) for p in ports]

    assert len(set(numbers)) == len(numbers), "two ports were given one VLAN"
    assert all(1 <= n <= CEILING for n in numbers), "a number outside the pool: %s" % (
        sorted(numbers)[:3],
    )
    # Asking again is not a second allocation: the number names the port's
    # patch pair, so a port whose number moved would lose its wiring.
    assert [hostns.local_vlan_for_port(p) for p in ports] == numbers


def test_a_number_comes_back_when_its_port_leaves(vlan_map):
    """The case a long-lived hypervisor lives or dies by.

    Not "a release works" -- one release working is easy. What matters is
    that the pool does not drift: five hundred guests come and go one after
    another, as they do over a host's life, and the host must be exactly as
    able to wire the last one as the first.
    """
    for _ in range(500):
        port = sys_uuid.uuid4()
        hostns.local_vlan_for_port(port)
        hostns.release_local_vlan(port)

        with open(vlan_map) as fl:
            left = json.load(fl)
        last = left.pop(hostns._VLAN_CURSOR_KEY)
        assert left == {}, "a departed guest left its number behind"

    # Round-robin: the next number follows the last one handed out.
    assert hostns.local_vlan_for_port(sys_uuid.uuid4()) > last


@pytest.mark.expect_logs(r"no free local VLAN left")
def test_a_full_host_refuses_the_port_it_cannot_wire(vlan_map):
    """The pool is genuinely full, and the answer must be a refusal.

    Raised as the driver's own retriable error so the agent reports it
    against this resource and goes on with the rest of its iteration --
    rather than a bare RuntimeError, which no caller expects and which takes
    the whole pass down with it.
    """
    _seed(vlan_map, CEILING)
    port = sys_uuid.uuid4()

    with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
        hostns.local_vlan_for_port(port)

    assert hostns.lookup_local_vlan(port) is None, (
        "a host with no numbers left recorded one anyway"
    )
    with open(vlan_map) as fl:
        assert len(json.load(fl)) == CEILING, "the refused port was written down"


@pytest.mark.expect_logs(r"no free local VLAN left")
def test_the_last_free_number_is_given_out_and_not_one_in_use(vlan_map):
    """The boundary, from the side where an off-by-one is a collision.

    A pool that stops one short wastes a guest; a pool that goes one over
    hands out a number another guest is already using, and the two share a
    broadcast domain while everything looks wired. So the host is put one
    number short of full and the arithmetic is read directly.
    """
    _seed(vlan_map, CEILING - 1)

    last = hostns.local_vlan_for_port(sys_uuid.uuid4())

    assert last == CEILING, "the last number in the pool was never handed out"
    with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
        hostns.local_vlan_for_port(sys_uuid.uuid4())


def test_many_guests_on_one_bridge_keep_their_own_wiring(fabric):
    """The numbers are one thing; what they name on the bridge is another.

    Eight ports through the real driver, which is enough for a collision to
    be visible: each must end up with its own patch pair and its own ofport.
    Then one leaves, and the other seven must be exactly as they were --
    a GC that reached past its own port would be invisible with two guests
    and ruinous with eighty.
    """
    guests = []
    for n in range(8):
        guest = fabric.add_guest("m%d" % n, vlan=0, ip="10.42.20.%d/24" % (n + 10))
        port_uuid = sys_uuid.uuid4()
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
        evpn.EvpnPort(
            uuid=port_uuid,
            mac=guest.mac,
            ipv4="10.42.20.%d" % (n + 10),
            vni=16720,
            imp_rt=["65001:16720"],
            exp_rt=["65001:16720"],
            dhcp={},
            nfs=[],
            security_rules=[],
            fips=[],
        ).dump_to_dp()
        guests.append((port_uuid, hostns.lookup_local_vlan(port_uuid)))

    vlans = [vlan for _, vlan in guests]
    assert None not in vlans, "a guest was wired without a number: %s" % vlans
    assert len(set(vlans)) == len(vlans), "two guests share a local VLAN: %s" % vlans

    ports = _run(["ovs-vsctl", "list-ports", fabric.br])
    assert all("pi%d" % vlan in ports for vlan in vlans), (
        "a guest ended up without its own patch: %s" % ports
    )
    ofports = {fabric.ofport_of("pi%d" % vlan) for vlan in vlans}
    assert len(ofports) == len(vlans), "two patches answer to one ofport"

    # One guest leaves. Its own wiring goes; nobody else's does.
    gone_uuid, gone_vlan = guests[3]
    hostns.gc_guest(gone_uuid)

    ports = _run(["ovs-vsctl", "list-ports", fabric.br])
    assert "pi%d" % gone_vlan not in ports, "the departed guest kept its patch"
    for port_uuid, vlan in guests:
        if port_uuid == gone_uuid:
            continue
        assert "pi%d" % vlan in ports, "guest %s lost its patch to a neighbour's GC" % (
            port_uuid,
        )
        assert hostns.lookup_local_vlan(port_uuid) == vlan, (
            "guest %s lost its number to a neighbour's GC" % port_uuid
        )
