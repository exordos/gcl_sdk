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
