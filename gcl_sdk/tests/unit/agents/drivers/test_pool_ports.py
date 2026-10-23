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

from types import SimpleNamespace
from unittest import mock
import uuid

import pytest
from restalchemy.dm import types_dynamic

from gcl_sdk.agents.universal.dm import models
from gcl_sdk.agents.universal.drivers import pool as p


def resource(kind, **value):
    return models.Resource.from_value(value, kind, target_fields=frozenset(value))


@pytest.mark.parametrize(
    "cls", [p.LibvirtPoolDriverSpec, p.ExordosLocalHyperDriverSpec]
)
@pytest.mark.parametrize("ovs", [False, True])
def test_pool_spec_uses_standard_kind_serialization(cls, ovs):
    kwargs = {"node": uuid.uuid4()} if cls is p.ExordosLocalHyperDriverSpec else {}
    spec = cls(connection_uri="qemu:///system", ovs=ovs, **kwargs)
    codec = types_dynamic.KindModelType(cls)
    view = codec.to_simple_type(spec)
    assert view["ovs"] is ovs
    assert codec.from_simple_type(view).ovs is ovs
    assert spec.dump_to_simple_view()["ovs"] is ovs


@pytest.fixture
def fabric(tmp_path):
    agent = p.PoolAgentDriver(meta_file=str(tmp_path / "pool.json"))
    agent.start()
    pool_id, machine_id, project_id = [uuid.uuid4() for _ in range(3)]
    machines, nics = {}, {}
    driver = mock.Mock(spec=p.AbstractPoolDriver)

    def observed(port):
        return SimpleNamespace(
            uuid=port.uuid,
            mac=port.mac,
            source="br-int" if port.overlay else port.source,
        )

    def attach(machine, port):
        nics[machine.uuid].append(observed(port))

    def detach(machine, port):
        nics[machine.uuid][:] = [
            nic for nic in nics[machine.uuid] if nic.mac != port.mac
        ]

    def create(machine, volumes, ports):
        machine.status = "ACTIVE"
        machines[machine.uuid] = machine
        nics[machine.uuid] = [observed(port) for port in ports]
        return machine, tuple(nics[machine.uuid])

    driver.get_machine.side_effect = lambda key: (machines[key], nics[key])
    driver.attach_port.side_effect = attach
    driver.detach_port.side_effect = detach
    driver.create_machine.side_effect = create
    driver.delete_machine.side_effect = lambda machine: machines.pop(machine.uuid, None)
    pool = SimpleNamespace(
        uuid=pool_id,
        dp_machine_map=machines,
        dp_port_map=nics,
        avail_cores=100,
        avail_ram=100000,
        load_driver=lambda: driver,
    )
    agent._coordinator_storage["pool"][pool_id] = pool
    return SimpleNamespace(
        agent=agent,
        pool=pool,
        driver=driver,
        machines=machines,
        nics=nics,
        machine=machine_id,
        project=project_id,
    )


def port_target(fabric, index=0, **kwargs):
    return resource(
        "pool_port",
        uuid=str(uuid.uuid4()),
        pool=str(fabric.pool.uuid),
        machine=str(fabric.machine),
        project_id=str(fabric.project),
        mac=f"52:54:00:00:00:{index:02x}",
        source="private",
        overlay=True,
        index=index,
        attached=True,
        boot="hd0",
        **kwargs,
    )


def existing_machine(fabric):
    machine = p.Machine(
        uuid=fabric.machine,
        project_id=fabric.project,
        cores=2,
        ram=2048,
        boot="hd0",
        name="guest",
        status="ACTIVE",
    )
    fabric.machines[machine.uuid] = machine
    fabric.nics[machine.uuid] = []
    return machine


def test_machine_consumes_port_declarations_without_dependency_cycle(fabric):
    target = port_target(fabric)
    pending = fabric.agent.create(target)
    assert pending.value["attached"] is False
    assert fabric.agent.get(target).hash != target.hash
    volume = p.MetaVolume(
        uuid=uuid.uuid4(),
        pool=fabric.pool.uuid,
        project_id=fabric.project,
        machine=fabric.machine,
        index=0,
        size=1,
        status="ACTIVE",
    )
    fabric.agent._coordinator_storage["pool_volume"][volume.uuid] = volume
    machine = resource(
        "pool_machine",
        uuid=str(fabric.machine),
        pool=str(fabric.pool.uuid),
        project_id=str(fabric.project),
        name="guest",
        cores=2,
        ram=2048,
        boot="hd0",
    )
    fabric.agent.create(machine)
    assert fabric.agent.get(target).hash == target.hash
    actual = fabric.driver.create_machine.call_args.args[2]
    assert [port.uuid for port in actual] == [target.uuid]
    fabric.driver.attach_port.assert_not_called()


def test_hotplug_and_delete_do_not_update_the_machine(fabric):
    existing_machine(fabric)
    primary, secondary = port_target(fabric), port_target(fabric, 1)
    assert fabric.agent.create(primary).hash == primary.hash
    assert fabric.agent.create(secondary).hash == secondary.hash
    fabric.agent.delete(secondary)
    fabric.agent.delete(primary)
    assert fabric.nics[fabric.machine] == []
    assert fabric.driver.attach_port.call_count == 2
    assert fabric.driver.detach_port.call_count == 2
    fabric.driver.create_machine.assert_not_called()
    fabric.driver.recreate_machine.assert_not_called()
    fabric.driver.delete_machine.assert_not_called()


def test_missing_nic_is_reported_and_repaired_idempotently(fabric):
    existing_machine(fabric)
    target = port_target(fabric)
    fabric.agent.create(target)
    fabric.nics[fabric.machine].clear()
    assert fabric.agent.get(target).hash != target.hash
    assert fabric.agent.update(target).hash == target.hash
    fabric.agent.update(target)
    assert fabric.driver.attach_port.call_count == 2
    assert len(fabric.nics[fabric.machine]) == 1


@pytest.mark.parametrize(
    "change", [{"mac": "52:54:00:ff:ff:ff"}, {"overlay": False, "source": "bootnet"}]
)
def test_port_changes_remove_the_previous_attachment(fabric, change):
    existing_machine(fabric)
    target = port_target(fabric)
    fabric.agent.create(target)
    updated = resource("pool_port", **(target.value | change))
    assert fabric.agent.update(updated).hash == updated.hash
    assert len(fabric.nics[fabric.machine]) == 1
    assert fabric.nics[fabric.machine][0].mac == updated.value["mac"]
    assert fabric.driver.detach_port.call_count == 1


def test_port_restores_from_meta_and_delete_tolerates_deleted_machine(fabric):
    existing_machine(fabric)
    target = port_target(fabric)
    fabric.agent.create(target)
    fabric.agent.finalize()
    restarted = p.PoolAgentDriver(meta_file=fabric.agent._meta_file)
    restarted.start()
    restarted._coordinator_storage["pool"][fabric.pool.uuid] = fabric.pool
    assert restarted.list("pool_port")[0].hash == target.hash
    fabric.machines.clear()
    restarted.delete(target)
    assert restarted.list("pool_port") == []


@pytest.mark.parametrize(
    "fields",
    [["uuid", "port_info", "ports_info"], {"uuid": None, "port_info": {"mac": None}}],
)
def test_released_machine_meta_restores_without_embedded_nics(fields):
    machine = p.MetaMachine.restore_from_simple_view(
        uuid=str(uuid.uuid4()),
        project_id=str(uuid.uuid4()),
        port_info={"mac": "52:54:00:00:00:00"},
        ports_info=None,
        target_fields=fields,
    )
    restored = machine.dump_to_simple_view()
    assert "port_info" not in restored
    assert "ports_info" not in restored
    assert set(machine.target_fields) == {"uuid"}


def test_port_observation_uses_the_pool_snapshot(fabric):
    existing_machine(fabric)
    target = port_target(fabric)
    fabric.agent.create(target)
    fabric.driver.reset_mock()
    assert fabric.agent.get(target).hash == target.hash
    assert fabric.agent.list("pool_port")[0].hash == target.hash
    fabric.driver.get_machine.assert_not_called()


def test_retry_after_attach_completed_but_meta_was_not_saved(fabric):
    existing_machine(fabric)
    target = port_target(fabric)
    attach = fabric.driver.attach_port.side_effect

    def interrupted(machine, port):
        attach(machine, port)
        raise RuntimeError("interrupted before meta persistence")

    fabric.driver.attach_port.side_effect = interrupted
    with pytest.raises(RuntimeError):
        fabric.agent.create(target)
    fabric.driver.attach_port.side_effect = attach
    assert fabric.agent.create(target).hash == target.hash
    assert fabric.driver.attach_port.call_count == 1
    assert len(fabric.nics[fabric.machine]) == 1


def test_reimage_waits_for_port_targets_from_the_same_boot_phase(fabric):
    machine = existing_machine(fabric)
    machine.image = "old"
    port = port_target(fabric)
    fabric.agent.create(port)
    previous = resource(
        "pool_machine",
        uuid=str(fabric.machine),
        pool=str(fabric.pool.uuid),
        project_id=str(fabric.project),
        name="guest",
        cores=2,
        ram=2048,
        boot="hd0",
        image="old",
    )
    fabric.agent._add_to_meta("pool_machine", p.MetaMachine.from_ua_resource(previous))
    target = resource(
        "pool_machine", **(previous.value | {"boot": "network", "image": "new"})
    )
    waiting = fabric.agent.update(target)
    assert waiting.hash != target.hash
    assert waiting.value["boot"] == "hd0"
    assert waiting.value["image"] == "old"
    fabric.driver.recreate_machine.assert_not_called()
    boot_port = resource(
        "pool_port",
        **(port.value | {"boot": "network", "source": "bootnet", "overlay": False}),
    )
    fabric.agent.update(boot_port)
    completed = fabric.agent.update(target)
    assert completed.hash == target.hash
    assert fabric.driver.recreate_machine.call_count == 1
    assert (
        fabric.driver.recreate_machine.call_args.kwargs["ports"][0].source == "bootnet"
    )


def test_delete_keeps_meta_until_async_detach_has_completed(fabric):
    existing_machine(fabric)
    target = port_target(fabric)
    fabric.agent.create(target)
    detach = fabric.driver.detach_port.side_effect
    fabric.driver.detach_port.side_effect = None
    with pytest.raises(p.PortAlreadyAttachedError):
        fabric.agent.delete(target)
    assert fabric.agent.list("pool_port")[0].uuid == target.uuid
    fabric.driver.detach_port.side_effect = detach
    fabric.agent.delete(target)
    assert fabric.agent.list("pool_port") == []


def test_missing_boot_source_does_not_create_a_nic_or_machine(fabric):
    target = port_target(fabric)
    pending = resource(
        "pool_port", **(target.value | {"overlay": False, "source": None})
    )
    created = fabric.agent.create(pending)
    assert created.value["attached"] is False
    declaration = fabric.agent._coordinator_storage["pool_port"][pending.uuid]
    volume = p.MetaVolume(
        uuid=uuid.uuid4(),
        pool=fabric.pool.uuid,
        project_id=fabric.project,
        machine=fabric.machine,
        index=0,
        size=1,
        status="ACTIVE",
    )
    machine = p.MetaMachine(
        uuid=fabric.machine,
        pool=fabric.pool.uuid,
        project_id=fabric.project,
        boot="hd0",
        cores=2,
        ram=2048,
    )
    with pytest.raises(
        p.MachinePortsNotReady,
        match=f"Ports are not ready for machine {fabric.machine}",
    ):
        machine.dump_to_dp(fabric.pool, [volume], [declaration])
    fabric.driver.create_machine.assert_not_called()
    fabric.driver.attach_port.assert_not_called()
