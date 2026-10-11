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

"""What a refresh from the data plane is allowed to forget about a port.

libvirt answers only mac and source; any control-plane key a refresh drops
makes actual differ from target forever and the machine never converges.
"""

from unittest import mock
import uuid as sys_uuid

import pytest

from gcl_sdk.agents.universal.drivers import pool as pool_driver

TARGET_PORT_INFO = {
    "mac": "52:54:00:5e:ca:be",
    "ipv4": "10.20.0.22",
    "mask": "255.255.252.0",
    "uuid": "cc9d65e5-9bd6-47d9-864d-880694a10569",
    "source": "exordos-core-net",
    "overlay": False,
}


@pytest.fixture
def machine():
    """A meta machine holding what the control plane sent as target.

    Built, not `__new__`ed: without real initialisation the property
    descriptors share one slot and every assignment overwrites the last —
    the model then lies about itself and the test measures nothing.
    """
    return pool_driver.MetaMachine(
        uuid=sys_uuid.uuid4(),
        project_id=sys_uuid.uuid4(),
        port_info=dict(TARGET_PORT_INFO),
    )


def _dp_machine():
    dp = mock.MagicMock()
    dp.cores, dp.ram, dp.status, dp.boot = 2, 2048, "ACTIVE", "hd0"
    return dp


def _dp_port(mac=TARGET_PORT_INFO["mac"], source=TARGET_PORT_INFO["source"]):
    """A port as the data plane reports it: mac and source, nothing else."""
    port = mock.MagicMock()
    port.mac, port.source = mac, source
    return port


def _refresh(meta_machine, ports):
    # _from_dp_machine also touches image/_is_core_machine; the port half is
    # what these tests are about, so stop right after it.
    with mock.patch.object(
        type(meta_machine), "_is_core_machine", property(lambda self: True)
    ):
        meta_machine._from_dp_machine(_dp_machine(), ports)


def test_a_refresh_keeps_every_fact_the_data_plane_cannot_answer(machine):
    _refresh(machine, [_dp_port()])

    assert machine.port_info == TARGET_PORT_INFO


@pytest.mark.parametrize("key", pool_driver.MetaMachine.DP_OPAQUE_PORT_INFO)
def test_no_control_plane_key_is_dropped_by_a_refresh(machine, key):
    """The regression itself: a dropped key never converges."""
    _refresh(machine, [_dp_port()])

    assert key in machine.port_info, (
        f"{key} was dropped, so actual can never equal target and the "
        "machine is updated for ever"
    )
    assert machine.port_info[key] == TARGET_PORT_INFO[key]


def test_what_the_data_plane_does_answer_is_taken_from_it(machine):
    """mac and source are observed, not preserved."""
    _refresh(machine, [_dp_port(mac="52:54:00:00:00:01", source="br-int")])

    assert machine.port_info["mac"] == "52:54:00:00:00:01"
    assert machine.port_info["source"] == "br-int"


def test_a_key_the_target_never_carried_is_not_invented(machine):
    """An older installation has no `overlay`; a refresh must not add one.

    Inventing `overlay: None` here would be the same defect mirrored: the
    target has no such key, so actual would differ from it for ever.
    """
    machine.port_info.pop("overlay")

    _refresh(machine, [_dp_port()])

    assert "overlay" not in machine.port_info


def test_the_source_of_an_overlay_port_is_not_taken_from_the_data_plane(machine):
    """The one key whose data-plane answer means something else.

    An overlay port is plugged into the integration bridge, so libvirt
    reports `br-int` for all of them, while the control plane names the
    subnet the leg belongs to. Taking the observation would leave actual
    differing from target for ever -- the same failure as a dropped key,
    measured live as one pointless update every three seconds.
    """
    machine.port_info = dict(TARGET_PORT_INFO, overlay=True, source="private-sub")

    _refresh(machine, [_dp_port(source="br-int")])

    assert machine.port_info["source"] == "private-sub"


def test_a_flat_port_still_takes_the_source_it_is_plugged_into(machine):
    """Off the overlay the answer is the question: the source is a libvirt
    network, the data plane knows which one, and drift there is worth
    hearing about."""
    machine.port_info = dict(TARGET_PORT_INFO, overlay=False)

    _refresh(machine, [_dp_port(source="exordos-core-net")])

    assert machine.port_info["source"] == "exordos-core-net"


def _pool_resource(spec):
    from gcl_sdk.agents.universal.dm import models as ua_models

    uuid = sys_uuid.uuid4()
    return ua_models.Resource(
        uuid=uuid,
        kind="machine_pool",
        res_uuid=sys_uuid.uuid4(),
        value={
            "uuid": str(uuid),
            "driver_spec": spec,
            "machine_type": "VM",
            "all_cores": 8,
            "all_ram": 16384,
            "avail_cores": 8,
            "avail_ram": 16384,
            "cores_ratio": 1.0,
            "ram_ratio": 1.0,
            "status": "ACTIVE",
            "storage_pools": [],
        },
    )


def _spec(**overrides):
    spec = pool_driver.LibvirtPoolDriverSpec(
        connection_uri="qemu:///system"
    ).dump_to_simple_view()
    spec.update(overrides)
    return spec


def test_the_agent_reports_the_driver_spec_it_was_handed():
    """A driver spec is the control plane's statement about the pool and the
    data plane owns none of it. A field this SDK has learned and the control
    plane has not must not appear in what is reported back: the actual would
    differ from the target on a key the control plane never wrote, the two
    hashes disagree, and the pool is updated on every iteration until the
    control plane is upgraded — which is exactly the window a data plane
    release lands in."""
    older = {key: value for key, value in _spec().items() if key != "ovs"}
    resource = _pool_resource(older)

    pool = pool_driver.MetaPool.from_ua_resource(resource)
    reported = pool.to_ua_resource(kind="machine_pool")

    assert reported.value["driver_spec"] == older
    assert "ovs" not in reported.value["driver_spec"]
    # The agent still behaves as the absent field says: a spec that does not
    # mention OVS is a pool whose guests are plugged the classic way.
    assert pool.driver_spec.ovs is False


def test_a_field_the_control_plane_does_send_is_reported_back():
    resource = _pool_resource(_spec(ovs=True))

    pool = pool_driver.MetaPool.from_ua_resource(resource)
    reported = pool.to_ua_resource(kind="machine_pool")

    assert reported.value["driver_spec"]["ovs"] is True
    assert pool.driver_spec.ovs is True


def test_a_pool_read_back_from_the_meta_file_still_reports_no_ovs():
    """`list()` rebuilds a pool from what the agent saved, not from the
    target, so the spec has to survive the meta file as well."""
    older = {key: value for key, value in _spec().items() if key != "ovs"}
    pool = pool_driver.MetaPool.from_ua_resource(_pool_resource(older))

    view = pool.dump_to_simple_view()
    if meta_fields := pool.get_meta_fields():
        view = {key: value for key, value in view.items() if key in meta_fields}
    restored = pool_driver.MetaPool.restore_from_simple_view(**view)

    assert restored.to_ua_resource(kind="machine_pool").value["driver_spec"] == older


RELEASED_LIBVIRT_SPEC_KEYS = {
    "kind",
    "connection_uri",
    "network",
    "storage_pool",
    "machine_prefix",
    "network_type",
    "iface_rom_file",
    "iface_mtu",
    "iface_source",
}


@pytest.mark.parametrize("model", [pool_driver.MetaPool, pool_driver.MachinePool])
def test_a_spec_that_asks_for_no_ovs_has_the_released_shape(model):
    """A peer on the last release refuses an unknown field inside a kind
    model outright, so a spec that asks for nothing new must not carry it."""
    spec_type = model.properties.properties["driver_spec"].get_property_type()
    spec = pool_driver.LibvirtPoolDriverSpec(connection_uri="qemu:///system")

    assert set(spec.dump_to_simple_view()) == RELEASED_LIBVIRT_SPEC_KEYS
    assert set(spec_type.to_simple_type(spec)) == RELEASED_LIBVIRT_SPEC_KEYS
    assert spec_type.from_simple_type(spec_type.to_simple_type(spec)).ovs is False


@pytest.mark.parametrize("model", [pool_driver.MetaPool, pool_driver.MachinePool])
def test_a_spec_that_asks_for_ovs_says_so(model):
    spec_type = model.properties.properties["driver_spec"].get_property_type()
    spec = pool_driver.LibvirtPoolDriverSpec(connection_uri="qemu:///system", ovs=True)

    view = spec_type.to_simple_type(spec)

    assert view["ovs"] is True
    assert spec_type.from_simple_type(view).ovs is True


@pytest.mark.parametrize("existing,desired", [(1, 2), (2, 1), (1, 0)])
def test_sdn_nics_hotplug_without_recreating_guest(existing, desired):
    infos = [
        dict(
            TARGET_PORT_INFO,
            mac=f"52:54:00:00:00:{i:02x}",
            overlay=True,
            source="private",
        )
        for i in range(2)
    ]
    meta = pool_driver.MetaMachine(
        uuid=sys_uuid.uuid4(),
        project_id=sys_uuid.uuid4(),
        cores=2,
        ram=2048,
        boot="hd0",
        port_info=infos[0] if desired else {},
        ports_info=infos[:desired],
    )
    dp = _dp_machine()
    dp.image = None
    dp_ports = [_dp_port(info["mac"], "br-int") for info in infos[:existing]]
    observed = [_dp_port(info["mac"], "br-int") for info in infos[:desired]]
    driver = mock.MagicMock()
    driver.get_machine.return_value = (dp, observed)
    pool = mock.MagicMock()
    pool.dp_machine_map = {meta.uuid: dp}
    pool.dp_port_map = {meta.uuid: dp_ports}
    pool.load_driver.return_value = driver
    with mock.patch.object(type(meta), "_is_core_machine", property(lambda self: True)):
        meta.update_on_dp(pool, [])
    assert driver.attach_port.call_count == max(0, desired - existing)
    assert driver.detach_port.call_count == max(0, existing - desired)
    driver.recreate_machine.assert_not_called()
    driver.delete_machine.assert_not_called()
    driver.set_machine_cores.assert_not_called()
    driver.set_machine_ram.assert_not_called()
    assert meta.ports_info == infos[:desired]


def test_sdn_refresh_matches_mac_without_reordering_logical_ports(machine):
    second = dict(
        TARGET_PORT_INFO,
        mac="52:54:00:00:00:02",
        uuid=str(sys_uuid.uuid4()),
        overlay=True,
        source="second",
    )
    first = dict(TARGET_PORT_INFO, overlay=True, source="first")
    machine.port_info = first
    machine.ports_info = [first, second]
    _refresh(
        machine, [_dp_port(second["mac"], "br-int"), _dp_port(first["mac"], "br-int")]
    )
    assert machine.ports_info == [first, second]
    assert machine.port_info == first
