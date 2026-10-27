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

"""Integration: EvpnPort._resolve_guest_port against real OVS externals.

The libvirt<->SDN-agent contract: the guest tap carries
``external_ids:iface-id=<port uuid>`` (stamped by the openvswitch
virtualport). Never ``attached-mac``: the MAC is the tenant's to set.
"""

import subprocess
import time
import uuid as sys_uuid

import pytest

from gcl_sdk.agents.universal.drivers import evpn
from gcl_sdk.agents.universal.drivers import exceptions as driver_exc


def _mk_port(port_uuid, mac, **externals):
    return evpn.EvpnPort(
        uuid=port_uuid,
        mac=mac,
        ipv4="10.42.9.9",
        vni=16333,
        imp_rt=["65001:16333"],
        exp_rt=["65001:16333"],
        dhcp={},
    )


def _set_externals(iface, **kv):
    for k, v in kv.items():
        subprocess.run(
            ["ovs-vsctl", "set", "interface", iface, 'external_ids:%s="%s"' % (k, v)],
            check=True,
        )


def test_resolve_by_iface_id(fabric):
    guest = fabric.add_guest("rid", vlan=41, ip="10.42.9.5/24")
    port_uuid = sys_uuid.uuid4()
    _set_externals(guest.host_veth, **{"iface-id": str(port_uuid)})

    port = _mk_port(port_uuid, "52:54:00:00:00:01")
    assert port._resolve_guest_port() == guest.host_veth


def test_a_tap_is_never_resolved_by_its_mac(fabric):
    guest = fabric.add_guest("rmac", vlan=42, ip="10.42.9.6/24")
    # iface-id foreign; attached-mac matches the port.
    _set_externals(
        guest.host_veth,
        **{"iface-id": str(sys_uuid.uuid4()), "attached-mac": guest.mac},
    )

    port = _mk_port(sys_uuid.uuid4(), guest.mac)
    with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
        port._resolve_guest_port()


def test_resolve_unplugged_is_retriable(fabric):
    port = _mk_port(sys_uuid.uuid4(), "52:54:00:de:ad:00")
    with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
        port._resolve_guest_port()


def test_stale_tap_does_not_hide_replugged_guest(fabric):
    from gcl_sdk.agents.universal.drivers.evpn import state

    guest = fabric.add_guest("rnew", vlan=43, ip="10.42.9.7/24")
    port_uuid = sys_uuid.uuid4()
    _set_externals(guest.host_veth, **{"iface-id": str(port_uuid)})
    stale = fabric.add_guest("rold", vlan=44, ip="10.42.9.8/24").host_veth
    _set_externals(stale, **{"iface-id": str(port_uuid)})
    subprocess.run(["ip", "link", "del", stale], check=True)
    for _ in range(30):
        if (
            subprocess.check_output(
                ["ovs-vsctl", "get", "Interface", stale, "ofport"], text=True
            ).strip()
            == "-1"
        ):
            break
        time.sleep(0.1)
    assert (
        subprocess.check_output(
            ["ovs-vsctl", "get", "Interface", stale, "ofport"], text=True
        ).strip()
        == "-1"
    )

    port = _mk_port(port_uuid, "52:54:00:00:00:02")
    assert port._resolve_guest_port() == guest.host_veth
    assert state.snapshot_plugged_ifaces()[str(port_uuid)] == guest.host_veth
