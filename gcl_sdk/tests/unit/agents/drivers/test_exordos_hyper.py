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

import subprocess
import uuid as sys_uuid

import pytest

# The driver imports `libvirt` and `rawstor` python bindings at module
# level. They ship as optional extras (not installed in every dev/CI
# environment), so skip this module instead of failing collection.
pytest.importorskip("libvirt")
pytest.importorskip("rawstor")

from gcl_sdk.agents.universal.drivers import exordos_hyper  # noqa: E402
from gcl_sdk.agents.universal.drivers import pool as pool_base  # noqa: E402


def _driver(tmp_path, node=None) -> exordos_hyper.ExordosLocalHyperDriver:
    # libvirt's built-in "test" driver simulates a hypervisor in-memory -
    # no real virtualization or daemon needed. rawstor's "file://" location
    # is a real, local, daemon-less backend (see pyrawstor/tests).
    spec = pool_base.ExordosLocalHyperDriverSpec(
        connection_uri="test:///default",
        node=node or sys_uuid.uuid4(),
        rawstor_location=f"file://{tmp_path}",
        rawstor_capacity_gb=100,
    )
    pool = pool_base.MachinePool(
        uuid=sys_uuid.uuid4(), name="rawstor-pool", driver_spec=spec
    )
    return exordos_hyper.ExordosLocalHyperDriver(pool)


def _no_op_systemctl(monkeypatch):
    """Record systemctl invocations instead of running the real binary."""
    calls = []

    def fake_check_call(cmd, *a, **kw):
        calls.append(cmd)
        return 0

    monkeypatch.setattr(subprocess, "check_call", fake_check_call)
    return calls


class TestVolumeUuidFromSocketPath:
    def test_target_uri(self, tmp_path):
        driver = _driver(tmp_path)
        volume_uuid = sys_uuid.uuid4()
        assert driver._uuid_from_socket_path(f"{driver._location.uri}/{volume_uuid}") == (
            volume_uuid
        )

    def test_socket_path(self, tmp_path):
        driver = _driver(tmp_path)
        volume_uuid = sys_uuid.uuid4()
        assert driver._uuid_from_socket_path(driver._socket_path(volume_uuid)) == (
            volume_uuid
        )

    def test_garbage_returns_none(self, tmp_path):
        driver = _driver(tmp_path)
        assert driver._uuid_from_socket_path("/run/rawstor/not-a-uuid.sock") is None


class TestBuildStoragePool:
    def test_capacity_is_hardcoded_from_spec(self, tmp_path):
        # rawstor has no capacity/stats API - the pool's usable capacity
        # is a fixed value supplied by exordos, not queried dynamically.
        driver = _driver(tmp_path)
        storage_pool = driver._build_storage_pool([])

        assert storage_pool.capacity_usable == 100
        assert storage_pool.pool_type == "rawstor"
        assert storage_pool.available == 100

    def test_existing_volumes_reduce_available_capacity(self, tmp_path):
        driver = _driver(tmp_path)
        volumes = [
            pool_base.MachineVolume(
                uuid=sys_uuid.uuid4(),
                project_id=sys_uuid.uuid4(),
                size=10,
            ),
            pool_base.MachineVolume(
                uuid=sys_uuid.uuid4(),
                project_id=sys_uuid.uuid4(),
                size=15,
            ),
        ]

        storage_pool = driver._build_storage_pool(volumes)

        assert storage_pool.available == 100 - 10 - 15


class TestVolumeLifecycle:
    def test_create_get_list_delete(self, tmp_path, monkeypatch):
        _no_op_systemctl(monkeypatch)
        driver = _driver(tmp_path)

        volume = pool_base.MachineVolume(
            uuid=sys_uuid.uuid4(),
            project_id=sys_uuid.uuid4(),
            size=1,
        )

        created = driver.create_volume(volume)
        assert created.status == pool_base.VolumeStatus.ACTIVE.value

        fetched = driver.get_volume(volume.uuid)
        assert fetched.uuid == volume.uuid
        assert fetched.size == 1
        assert fetched.machine is None

        listed = driver.list_volumes()
        assert [v.uuid for v in listed] == [volume.uuid]

        driver.delete_volume(created)

        with pytest.raises(pool_base.VolumeNotFoundError):
            driver.get_volume(volume.uuid)

    def test_create_twice_raises_already_exists(self, tmp_path, monkeypatch):
        _no_op_systemctl(monkeypatch)
        driver = _driver(tmp_path)

        volume = pool_base.MachineVolume(
            uuid=sys_uuid.uuid4(), project_id=sys_uuid.uuid4(), size=1
        )
        driver.create_volume(volume)

        with pytest.raises(pool_base.VolumeAlreadyExistsError):
            driver.create_volume(volume)

    def test_get_missing_volume_raises_not_found(self, tmp_path):
        driver = _driver(tmp_path)

        with pytest.raises(pool_base.VolumeNotFoundError):
            driver.get_volume(sys_uuid.uuid4())

    def test_delete_missing_volume_is_idempotent(self, tmp_path, monkeypatch):
        _no_op_systemctl(monkeypatch)
        driver = _driver(tmp_path)

        volume = pool_base.MachineVolume(
            uuid=sys_uuid.uuid4(), project_id=sys_uuid.uuid4(), size=1
        )

        # Must not raise, even though it was never created.
        driver.delete_volume(volume)

    def test_delete_stops_vhost_before_removing_the_object(self, tmp_path, monkeypatch):
        # rawstor-vhost is an attachment of the volume: deleting the
        # object without stopping it first would leave a dangling
        # backend process.
        calls = _no_op_systemctl(monkeypatch)
        driver = _driver(tmp_path)

        volume = pool_base.MachineVolume(
            uuid=sys_uuid.uuid4(), project_id=sys_uuid.uuid4(), size=1
        )
        driver.create_volume(volume)
        calls.clear()

        driver.delete_volume(volume)

        assert calls == [
            ["systemctl", "disable", "--now", f"rawstor-vhost@{volume.uuid}"]
        ]


class TestDeleteMachine:
    def test_removes_all_volumes_and_stops_their_vhost_units(
        self, tmp_path, monkeypatch
    ):
        # Regression: volume-to-machine attribution is read from the
        # domain's own XML, so deleting all the machine's volumes must
        # happen before the domain is undefined - otherwise the volumes
        # (and their still-running rawstor-vhost units) are silently
        # orphaned.
        calls = _no_op_systemctl(monkeypatch)
        driver = _driver(tmp_path)
        monkeypatch.setattr(driver, "_wait_for_socket", lambda socket_path: None)

        machine = pool_base.Machine(
            uuid=sys_uuid.uuid4(),
            project_id=sys_uuid.uuid4(),
            name="vm1",
            cores=1,
            ram=512,
        )
        root_vol = pool_base.MachineVolume(
            uuid=sys_uuid.uuid4(),
            project_id=sys_uuid.uuid4(),
            size=1,
            index=0,
            machine=machine.uuid,
        )
        data_vol = pool_base.MachineVolume(
            uuid=sys_uuid.uuid4(),
            project_id=sys_uuid.uuid4(),
            size=2,
            index=1,
            machine=machine.uuid,
        )
        for v in (root_vol, data_vol):
            driver.create_volume(v)

        port = pool_base.Port(
            uuid=sys_uuid.uuid4(),
            project_id=sys_uuid.uuid4(),
            mac="52:54:00:11:22:33",
            source="default",
            status="ACTIVE",
        )
        driver.create_machine(machine, [root_vol, data_vol], [port])
        calls.clear()

        driver.delete_machine(machine, delete_volumes=True)

        assert list(driver.list_volumes()) == []
        stopped_units = {
            c[-1] for c in calls if c[:3] == ["systemctl", "disable", "--now"]
        }
        assert stopped_units == {
            f"rawstor-vhost@{root_vol.uuid}",
            f"rawstor-vhost@{data_vol.uuid}",
        }


class TestResizeVolume:
    def test_raises_not_supported(self, tmp_path):
        driver = _driver(tmp_path)
        volume = pool_base.MachineVolume(
            uuid=sys_uuid.uuid4(), project_id=sys_uuid.uuid4(), size=1
        )

        with pytest.raises(pool_base.VolumeResizeNotSupportedError):
            driver.resize_volume(volume)
