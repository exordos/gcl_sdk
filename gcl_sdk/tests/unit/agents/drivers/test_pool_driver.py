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
from gcl_sdk.infra import constants as ic


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
            storage_pool="storage",
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


def _make_storage_pool(name, tier, ephemeral, capacity_usable, capacity_provisioned=0):
    return pool_driver.ThinStoragePool(
        name=name,
        pool_type="dir",
        tier=tier,
        ephemeral=ephemeral,
        capacity_usable=capacity_usable,
        capacity_provisioned=capacity_provisioned,
    )


class TestSelectStoragePool:
    """The soft match picks the most free-space pool, not the first one."""

    def test_exact_match_picks_the_one_with_most_free_space(self):
        small_hot = _make_storage_pool("small-hot", ic.DiskTier.HOT.value, False, 20)
        big_hot = _make_storage_pool("big-hot", ic.DiskTier.HOT.value, False, 100)
        warm = _make_storage_pool("warm", ic.DiskTier.WARM.value, False, 1000)

        selected = pool_driver.select_storage_pool(
            [small_hot, big_hot, warm], ic.DiskTier.HOT.value, False, 10
        )

        assert selected is big_hot

    def test_fallback_match_picks_the_one_with_most_free_space(self):
        # Neither pool is hot, so this falls back to the largest pool with
        # room, rather than whichever happens to be listed first.
        small = _make_storage_pool("small", ic.DiskTier.WARM.value, False, 20)
        big = _make_storage_pool("big", ic.DiskTier.COLD.value, False, 100)

        selected = pool_driver.select_storage_pool(
            [small, big], ic.DiskTier.HOT.value, False, 10
        )

        assert selected is big

    def test_fuller_exact_match_loses_to_emptier_one(self):
        full_hot = _make_storage_pool(
            "full-hot", ic.DiskTier.HOT.value, False, 100, capacity_provisioned=95
        )
        empty_hot = _make_storage_pool("empty-hot", ic.DiskTier.HOT.value, False, 100)

        selected = pool_driver.select_storage_pool(
            [full_hot, empty_hot], ic.DiskTier.HOT.value, False, 10
        )

        assert selected is empty_hot

    def test_assigned_name_ignores_weight(self):
        assigned = _make_storage_pool("assigned", ic.DiskTier.WARM.value, False, 20)
        bigger = _make_storage_pool("bigger", ic.DiskTier.WARM.value, False, 100)

        selected = pool_driver.select_storage_pool(
            [assigned, bigger],
            ic.DiskTier.WARM.value,
            False,
            10,
            assigned_name="assigned",
        )

        assert selected is assigned

    def test_no_pool_fits(self):
        small = _make_storage_pool("small", ic.DiskTier.WARM.value, False, 5)

        selected = pool_driver.select_storage_pool(
            [small], ic.DiskTier.WARM.value, False, 10
        )

        assert selected is None

    def test_never_places_a_durable_request_on_an_ephemeral_pool(self):
        # Unlike tier, ephemeral is a data-safety property: falling
        # back across it could silently wipe a "durable" disk on host
        # reboot, so the durable request must go unscheduled (retried
        # later) rather than land on the ephemeral pool with room.
        ephemeral_pool = _make_storage_pool(
            "ephemeral", ic.DiskTier.HOT.value, True, 100
        )

        selected = pool_driver.select_storage_pool(
            [ephemeral_pool], ic.DiskTier.HOT.value, False, 10
        )

        assert selected is None

    def test_never_places_an_ephemeral_request_on_a_durable_pool(self):
        durable_pool = _make_storage_pool("durable", ic.DiskTier.HOT.value, False, 100)

        selected = pool_driver.select_storage_pool(
            [durable_pool], ic.DiskTier.HOT.value, True, 10
        )

        assert selected is None

    def test_tier_still_falls_back_within_the_matching_ephemeral_tier(self):
        cold_ephemeral = _make_storage_pool(
            "cold-ephemeral", ic.DiskTier.COLD.value, True, 100
        )
        hot_durable = _make_storage_pool(
            "hot-durable", ic.DiskTier.HOT.value, False, 100
        )

        # Requesting hot+ephemeral: no exact match, but the ephemeral
        # pool (wrong tier) is still preferred over the durable one
        # (wrong ephemeral) - the tier fallback only crosses within the
        # same durability tier.
        selected = pool_driver.select_storage_pool(
            [cold_ephemeral, hot_durable], ic.DiskTier.HOT.value, True, 10
        )

        assert selected is cold_ephemeral


class TestExordosLocalHyperDriverSpecStoragePools:
    def _spec(self, storage_pool):
        return pool_driver.ExordosLocalHyperDriverSpec(
            connection_uri="qemu:///system",
            node=sys_uuid.uuid4(),
            storage_pool=storage_pool,
        )

    def test_list_round_trips(self):
        entries = [{"name": "local", "tier": "WARM", "ephemeral": True}]
        spec = self._spec(entries)
        restored = pool_driver.ExordosLocalHyperDriverSpec.restore_from_simple_view(
            **spec.dump_to_simple_view()
        )
        assert restored.storage_pool == entries

    @pytest.mark.parametrize(
        "value",
        [
            123,
            "local",
            [123],
            [None],
            ["local"],
            [{}],
            [{"name": "local", "unknown": True}],
            [{"name": "local", "tier": "invalid"}],
        ],
    )
    def test_invalid_shapes_raise_type_error(self, value):
        with pytest.raises(TypeError):
            self._spec(value)
        with pytest.raises(TypeError):
            pool_driver.ExordosLocalHyperDriverSpec.restore_from_simple_view(
                kind="exordos_local_hyper",
                connection_uri="qemu:///system",
                node=str(sys_uuid.uuid4()),
                storage_pool=value,
            )


class _FailingCreatePoolDriver(pool_driver.DummyPoolDriver):
    """DummyPoolDriver whose create_volume always fails."""

    def create_volume(self, volume):
        raise RuntimeError("boom")


class TestDumpToDpStoragePoolPinning:
    """A failed create must not leave the volume pinned to the pool it
    failed on - otherwise a later retry can never try a different one,
    even if that pool is now full/unavailable and another has room.
    """

    def test_create_failure_does_not_pin_storage_pool(self):
        storage_pool = pool_driver.ThinStoragePool(
            name="storage",
            pool_type="dir",
            capacity_usable=100,
            capacity_provisioned=0,
        )
        meta_pool = pool_driver.MetaPool(
            uuid=sys_uuid.uuid4(),
            driver_spec=pool_driver.DummyPoolDriverSpec(),
        )
        meta_pool.storage_pools = [storage_pool]
        meta_pool.dp_volume_map = {}

        volume_uuid = sys_uuid.uuid4()
        meta_volume = pool_driver.MetaVolume(
            uuid=volume_uuid,
            pool=meta_pool.uuid,
            name=str(volume_uuid),
            size=10,
            project_id=sys_uuid.uuid4(),
        )

        driver = _FailingCreatePoolDriver(meta_pool)

        with mock.patch.object(
            pool_driver.MetaPool, "load_driver", return_value=driver
        ):
            with pytest.raises(RuntimeError):
                meta_volume.dump_to_dp(meta_pool)

        assert meta_volume.storage_pool is None
        assert storage_pool.capacity_provisioned == 0


class TestStoragePoolConsistency:
    def test_update_on_dp_rejects_a_conflicting_storage_pool_change(self):
        # The scheduler/meta state says "pool-b", but the volume
        # actually lives on "pool-a" - migrating between pools isn't
        # supported, so the data plane's own record must win.
        storage_pool_a = pool_driver.ThinStoragePool(
            name="pool-a", pool_type="dir", capacity_usable=100
        )
        meta_pool = pool_driver.MetaPool(
            uuid=sys_uuid.uuid4(), driver_spec=pool_driver.DummyPoolDriverSpec()
        )
        meta_pool.storage_pools = [storage_pool_a]

        volume_uuid = sys_uuid.uuid4()
        dp_volume = pool_driver.MachineVolume(
            uuid=volume_uuid,
            name=str(volume_uuid),
            size=10,
            project_id=pool_driver.SYSTEM_PROJECT_ID,
            status=pool_driver.VolumeStatus.ACTIVE.value,
            storage_pool="pool-a",
        )
        meta_pool.dp_volume_map = {volume_uuid: dp_volume}

        meta_volume = pool_driver.MetaVolume(
            uuid=volume_uuid,
            pool=meta_pool.uuid,
            name=str(volume_uuid),
            size=10,
            project_id=sys_uuid.uuid4(),
            storage_pool="pool-b",
        )

        driver = _ResizingPoolDriver(meta_pool, dp_volume)

        with mock.patch.object(
            pool_driver.MetaPool, "load_driver", return_value=driver
        ):
            meta_volume.update_on_dp(meta_pool)

        assert meta_volume.storage_pool == "pool-a"
        assert meta_volume.status == pool_driver.VolumeStatus.ERROR.value
        assert driver.resized_to is None

    def test_from_dp_volume_backfills_a_missing_storage_pool(self):
        # A volume that predates storage_pool tracking (e.g. loaded from
        # an older meta file) has no pinned pool yet - trust the data
        # plane's own record of where it actually lives.
        volume_uuid = sys_uuid.uuid4()
        meta_volume = pool_driver.MetaVolume(
            uuid=volume_uuid,
            pool=sys_uuid.uuid4(),
            name=str(volume_uuid),
            size=10,
            project_id=sys_uuid.uuid4(),
        )
        assert meta_volume.storage_pool is None

        dp_volume = pool_driver.MachineVolume(
            uuid=volume_uuid,
            name=str(volume_uuid),
            size=10,
            project_id=pool_driver.SYSTEM_PROJECT_ID,
            storage_pool="pool-a",
        )

        meta_volume._from_dp_volume(dp_volume)

        assert meta_volume.storage_pool == "pool-a"


@pytest.mark.parametrize("name", ["duplicate", ""])
def test_selector_rejects_ambiguous_assigned_names_even_if_one_pool_is_full(name):
    first = _make_storage_pool(name, ic.DiskTier.WARM.value, True, 10, 10)
    second = _make_storage_pool(name, ic.DiskTier.WARM.value, True, 100)
    assert (
        pool_driver.select_storage_pool(
            [first, second], ic.DiskTier.WARM.value, True, 1, assigned_name=name
        )
        is None
    )
    assert (
        pool_driver.select_storage_pool(
            [first, second], ic.DiskTier.WARM.value, True, 1
        )
        is None
    )


@pytest.mark.parametrize("cached", [None, "wrong"])
def test_restore_reads_the_actual_storage_pool(cached):
    uid = sys_uuid.uuid4()
    volume = pool_driver.MetaVolume(
        uuid=uid,
        pool=sys_uuid.uuid4(),
        size=10,
        project_id=sys_uuid.uuid4(),
        storage_pool=cached,
    )
    actual = pool_driver.MachineVolume(
        uuid=uid, size=10, project_id=sys_uuid.uuid4(), storage_pool="actual"
    )
    volume.restore_from_dp(mock.Mock(dp_volume_map={uid: actual}))
    assert volume.storage_pool == "actual"


def test_resize_without_pin_charges_only_the_actual_pool():
    uid = sys_uuid.uuid4()
    actual_pool = _make_storage_pool("actual", "COLD", True, 100, 10)
    other_pool = _make_storage_pool("other", "WARM", True, 1000)
    actual = pool_driver.MachineVolume(
        uuid=uid, size=10, project_id=sys_uuid.uuid4(), storage_pool="actual"
    )
    volume = pool_driver.MetaVolume(
        uuid=uid, pool=sys_uuid.uuid4(), size=12, project_id=sys_uuid.uuid4()
    )
    driver = mock.Mock()
    driver.get_volume.return_value = actual
    pool = mock.Mock(
        storage_pools=[actual_pool, other_pool], dp_volume_map={uid: actual}
    )
    pool.load_driver.return_value = driver
    volume.update_on_dp(pool)
    assert actual_pool.capacity_provisioned == 12
    assert other_pool.capacity_provisioned == 0
    assert volume.storage_pool == "actual"


def test_resize_with_unknown_actual_pool_does_not_touch_driver():
    uid = sys_uuid.uuid4()
    actual = pool_driver.MachineVolume(uuid=uid, size=10, project_id=sys_uuid.uuid4())
    volume = pool_driver.MetaVolume(
        uuid=uid, pool=sys_uuid.uuid4(), size=12, project_id=sys_uuid.uuid4()
    )
    driver = mock.Mock()
    pool = mock.Mock(dp_volume_map={uid: actual})
    pool.load_driver.return_value = driver
    volume.update_on_dp(pool)
    driver.resize_volume.assert_not_called()
    assert actual.size == 10
    assert volume.status == "ERROR"


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
        with (
            mock.patch.object(operations, "now", lambda: self.clock_now),
            mock.patch.object(pool_driver.MetaMachine, "__operations__", self.catalog),
        ):
            yield

    def _tick(self, seconds):
        self.clock_now += datetime.timedelta(seconds=seconds)

    def _machine(self, dp_power_state):
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
        meta_pool.dp_port_map = {machine_uuid: ()}
        meta_pool.all_cores = meta_pool.avail_cores = 16
        meta_pool.all_ram = meta_pool.avail_ram = 16384
        driver = _PowerPoolDriver(meta_pool, dp_machine)
        return meta_pool, driver, dp_machine, machine_uuid

    def _iterate(self, meta_pool, driver, machine_uuid, power_state, cores=1):
        """Run an agent iteration for the machine and return the actual state.

        Like the agent, restore the machine from the data plane first and
        update it only if it differs from the target.
        """
        with mock.patch.object(
            pool_driver.MetaPool, "load_driver", return_value=driver
        ):
            actual = pool_driver.MetaMachine(
                uuid=machine_uuid,
                name="vm",
                pool=meta_pool.uuid,
                project_id=sys_uuid.uuid4(),
            )
            actual.restore_from_dp(meta_pool, volumes=())

            power_differs = power_state is not None and (
                power_state != actual.power_state
            )
            if not power_differs and cores == actual.cores:
                return actual.power_state

            target = pool_driver.MetaMachine(
                uuid=machine_uuid,
                name="vm",
                cores=cores,
                ram=1024,
                pool=meta_pool.uuid,
                project_id=sys_uuid.uuid4(),
                power_state=power_state,
            )
            target.update_on_dp(meta_pool, volumes=())
            return target.power_state

    def _op(self, uuid, op_class):
        return self.catalog.get(uuid, op_class)

    def test_power_off_is_graceful_then_forced(self):
        pool, driver, dp_machine, uuid = self._machine("ON")

        assert self._iterate(pool, driver, uuid, "OFF") == "ON"
        assert driver.calls == [("shutdown", False)]

        # The guest is still shutting down, nothing to do
        self._tick(10)
        self._iterate(pool, driver, uuid, "OFF")
        assert driver.calls == [("shutdown", False)]

        # The guest ignored the shutdown
        self._tick(60)
        self._iterate(pool, driver, uuid, "OFF")
        assert driver.calls == [("shutdown", False), ("shutdown", True)]
        op = self._op(uuid, pool_driver.PowerOffMachineOperation)
        assert [r["action"] for r in op.journal] == [
            "started",
            "send_shutdown",
            "send_force_shutdown",
        ]

        # The target is reached, the operation isn't requested anymore
        # and it's dropped in an iteration
        dp_machine.power_state = "OFF"
        assert self._iterate(pool, driver, uuid, "OFF") == "OFF"
        assert self._op(uuid, pool_driver.PowerOffMachineOperation) is not None
        self._iterate(pool, driver, uuid, "OFF")
        assert self._op(uuid, pool_driver.PowerOffMachineOperation) is None

    def test_power_on(self):
        pool, driver, dp_machine, uuid = self._machine("OFF")

        self._iterate(pool, driver, uuid, "ON")
        assert driver.calls == [("start",)]

        dp_machine.power_state = "ON"
        assert self._iterate(pool, driver, uuid, "ON") == "ON"
        assert driver.calls == [("start",)]

    def test_latest_target_wins(self):
        pool, driver, dp_machine, uuid = self._machine("ON")

        self._iterate(pool, driver, uuid, "OFF")
        dp_machine.power_state = "UNKNOWN"
        self._iterate(pool, driver, uuid, "ON")

        assert self._op(uuid, pool_driver.PowerOffMachineOperation) is None
        assert self._op(uuid, pool_driver.PowerOnMachineOperation) is not None
        assert driver.calls == [("shutdown", False), ("start",)]

    def test_power_off_starts_over_after_it_was_dropped(self):
        """The machine was powered off, then started manually. The new
        power off must start with a graceful shutdown rather than continue
        by the old journal.
        """
        pool, driver, dp_machine, uuid = self._machine("ON")
        self._iterate(pool, driver, uuid, "OFF")
        dp_machine.power_state = "OFF"
        self._iterate(pool, driver, uuid, "OFF")
        self._iterate(pool, driver, uuid, "OFF")
        assert self._op(uuid, pool_driver.PowerOffMachineOperation) is None

        # Started manually an hour later
        self._tick(3600)
        dp_machine.power_state = "ON"
        driver.calls.clear()
        self._iterate(pool, driver, uuid, "OFF")

        assert driver.calls == [("shutdown", False)]

    def test_reboot_is_shutdown_and_start(self):
        pool, driver, dp_machine, uuid = self._machine("ON")

        # Intermediate states of the machine are hidden behind REBOOTING
        assert self._iterate(pool, driver, uuid, "REBOOT") == "REBOOTING"
        assert driver.calls == [("shutdown", False)]

        dp_machine.power_state = "OFF"
        assert self._iterate(pool, driver, uuid, "REBOOT") == "REBOOTING"
        assert driver.calls == [("shutdown", False), ("start",)]

        dp_machine.power_state = "ON"
        assert self._iterate(pool, driver, uuid, "REBOOT") == "REBOOTED"

    def test_finished_reboot_is_kept_while_requested(self):
        pool, driver, dp_machine, uuid = self._machine("OFF")
        self._iterate(pool, driver, uuid, "REBOOT")
        dp_machine.power_state = "ON"
        self._iterate(pool, driver, uuid, "REBOOT")

        # The control plane hasn't switched the target back to `ON` yet
        for _ in range(5):
            self._tick(600)
            assert self._iterate(pool, driver, uuid, "REBOOT") == "REBOOTED"

        assert driver.calls == [("start",)]

    def test_finished_reboot_is_dropped_once_target_is_on(self):
        pool, driver, dp_machine, uuid = self._machine("OFF")
        self._iterate(pool, driver, uuid, "REBOOT")
        dp_machine.power_state = "ON"
        self._iterate(pool, driver, uuid, "REBOOT")

        # Nothing to do for the target `ON`, the reboot isn't requested
        # anymore and it's dropped in an iteration
        assert self._iterate(pool, driver, uuid, "ON") == "REBOOTED"
        assert self._iterate(pool, driver, uuid, "ON") == "ON"
        assert driver.calls == [("start",)]

        # The next reboot starts at once
        driver.calls.clear()
        assert self._iterate(pool, driver, uuid, "REBOOT") == "REBOOTING"
        assert driver.calls == [("shutdown", False)]

    def test_reboot_of_powered_off_machine_starts_it(self):
        pool, driver, dp_machine, uuid = self._machine("OFF")

        self._iterate(pool, driver, uuid, "REBOOT")
        assert driver.calls == [("start",)]

        dp_machine.power_state = "ON"
        assert self._iterate(pool, driver, uuid, "REBOOT") == "REBOOTED"

    def test_target_off_in_the_middle_of_reboot(self):
        pool, driver, dp_machine, uuid = self._machine("ON")
        self._iterate(pool, driver, uuid, "REBOOT")
        dp_machine.power_state = "OFF"

        # The machine is already off, so nothing is done and the reboot
        # is dropped in an iteration
        assert self._iterate(pool, driver, uuid, "OFF") == "REBOOTING"
        assert self._iterate(pool, driver, uuid, "OFF") == "OFF"
        assert self._op(uuid, pool_driver.PowerOffMachineOperation) is None
        assert driver.calls == [("shutdown", False)]

    def test_target_on_in_the_middle_of_reboot(self):
        pool, driver, dp_machine, uuid = self._machine("ON")
        self._iterate(pool, driver, uuid, "REBOOT")
        dp_machine.power_state = "OFF"

        self._iterate(pool, driver, uuid, "ON")

        assert self._op(uuid, pool_driver.RebootMachineOperation) is None
        assert driver.calls == [("shutdown", False), ("start",)]

    def test_no_power_management_without_target(self):
        pool, driver, dp_machine, uuid = self._machine("OFF")

        actual = self._iterate(pool, driver, uuid, None, cores=2)

        assert driver.calls == [("cores", 2)]
        assert actual == "OFF"

    def test_other_changes_wait_for_power_operation(self):
        pool, driver, dp_machine, uuid = self._machine("OFF")

        self._iterate(pool, driver, uuid, "ON", cores=2)
        assert driver.calls == [("start",)]

        dp_machine.power_state = "ON"
        self._iterate(pool, driver, uuid, "ON", cores=2)
        assert driver.calls == [("start",), ("cores", 2)]

    def test_powered_off_machine_is_updated_then_powered_off_again(self):
        pool, driver, dp_machine, uuid = self._machine("OFF")

        self._iterate(pool, driver, uuid, "OFF", cores=2)
        assert driver.calls == [("cores", 2)]

        # Recreation of the domain started the machine
        dp_machine.cores = 2
        dp_machine.power_state = "ON"
        self._iterate(pool, driver, uuid, "OFF", cores=2)

        assert driver.calls == [("cores", 2), ("shutdown", False)]

    def test_vanished_machine_drops_its_operations(self):
        pool, driver, dp_machine, uuid = self._machine("ON")
        self._iterate(pool, driver, uuid, "OFF")
        pool.dp_machine_map = {}

        for _ in range(2):
            listed = pool_driver.MetaMachine(
                uuid=uuid, name="vm", pool=pool.uuid, project_id=sys_uuid.uuid4()
            )
            with pytest.raises(pool_driver.ua_driver_exc.ResourceNotFound):
                listed.restore_from_dp(pool, volumes=())

        assert self._op(uuid, pool_driver.PowerOffMachineOperation) is None

    @pytest.mark.parametrize(
        ("target", "expected"),
        [("ON", "ON"), ("OFF", "ON"), ("REBOOT", "ON"), (None, None)],
    )
    def test_created_machine_reports_actual_power_state(self, target, expected):
        pool, driver, dp_machine, uuid = self._machine("ON")
        machine = pool_driver.MetaMachine(
            uuid=uuid,
            name="vm",
            pool=pool.uuid,
            project_id=sys_uuid.uuid4(),
            power_state=target,
        )

        machine._create_machine(driver, dp_machine, (), ())

        assert machine.power_state == expected
