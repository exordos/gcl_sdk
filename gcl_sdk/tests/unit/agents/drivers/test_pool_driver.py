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

from unittest import mock
import uuid as sys_uuid

import pytest

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


def _make_storage_pool(name, speed, ephemeral, capacity_usable, capacity_provisioned=0):
    return pool_driver.ThinStoragePool(
        name=name,
        pool_type="dir",
        speed=speed,
        ephemeral=ephemeral,
        capacity_usable=capacity_usable,
        capacity_provisioned=capacity_provisioned,
    )


class TestSelectStoragePool:
    """The soft match picks the most free-space pool, not the first one."""

    def test_exact_match_picks_the_one_with_most_free_space(self):
        small_hot = _make_storage_pool("small-hot", ic.DiskSpeed.HOT.value, False, 20)
        big_hot = _make_storage_pool("big-hot", ic.DiskSpeed.HOT.value, False, 100)
        warm = _make_storage_pool("warm", ic.DiskSpeed.WARM.value, False, 1000)

        selected = pool_driver.select_storage_pool(
            [small_hot, big_hot, warm], ic.DiskSpeed.HOT.value, False, 10
        )

        assert selected is big_hot

    def test_fallback_match_picks_the_one_with_most_free_space(self):
        # Neither pool is hot, so this falls back to the largest pool with
        # room, rather than whichever happens to be listed first.
        small = _make_storage_pool("small", ic.DiskSpeed.WARM.value, False, 20)
        big = _make_storage_pool("big", ic.DiskSpeed.COLD.value, False, 100)

        selected = pool_driver.select_storage_pool(
            [small, big], ic.DiskSpeed.HOT.value, False, 10
        )

        assert selected is big

    def test_fuller_exact_match_loses_to_emptier_one(self):
        full_hot = _make_storage_pool(
            "full-hot", ic.DiskSpeed.HOT.value, False, 100, capacity_provisioned=95
        )
        empty_hot = _make_storage_pool("empty-hot", ic.DiskSpeed.HOT.value, False, 100)

        selected = pool_driver.select_storage_pool(
            [full_hot, empty_hot], ic.DiskSpeed.HOT.value, False, 10
        )

        assert selected is empty_hot

    def test_assigned_name_ignores_weight(self):
        assigned = _make_storage_pool("assigned", ic.DiskSpeed.WARM.value, False, 20)
        bigger = _make_storage_pool("bigger", ic.DiskSpeed.WARM.value, False, 100)

        selected = pool_driver.select_storage_pool(
            [assigned, bigger],
            ic.DiskSpeed.WARM.value,
            False,
            10,
            assigned_name="assigned",
        )

        assert selected is assigned

    def test_no_pool_fits(self):
        small = _make_storage_pool("small", ic.DiskSpeed.WARM.value, False, 5)

        selected = pool_driver.select_storage_pool(
            [small], ic.DiskSpeed.WARM.value, False, 10
        )

        assert selected is None

    def test_never_places_a_durable_request_on_an_ephemeral_pool(self):
        # Unlike speed, ephemeral is a data-safety property: falling
        # back across it could silently wipe a "durable" disk on host
        # reboot, so the durable request must go unscheduled (retried
        # later) rather than land on the ephemeral pool with room.
        ephemeral_pool = _make_storage_pool(
            "ephemeral", ic.DiskSpeed.HOT.value, True, 100
        )

        selected = pool_driver.select_storage_pool(
            [ephemeral_pool], ic.DiskSpeed.HOT.value, False, 10
        )

        assert selected is None

    def test_never_places_an_ephemeral_request_on_a_durable_pool(self):
        durable_pool = _make_storage_pool("durable", ic.DiskSpeed.HOT.value, False, 100)

        selected = pool_driver.select_storage_pool(
            [durable_pool], ic.DiskSpeed.HOT.value, True, 10
        )

        assert selected is None

    def test_speed_still_falls_back_within_the_matching_ephemeral_tier(self):
        cold_ephemeral = _make_storage_pool(
            "cold-ephemeral", ic.DiskSpeed.COLD.value, True, 100
        )
        hot_durable = _make_storage_pool("hot-durable", ic.DiskSpeed.HOT.value, False, 100)

        # Requesting hot+ephemeral: no exact match, but the ephemeral
        # pool (wrong speed) is still preferred over the durable one
        # (wrong ephemeral) - the speed fallback only crosses within the
        # same durability tier.
        selected = pool_driver.select_storage_pool(
            [cold_ephemeral, hot_durable], ic.DiskSpeed.HOT.value, True, 10
        )

        assert selected is cold_ephemeral


class TestExordosLocalHyperDriverSpecStoragePoolCompat:
    """`storage_pool` also accepts a bare pool name string, so a gcl_sdk
    upgrade doesn't require exordos_core to move to the named-pool list
    format at the same time.
    """

    def _spec(self, storage_pool):
        return pool_driver.ExordosLocalHyperDriverSpec(
            connection_uri="qemu:///system",
            node=sys_uuid.uuid4(),
            storage_pool=storage_pool,
        )

    def test_legacy_string_round_trips_unchanged(self):
        spec = self._spec("default-pool")

        assert spec.storage_pool == "default-pool"
        assert spec.dump_to_simple_view()["storage_pool"] == "default-pool"

        restored = pool_driver.ExordosLocalHyperDriverSpec.restore_from_simple_view(
            **spec.dump_to_simple_view()
        )
        assert restored.storage_pool == "default-pool"

    def test_new_list_format_round_trips(self):
        entries = [
            {"name": "hot-pool", "speed": ic.DiskSpeed.HOT.value, "ephemeral": True}
        ]
        spec = self._spec(entries)

        assert spec.storage_pool == entries
        assert spec.dump_to_simple_view()["storage_pool"] == entries

        restored = pool_driver.ExordosLocalHyperDriverSpec.restore_from_simple_view(
            **spec.dump_to_simple_view()
        )
        assert restored.storage_pool == entries

    def test_invalid_shapes_are_rejected(self):
        with pytest.raises(Exception):
            self._spec(123)
        with pytest.raises(Exception):
            self._spec(
                [{"speed": ic.DiskSpeed.HOT.value}]
            )  # missing mandatory "name"

    def _storage_pool_type(self):
        return (
            self._spec("default-pool")
            .properties.properties["storage_pool"]
            .get_property_type()
        )

    def test_from_unicode_rejects_a_malformed_list_instead_of_treating_it_as_a_name(
        self,
    ):
        # Regression: a list-shaped value that fails validation used to
        # be silently downgraded to "it must be a bare legacy pool
        # name", so a malformed new-format config passed validation and
        # was only discovered later as a mysteriously "nonexistent pool".
        field_type = self._storage_pool_type()

        # Missing the mandatory "name" key.
        with pytest.raises(Exception):
            field_type.from_unicode('[{"speed": "HOT"}]')

    def test_from_unicode_still_accepts_a_bare_legacy_name(self):
        field_type = self._storage_pool_type()

        assert field_type.from_unicode("default-pool") == "default-pool"


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
    def test_update_on_dp_ignores_a_conflicting_storage_pool_change(self):
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
