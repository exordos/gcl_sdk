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

"""Integration: what a port reports about itself against the real host.

The snapshot is parsed out of live ``ovs-vsctl``/``ip netns`` output, so
this is the tier that proves the checks read the host correctly — a unit
test can only prove what they do with an answer that was handed to them.

Each case takes exactly one thing away from a healthy guest and asserts
the port names it; the last one takes nothing away and asserts silence,
because a false "missing" recreates the port on every iteration for ever.
"""

import json
import os
import uuid as sys_uuid

from gcl_sdk.agents.universal.drivers import evpn
from gcl_sdk.agents.universal.drivers.evpn import base
from gcl_sdk.agents.universal.drivers.evpn import sh
from gcl_sdk.agents.universal.drivers.evpn import state
from gcl_sdk.tests.integration.sdn import harness

_run = harness._run


def _plug(fabric, name, vlan, port_uuid, mac):
    """A guest wired the way libvirt wires one, with its patch pair."""
    guest = fabric.add_guest(name, vlan=vlan, ip="10.42.8.5/24", mac=mac)
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
            'external_ids:attached-mac="%s"' % mac,
        ]
    )
    _run(
        [
            "ovs-vsctl",
            "--may-exist",
            "add-port",
            fabric.br,
            "pi%d" % vlan,
            "--",
            "set",
            "port",
            "pi%d" % vlan,
            "tag=%d" % vlan,
            "--",
            "set",
            "interface",
            "pi%d" % vlan,
            "type=patch",
            "options:peer=pe%d" % vlan,
            "--",
            "--may-exist",
            "add-port",
            fabric.evpn_br,
            "pe%d" % vlan,
            "--",
            "set",
            "interface",
            "pe%d" % vlan,
            "type=patch",
            "options:peer=pi%d" % vlan,
        ]
    )
    return guest


def _port_for(port_uuid, mac, vlan):
    port = evpn.EvpnPort(
        uuid=port_uuid,
        mac=mac,
        ipv4="10.42.8.5",
        vni=16444,
        imp_rt=["65001:16444"],
        exp_rt=["65001:16444"],
        dhcp={},
        nfs=[],
        security_rules=[],
        fips=[],
        # Wired by hand: no anti-spoof flows to ask about.
        port_security=False,
    )
    # The client config is the one fact that is not read off the host.
    sh.write_atomic(os.path.join(base.CLIENT_CONF_DIR, "%s.json" % port_uuid), "{}")
    # The map is written by the driver when it hands a guest its VLAN. These
    # cases wire the guest by hand, so on a fresh work directory there is
    # nothing to read yet — and reading it was failing every case here at
    # setup, which reads as four broken checks rather than an absent file.
    try:
        with open(base.VLAN_MAP_PATH) as fl:
            vlan_map = json.load(fl)
    except (OSError, ValueError):
        vlan_map = {}
    vlan_map[str(port_uuid)] = vlan
    sh.write_atomic(base.VLAN_MAP_PATH, json.dumps(vlan_map))
    return port


def _missing(port):
    """What the port reports against a freshly read host."""
    state._DP_SNAPSHOT = state.snapshot_dataplane()
    try:
        return port._missing_on_dp()
    finally:
        state._DP_SNAPSHOT = None


def test_a_healthy_guest_reports_nothing(fabric):
    port_uuid, mac, vlan = sys_uuid.uuid4(), "52:54:00:d0:0d:01", 51
    _plug(fabric, "ok", vlan, port_uuid, mac)
    assert _missing(_port_for(port_uuid, mac, vlan)) == []


def test_the_snapshot_reads_the_host_it_is_given(fabric):
    """The parsers, against real ovs-vsctl output: an access tag is an
    integer (a trunk port's is not), and a guest is found by its iface-id
    alone: the MAC is the tenant's to set."""
    port_uuid, mac, vlan = sys_uuid.uuid4(), "52:54:00:d0:0d:02", 52
    guest = _plug(fabric, "snap", vlan, port_uuid, mac)

    ovs = state.snapshot_ovs()
    assert ovs["tags"][guest.host_veth] == vlan
    assert ovs["tags"]["pi%d" % vlan] == vlan
    assert guest.host_veth in ovs["ports"] and "pi%d" % vlan in ovs["ports"]
    assert "pe%d" % vlan in ovs["evpn_ports"]

    plugged = state.snapshot_plugged_ifaces()
    assert plugged[str(port_uuid)] == guest.host_veth
    assert mac not in plugged


def test_a_guest_that_lost_its_far_patch_is_reported(fabric):
    port_uuid, mac, vlan = sys_uuid.uuid4(), "52:54:00:d0:0d:03", 53
    _plug(fabric, "pe", vlan, port_uuid, mac)
    port = _port_for(port_uuid, mac, vlan)
    assert _missing(port) == []

    _run(["ovs-vsctl", "del-port", fabric.evpn_br, "pe%d" % vlan])
    assert "evpn patch" in _missing(port)


def test_a_guest_knocked_off_its_vlan_is_reported(fabric):
    port_uuid, mac, vlan = sys_uuid.uuid4(), "52:54:00:d0:0d:04", 54
    guest = _plug(fabric, "tag", vlan, port_uuid, mac)
    port = _port_for(port_uuid, mac, vlan)
    assert _missing(port) == []

    _run(["ovs-vsctl", "remove", "port", guest.host_veth, "tag", str(vlan)])
    assert "guest vlan tag" in _missing(port)

    # A guest that is not plugged at all is not drift: its port keeps
    # everything it was given, and nothing needs reinstalling for a VM
    # that is merely switched off.
    _run(["ovs-vsctl", "del-port", fabric.br, guest.host_veth])
    assert _missing(port) == []


def test_an_egress_the_subnet_asked_for_is_reported(fabric):
    """The namespace, its config and its leg are read off the host, and a
    subnet that never asked for a gateway is not asked about any of them."""
    port_uuid, mac, vlan = sys_uuid.uuid4(), "52:54:00:d0:0d:05", 55
    guest = _plug(fabric, "eg", vlan, port_uuid, mac)
    port = _port_for(port_uuid, mac, vlan)
    routed = {
        "cidr": "10.42.8.0/24",
        "routers": [{"to": "0.0.0.0/0", "via": "10.42.8.1"}],
        "mtu": 1450,
    }

    port.dhcp = routed
    reported = _missing(port)
    assert "egress namespace" in reported
    assert "egress gateway config" in reported
    assert "egress gateway leg" in reported

    fabric.apply_egress(guest, port.vni, routed["cidr"], "10.42.8.1")
    # This tier emulates evpn_connector's delivery by re-plugging the leg
    # onto br-int, so the leg really is off the evpn bridge here — and
    # everything else the subnet asked for is now installed.
    assert _missing(port) == ["egress gateway leg"]

    port.dhcp = {"cidr": "10.42.8.0/24", "mtu": 1450}  # no gateway
    assert _missing(port) == []
