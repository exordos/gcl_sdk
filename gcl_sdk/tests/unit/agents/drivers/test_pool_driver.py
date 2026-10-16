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

import datetime
from unittest import mock
import uuid as sys_uuid

import pytest

from gcl_sdk.agents.universal import operations
from gcl_sdk.agents.universal.dm import models
from gcl_sdk.agents.universal.drivers import pool as pool_driver


def _make_resource(kind: str, value: dict) -> models.Resource:
    return models.Resource.from_value(
        value, kind, target_fields=frozenset(value.keys())
    )


def _make_pool(drv: pool_driver.PoolAgentDriver) -> sys_uuid.UUID:
    pool_uuid = sys_uuid.uuid4()
    pool_res = _make_resource(
        "pool", {"uuid": str(pool_uuid), "driver_spec": {"kind": "dummy"}}
    )
    drv.create(pool_res)
    return pool_uuid


class TestPoolAgentDriver:
    """Exercise the pool/volume/machine coordinator driver via DummyPoolDriver."""

    def test_create_and_get_pool(self, tmp_path):
        drv = pool_driver.PoolAgentDriver(meta_file=str(tmp_path / "meta.json"))
        drv.start()

        pool_uuid = sys_uuid.uuid4()
        pool_res = _make_resource(
            "pool", {"uuid": str(pool_uuid), "driver_spec": {"kind": "dummy"}}
        )
        created = drv.create(pool_res)
        assert created.value["driver_spec"]["kind"] == "dummy"

        fetched = drv.get(pool_res)
        assert fetched.uuid == pool_uuid

    def test_create_volume_without_storage_capacity_marks_error(self, tmp_path):
        drv = pool_driver.PoolAgentDriver(meta_file=str(tmp_path / "meta.json"))
        drv.start()
        pool_uuid = _make_pool(drv)

        volume_res = _make_resource(
            "pool_volume",
            {
                "uuid": str(sys_uuid.uuid4()),
                "pool": str(pool_uuid),
                "size": 10,
                "project_id": str(sys_uuid.uuid4()),
            },
        )

        # DummyPoolDriver reports no storage pools, so the coordinator
        # must refuse to create the volume and mark it as errored
        # instead of pretending it was created.
        created = drv.create(volume_res)
        assert created.value["status"] == pool_driver.VolumeStatus.ERROR.value

    def test_create_machine_without_root_volume_raises(self, tmp_path):
        drv = pool_driver.PoolAgentDriver(meta_file=str(tmp_path / "meta.json"))
        drv.start()
        pool_uuid = _make_pool(drv)

        machine_res = _make_resource(
            "pool_machine",
            {
                "uuid": str(sys_uuid.uuid4()),
                "pool": str(pool_uuid),
                "project_id": str(sys_uuid.uuid4()),
            },
        )

        with pytest.raises(pool_driver.RootVolumeNotFound):
            drv.create(machine_res)

    def test_local_pool_agent_driver_adds_local_pool_capability(self, tmp_path):
        local_drv = pool_driver.LocalPoolAgentDriver(
            meta_file=str(tmp_path / "meta.json")
        )
        assert "local_pool" in local_drv.get_capabilities()

        drv = pool_driver.PoolAgentDriver(meta_file=str(tmp_path / "meta.json"))
        assert "pool" in drv.get_capabilities()

    def test_local_pool_agent_driver_lists_local_pool_as_empty(self, tmp_path):
        """ "local_pool" is a scheduling-only marker, not a real resource
        kind: the generic actualization loop calls list() for every
        advertised capability, so it must not raise for this one.
        """
        local_drv = pool_driver.LocalPoolAgentDriver(
            meta_file=str(tmp_path / "meta.json")
        )
        local_drv.start()

        assert local_drv.list("local_pool") == []

    def test_local_pool_agent_driver_lists_pool_normally(self, tmp_path):
        local_drv = pool_driver.LocalPoolAgentDriver(
            meta_file=str(tmp_path / "meta.json")
        )
        local_drv.start()
        pool_uuid = _make_pool(local_drv)

        listed = local_drv.list("pool")
        assert [r.uuid for r in listed] == [pool_uuid]


class TestDummyPoolDriver:
    def test_get_machine_returns_a_machine(self):
        """`status` used to be "running", which is not a MachineStatus, so
        every call raised instead of returning the dummy machine.
        """
        drv = pool_driver.DummyPoolDriver(
            pool_driver.MachinePool(
                name="dummy-pool",
                driver_spec=pool_driver.DummyPoolDriverSpec(),
            )
        )
        machine_uuid = sys_uuid.uuid4()

        machine, ports = drv.get_machine(machine_uuid)

        assert machine.uuid == machine_uuid
        assert machine.status in {s.value for s in pool_driver.MachineStatus}
        assert ports == ()


class _ResizingPoolDriver(pool_driver.DummyPoolDriver):
    """DummyPoolDriver that can answer `get_volume` after a resize."""

    def __init__(self, pool, dp_volume):
        super().__init__(pool)
        self._dp_volume = dp_volume
        self.resized_to = None

    def resize_volume(self, volume):
        self.resized_to = volume.size

    def get_volume(self, volume):
        return self._dp_volume


class TestVolumeResizeCapacity:
    def _pool_with_volume(self, dp_size):
        storage_pool = pool_driver.ThinStoragePool(
            name="storage",
            pool_type="dir",
            capacity_usable=100,
            capacity_provisioned=dp_size,
            oversubscription_ratio=1.0,
        )
        meta_pool = pool_driver.MetaPool(
            uuid=sys_uuid.uuid4(),
            driver_spec=pool_driver.DummyPoolDriverSpec(),
        )
        meta_pool.storage_pools = [storage_pool]

        volume_uuid = sys_uuid.uuid4()
        dp_volume = pool_driver.MachineVolume(
            uuid=volume_uuid,
            name=str(volume_uuid),
            size=dp_size,
            project_id=pool_driver.SYSTEM_PROJECT_ID,
            # What a real driver reports for a volume that exists: both
            # LibvirtPoolDriver.create_volume and its `get_volume` stamp
            # ACTIVE. `update_on_dp` mirrors this back onto the meta model.
            status=pool_driver.VolumeStatus.ACTIVE.value,
        )
        meta_pool.dp_volume_map = {volume_uuid: dp_volume}

        meta_volume = pool_driver.MetaVolume(
            uuid=volume_uuid,
            pool=meta_pool.uuid,
            name=str(volume_uuid),
            size=dp_size,
            project_id=sys_uuid.uuid4(),
        )
        return meta_pool, meta_volume, dp_volume, storage_pool

    def test_growth_is_charged_to_the_storage_pool(self):
        """The delta used to be computed after `dp_volume.size` had already
        been overwritten, so it was always 0: the pool was never charged for
        the growth and `capacity_provisioned` drifted below reality.
        """
        meta_pool, meta_volume, dp_volume, storage_pool = self._pool_with_volume(10)
        meta_volume.size = 30
        # As a previous iteration that refused the resize would have left
        # it: a successful one has to clear that, or the volume stays
        # errored forever once the pool has room again.
        meta_volume.status = pool_driver.VolumeStatus.ERROR.value
        driver = _ResizingPoolDriver(meta_pool, dp_volume)

        with mock.patch.object(
            pool_driver.MetaPool, "load_driver", return_value=driver
        ):
            meta_volume.update_on_dp(meta_pool)

        assert driver.resized_to == 30
        assert storage_pool.capacity_provisioned == 30
        assert meta_volume.status == pool_driver.VolumeStatus.ACTIVE.value

    def test_a_growth_the_pool_cannot_fit_is_refused(self):
        meta_pool, meta_volume, dp_volume, storage_pool = self._pool_with_volume(10)
        meta_volume.size = 500
        driver = _ResizingPoolDriver(meta_pool, dp_volume)

        with mock.patch.object(
            pool_driver.MetaPool, "load_driver", return_value=driver
        ):
            meta_volume.update_on_dp(meta_pool)

        assert meta_volume.status == pool_driver.VolumeStatus.ERROR.value
        assert driver.resized_to is None
        assert storage_pool.capacity_provisioned == 10


class _PowerPoolDriver(pool_driver.DummyPoolDriver):
    """DummyPoolDriver with a machine whose power state the test controls.

    Power calls are only recorded, the machine doesn't change its state by
    itself, like a real hypervisor in the middle of a shutdown.
    """

    def __init__(self, pool, dp_machine):
        super().__init__(pool)
        self.dp_machine = dp_machine
        self.calls = []

    def get_machine(self, machine):
        return self.dp_machine, ()

    def shutdown_machine(self, machine, force=False):
        self.calls.append(("shutdown", force))

    def start_machine(self, machine):
        self.calls.append(("start",))

    def set_machine_cores(self, machine, cores):
        self.calls.append(("cores", cores))


class TestMachinePowerState:
    @pytest.fixture(autouse=True)
    def _setup(self):
        self.clock_now = datetime.datetime(
            2026, 10, 15, 12, 0, tzinfo=datetime.timezone.utc
        )
        self.catalog = operations.OperationCatalog()
        with mock.patch.object(operations, "_now", lambda: self.clock_now):
            yield

    def _tick(self, seconds):
        self.clock_now += datetime.timedelta(seconds=seconds)

    def _machine(self, power_state, dp_power_state):
        meta_pool = pool_driver.MetaPool(
            uuid=sys_uuid.uuid4(),
            driver_spec=pool_driver.DummyPoolDriverSpec(),
        )
        machine_uuid = sys_uuid.uuid4()
        dp_machine = pool_driver.Machine(
            uuid=machine_uuid,
            name="vm",
            cores=1,
            ram=1024,
            power_state=dp_power_state,
            project_id=pool_driver.SYSTEM_PROJECT_ID,
        )
        meta_pool.dp_machine_map = {machine_uuid: dp_machine}
        meta_pool.all_cores = meta_pool.avail_cores = 16
        meta_pool.all_ram = meta_pool.avail_ram = 16384
        driver = _PowerPoolDriver(meta_pool, dp_machine)
        return meta_pool, driver, dp_machine, machine_uuid

    def _update(self, meta_pool, driver, machine_uuid, power_state, cores=1):
        """Run `update_on_dp` like the coordinator does on every iteration."""
        meta_machine = pool_driver.MetaMachine(
            uuid=machine_uuid,
            name="vm",
            cores=cores,
            ram=1024,
            pool=meta_pool.uuid,
            project_id=sys_uuid.uuid4(),
            power_state=power_state,
        )
        with mock.patch.object(
            pool_driver.MetaPool, "load_driver", return_value=driver
        ):
            meta_machine.update_on_dp(meta_pool, volumes=(), ops=self.catalog)
        return meta_machine

    def test_power_off_is_graceful_then_forced(self):
        pool, driver, dp_machine, uuid = self._machine("OFF", "ON")

        actual = self._update(pool, driver, uuid, "OFF")
        assert driver.calls == [("shutdown", False)]
        assert actual.power_state == "ON"

        # The guest is still shutting down, nothing to do
        self._tick(10)
        self._update(pool, driver, uuid, "OFF")
        assert driver.calls == [("shutdown", False)]

        # The guest ignored the shutdown
        self._tick(60)
        self._update(pool, driver, uuid, "OFF")
        assert driver.calls == [("shutdown", False), ("shutdown", True)]

        # The machine reached the target, so the power operation is not
        # called anymore and it's dropped by the driver on `finalize`.
        dp_machine.power_state = "OFF"
        actual = self._update(pool, driver, uuid, "OFF")
        assert actual.power_state == "OFF"
        state = self.catalog.get(uuid, pool_driver.PowerOffMachineOperation)
        assert [r["action"] for r in state.journal] == [
            "started",
            "send_shutdown",
            "send_destroy",
        ]

    def test_power_on(self):
        pool, driver, dp_machine, uuid = self._machine("ON", "OFF")

        self._update(pool, driver, uuid, "ON")
        assert driver.calls == [("start",)]

        dp_machine.power_state = "ON"
        actual = self._update(pool, driver, uuid, "ON")
        assert actual.power_state == "ON"
        assert driver.calls == [("start",)]

    def test_latest_target_wins(self):
        pool, driver, dp_machine, uuid = self._machine("OFF", "ON")

        self._update(pool, driver, uuid, "OFF")
        dp_machine.power_state = "UNKNOWN"
        self._update(pool, driver, uuid, "ON")

        assert self.catalog.get(uuid, pool_driver.PowerOffMachineOperation) is None
        assert self.catalog.get(uuid, pool_driver.PowerOnMachineOperation) is not None
        assert driver.calls == [("shutdown", False), ("start",)]

    def test_reboot_is_shutdown_and_start(self):
        pool, driver, dp_machine, uuid = self._machine("REBOOT", "ON")

        self._update(pool, driver, uuid, "REBOOT")
        assert driver.calls == [("shutdown", False)]

        dp_machine.power_state = "OFF"
        self._update(pool, driver, uuid, "REBOOT")
        assert driver.calls == [("shutdown", False), ("start",)]

        dp_machine.power_state = "ON"
        actual = self._update(pool, driver, uuid, "REBOOT")
        assert actual.power_state == "REBOOTED"

        # Waiting for the control plane to switch the target back to `ON`
        actual = self._update(pool, driver, uuid, "REBOOT")
        assert actual.power_state == "REBOOTED"
        assert len(driver.calls) == 2

        actual = self._update(pool, driver, uuid, "ON")
        assert actual.power_state == "ON"
        assert len(driver.calls) == 2
        assert self.catalog.get(uuid, pool_driver.RebootMachineOperation) is None

    def test_reboot_of_powered_off_machine_starts_it(self):
        pool, driver, dp_machine, uuid = self._machine("REBOOT", "OFF")

        self._update(pool, driver, uuid, "REBOOT")
        assert driver.calls == [("start",)]

        dp_machine.power_state = "ON"
        actual = self._update(pool, driver, uuid, "REBOOT")
        assert actual.power_state == "REBOOTED"

    def test_reboot_cooldown(self):
        pool, driver, dp_machine, uuid = self._machine("REBOOT", "OFF")
        self._update(pool, driver, uuid, "REBOOT")
        dp_machine.power_state = "ON"
        self._update(pool, driver, uuid, "REBOOT")
        self._update(pool, driver, uuid, "ON")
        driver.calls.clear()

        # The next reboot right after the previous one waits
        self._tick(10)
        self._update(pool, driver, uuid, "REBOOT")
        assert driver.calls == []
        assert (
            self.catalog.get(uuid, pool_driver.RebootMachineOperation).last("cooldown")
            is not None
        )

        self._tick(25)
        self._update(pool, driver, uuid, "REBOOT")
        assert driver.calls == [("shutdown", False)]

    def test_no_power_management_without_target(self):
        pool, driver, dp_machine, uuid = self._machine(None, "OFF")

        actual = self._update(pool, driver, uuid, None, cores=2)

        assert driver.calls == [("cores", 2)]
        assert actual.power_state == "OFF"

    def test_other_changes_wait_for_power_operation(self):
        pool, driver, dp_machine, uuid = self._machine("ON", "OFF")

        self._update(pool, driver, uuid, "ON", cores=2)
        assert driver.calls == [("start",)]

        dp_machine.power_state = "ON"
        self._update(pool, driver, uuid, "ON", cores=2)
        assert driver.calls == [("start",), ("cores", 2)]

    def test_other_changes_are_deferred_for_powered_off_machine(self):
        pool, driver, dp_machine, uuid = self._machine("OFF", "OFF")

        self._update(pool, driver, uuid, "OFF", cores=2)

        assert driver.calls == []

    def test_driver_passes_its_operations_to_machines(self, tmp_path):
        drv = pool_driver.PoolAgentDriver(meta_file=str(tmp_path / "meta.json"))
        drv.start()
        machine = pool_driver.MetaMachine(
            uuid=sys_uuid.uuid4(), pool=None, project_id=sys_uuid.uuid4()
        )

        deps = drv._get_dependencies(
            "pool_machine", drv.__coordinator_map__["pool_machine"], machine
        )

        assert deps["ops"] is drv._operations

    def test_driver_drops_operations_of_settled_machines(self, tmp_path):
        drv = pool_driver.PoolAgentDriver(meta_file=str(tmp_path / "meta.json"))
        ops = drv._operations
        settled, updating = sys_uuid.uuid4(), sys_uuid.uuid4()
        ctx = {"driver": mock.Mock(), "machine": mock.Mock(power_state="ON")}

        drv.start()
        for uuid in (settled, updating):
            ops.run(uuid, pool_driver.PowerOffMachineOperation(), **ctx)
        drv.finalize()

        # Only the machine that is still being updated touches its operation
        drv.start()
        ops.run(updating, pool_driver.PowerOffMachineOperation(), **ctx)
        drv.finalize()

        assert ops.get(settled, pool_driver.PowerOffMachineOperation) is None
        assert ops.get(updating, pool_driver.PowerOffMachineOperation) is not None
