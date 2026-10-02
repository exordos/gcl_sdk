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

import types
import uuid as sys_uuid

import pytest

# rawstor_cluster imports the `rawstor` python bindings at module level.
# They ship as an optional extra (not installed in every dev/CI
# environment), so skip this module instead of failing collection - see
# test_exordos_hyper.py for the same pattern.
pytest.importorskip("rawstor")

import rawstor  # noqa: E402

from gcl_sdk.agents.universal.dm import models as ua_models  # noqa: E402
from gcl_sdk.agents.universal.drivers import pool as pool_base  # noqa: E402
from gcl_sdk.agents.universal.drivers import rawstor_cluster  # noqa: E402
from gcl_sdk.infra import constants as ic  # noqa: E402


def _cluster(tmp_path, speed=ic.DiskSpeed.HOT.value, ephemeral=False):
    # rawstor's "file://" location is a real, local, daemon-less backend
    # (see pyrawstor/tests) - no server needed to exercise it for real.
    spec = rawstor_cluster.RawstorStorageClusterDriverSpec(
        location=f"file://{tmp_path}",
        endpoint="ost://10.0.0.5:7777",
        speed=speed,
        ephemeral=ephemeral,
    )
    return rawstor_cluster.MetaStorageCluster(uuid=sys_uuid.uuid4(), driver_spec=spec)


class _FakeLocation:
    def __init__(self, used_gb, total_gb):
        self._used_gb = used_gb
        self._total_gb = total_gb

    def info(self):
        return types.SimpleNamespace(
            used=self._used_gb << 30, total=self._total_gb << 30
        )

    def __iter__(self):
        return iter(())


def _fake_location_info(monkeypatch, *, used_gb, total_gb):
    # __iter__ must live on the class, not an instance attribute (as a
    # plain SimpleNamespace(__iter__=...) used to try) - Python looks up
    # dunder methods on the type, so `for x in obj` never saw it there.
    monkeypatch.setattr(
        rawstor_cluster.rawstor,
        "Location",
        lambda uri: _FakeLocation(used_gb, total_gb),
    )


class TestRawstorStorageClusterDriver:
    def test_rejects_a_mismatched_driver_spec_kind(self, tmp_path):
        class _OtherSpec(rawstor_cluster.AbstractStorageClusterDriverSpec):
            KIND = "other"

        cluster = rawstor_cluster.MetaStorageCluster(
            uuid=sys_uuid.uuid4(), driver_spec=_OtherSpec()
        )
        with pytest.raises(ValueError):
            rawstor_cluster.RawstorStorageClusterDriver(cluster)

    def test_capacity_usable_comes_from_location_info(self, tmp_path, monkeypatch):
        cluster = _cluster(tmp_path)
        _fake_location_info(monkeypatch, used_gb=20, total_gb=100)

        [storage_pool] = rawstor_cluster.RawstorStorageClusterDriver(
            cluster
        ).get_capacity()

        assert storage_pool.name == "default"
        assert storage_pool.pool_type == "rawstor"
        assert storage_pool.capacity_usable == 100
        assert storage_pool.available_actual == 80

    def test_speed_and_ephemeral_come_from_the_driver_spec(self, tmp_path, monkeypatch):
        cluster = _cluster(tmp_path, speed=ic.DiskSpeed.COLD.value, ephemeral=True)
        _fake_location_info(monkeypatch, used_gb=0, total_gb=100)

        [storage_pool] = rawstor_cluster.RawstorStorageClusterDriver(
            cluster
        ).get_capacity()

        assert storage_pool.speed == ic.DiskSpeed.COLD.value
        assert storage_pool.ephemeral is True

    def test_capacity_provisioned_sums_real_objects_at_the_location(self, tmp_path):
        # No mocking here - real rawstor objects at a real (daemon-less)
        # file:// location, to exercise the actual enumeration path.
        cluster = _cluster(tmp_path)
        location = rawstor.Location(f"file://{tmp_path}")
        location.create(size=10 << 30)
        location.create(size=5 << 30)

        [storage_pool] = rawstor_cluster.RawstorStorageClusterDriver(
            cluster
        ).get_capacity()

        assert storage_pool.capacity_provisioned == 15

    def test_uuid_is_deterministic_per_cluster(self, tmp_path, monkeypatch):
        cluster = _cluster(tmp_path)
        _fake_location_info(monkeypatch, used_gb=0, total_gb=100)
        driver = rawstor_cluster.RawstorStorageClusterDriver(cluster)

        [first] = driver.get_capacity()
        [second] = driver.get_capacity()

        assert first.uuid == second.uuid == sys_uuid.uuid5(cluster.uuid, "default")


class _FakeStorageClusterDriver(rawstor_cluster.AbstractStorageClusterDriver):
    def __init__(self, cluster, pools):
        super().__init__(cluster)
        self._pools = pools

    def get_capacity(self):
        return self._pools


class TestMetaStorageCluster:
    def test_get_meta_model_fields_only_keeps_uuid_and_driver_spec(self, tmp_path):
        cluster = _cluster(tmp_path)
        assert cluster.get_meta_model_fields() == {"uuid", "driver_spec"}

    def test_restore_from_dp_refreshes_storage_pools_and_status(
        self, tmp_path, monkeypatch
    ):
        cluster = _cluster(tmp_path)
        pool = pool_base.ThinStoragePool(
            name="default", pool_type="rawstor", capacity_usable=50
        )
        monkeypatch.setattr(
            cluster, "load_driver", lambda: _FakeStorageClusterDriver(cluster, [pool])
        )

        cluster.restore_from_dp()

        assert cluster.storage_pools == [pool]
        assert cluster.status == pool_base.MachinePoolStatus.ACTIVE.value

    def test_dump_to_dp_also_reports_capacity(self, tmp_path, monkeypatch):
        cluster = _cluster(tmp_path)
        pool = pool_base.ThinStoragePool(
            name="default", pool_type="rawstor", capacity_usable=50
        )
        monkeypatch.setattr(
            cluster, "load_driver", lambda: _FakeStorageClusterDriver(cluster, [pool])
        )

        cluster.dump_to_dp()

        assert cluster.storage_pools == [pool]


class TestStorageClusterAgentDriver:
    def test_create_and_list_round_trip(self, tmp_path, monkeypatch):
        pool = pool_base.ThinStoragePool(
            name="default", pool_type="rawstor", capacity_usable=50
        )
        monkeypatch.setattr(
            rawstor_cluster.MetaStorageCluster,
            "load_driver",
            lambda self: _FakeStorageClusterDriver(self, [pool]),
        )

        meta_file = str(tmp_path / "meta.json")
        driver = rawstor_cluster.StorageClusterAgentDriver(meta_file=meta_file)
        assert driver.get_capabilities() == ["storage_cluster"]

        driver.start()
        cluster_uuid = sys_uuid.uuid4()
        resource = ua_models.Resource(
            uuid=cluster_uuid,
            kind="storage_cluster",
            value={
                "uuid": str(cluster_uuid),
                "driver_spec": {
                    "kind": "rawstor",
                    "location": f"file://{tmp_path}",
                    "endpoint": "ost://10.0.0.5:7777",
                    "speed": ic.DiskSpeed.HOT.value,
                    "ephemeral": False,
                },
            },
        )
        created = driver.create(resource)
        driver.finalize()

        assert created.value["status"] == pool_base.MachinePoolStatus.ACTIVE.value
        assert created.value["storage_pools"][0]["name"] == "default"

        driver2 = rawstor_cluster.StorageClusterAgentDriver(meta_file=meta_file)
        driver2.start()
        [listed] = driver2.list("storage_cluster")
        assert listed.value["uuid"] == str(cluster_uuid)
