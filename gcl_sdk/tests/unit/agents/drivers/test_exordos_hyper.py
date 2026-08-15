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
import types
import uuid as sys_uuid
from xml.etree import ElementTree as ET

import pytest

# The driver imports `libvirt` and `rawstor` python bindings at module
# level. They ship as optional extras (not installed in every dev/CI
# environment), so skip this module instead of failing collection.
pytest.importorskip("libvirt")
pytest.importorskip("rawstor")

from gcl_sdk.agents.universal.drivers import exordos_hyper  # noqa: E402
from gcl_sdk.agents.universal.drivers import libvirt as libvirt_driver  # noqa: E402
from gcl_sdk.agents.universal.drivers import pool as pool_base  # noqa: E402


def _driver(
    tmp_path, node=None, storage_pool=None
) -> exordos_hyper.ExordosLocalHyperDriver:
    # libvirt's built-in "test" driver simulates a hypervisor in-memory -
    # no real virtualization or daemon needed. rawstor's "file://" location
    # is a real, local, daemon-less backend (see pyrawstor/tests).
    spec = pool_base.ExordosLocalHyperDriverSpec(
        connection_uri="test:///default",
        node=node or sys_uuid.uuid4(),
        rawstor_location=f"file://{tmp_path}",
        storage_pool=storage_pool,
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


def _fake_location_info(monkeypatch, driver, *, used_gb, total_gb):
    monkeypatch.setattr(
        driver._location,
        "info",
        lambda: types.SimpleNamespace(used=used_gb << 30, total=total_gb << 30),
    )


class TestBuildStoragePool:
    def test_capacity_comes_from_location_info(self, tmp_path, monkeypatch):
        # rawstor 0.2.4 added Location.info() (used/total bytes for the
        # backend) - the pool's usable capacity reflects that instead of a
        # hardcoded placeholder.
        driver = _driver(tmp_path)
        _fake_location_info(monkeypatch, driver, used_gb=20, total_gb=100)

        storage_pool = driver._build_storage_pool([])

        assert storage_pool.capacity_usable == 100
        assert storage_pool.pool_type == "rawstor"
        assert storage_pool.available == 100
        assert storage_pool.available_actual == 80

    def test_existing_volumes_reduce_available_capacity(self, tmp_path, monkeypatch):
        driver = _driver(tmp_path)
        _fake_location_info(monkeypatch, driver, used_gb=0, total_gb=100)
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

    def test_delete_aborts_if_the_vhost_unit_fails_to_stop(self, tmp_path, monkeypatch):
        # Regression: a swallowed disable failure used to let delete_volume
        # remove the object anyway, orphaning a still-enabled unit behind
        # it - a real failure to stop must abort the delete instead.
        _no_op_systemctl(monkeypatch)
        driver = _driver(tmp_path)
        volume = pool_base.MachineVolume(
            uuid=sys_uuid.uuid4(), project_id=sys_uuid.uuid4(), size=1
        )
        driver.create_volume(volume)

        def fake_check_call(cmd, *a, **kw):
            raise subprocess.CalledProcessError(1, cmd)

        monkeypatch.setattr(subprocess, "check_call", fake_check_call)

        with pytest.raises(subprocess.CalledProcessError):
            driver.delete_volume(volume)

        # The object must still exist - the delete never got past the
        # failed vhost stop.
        assert driver.get_volume(volume.uuid).uuid == volume.uuid


class TestForeignVolumes:
    """A machine can be adopted into this pool with disks that were never
    created by this driver - e.g. the core bootstrap VM, whose qcow2
    disks are created directly by the CLI before this pool exists. The
    driver must recognize them as already satisfied rather than trying to
    create a rawstor volume and vhost-attach it on top of them.
    """

    def _foreign_machine_and_volume(self, storage_pool_name):
        # Built through the plain LibvirtPoolDriver - matches how the CLI's
        # LibvirtInfraDriver.create_stand provisions the bootstrap VM,
        # entirely independent of this pool/driver.
        #
        # The returned driver must be kept alive by the caller: libvirt's
        # "test:///default" backend only keeps its in-memory state around
        # while at least one connection to it is open - if `base_driver`
        # (and its connection) gets garbage collected, the domain/volume
        # created below disappear before a second driver can find them.
        base_spec = pool_base.LibvirtPoolDriverSpec(
            connection_uri="test:///default", storage_pool=storage_pool_name
        )
        base_pool = pool_base.MachinePool(
            uuid=sys_uuid.uuid4(), name="plain-pool", driver_spec=base_spec
        )
        base_driver = libvirt_driver.LibvirtPoolDriver(base_pool)

        machine = pool_base.Machine(
            uuid=sys_uuid.uuid4(),
            project_id=sys_uuid.uuid4(),
            name="vm-exordos-core-bootstrap",
            cores=1,
            ram=512,
        )
        volume_uuid = sys_uuid.uuid4()
        volume = pool_base.MachineVolume(
            uuid=volume_uuid,
            project_id=sys_uuid.uuid4(),
            size=1,
            index=0,
            machine=machine.uuid,
            name=str(volume_uuid),
        )
        volume = base_driver.create_volume(volume)
        port = pool_base.Port(
            uuid=sys_uuid.uuid4(),
            project_id=sys_uuid.uuid4(),
            mac="52:54:00:11:22:33",
            source="default",
            status="ACTIVE",
        )
        base_driver.create_machine(machine, [volume], [port])

        return base_driver, machine, volume

    def test_get_volume_recognizes_a_foreign_qcow2_disk(self, tmp_path):
        base_driver, machine, volume = self._foreign_machine_and_volume(
            "default-pool"
        )
        driver = _driver(tmp_path, storage_pool="default-pool")

        found = driver.get_volume(volume.uuid)

        assert found.machine == machine.uuid
        assert found.size == volume.size
        assert base_driver is not None  # keep the connection alive until here

    def test_list_volumes_includes_a_foreign_qcow2_disk(self, tmp_path):
        base_driver, _, volume = self._foreign_machine_and_volume("default-pool")
        driver = _driver(tmp_path, storage_pool="default-pool")

        assert volume.uuid in {v.uuid for v in driver.list_volumes()}
        assert base_driver is not None  # keep the connection alive until here

    def test_attach_volume_is_a_noop_for_an_already_present_foreign_disk(
        self, tmp_path
    ):
        # Regression: attaching would build a vhostuser disk and hotplug
        # it onto a domain that already has a plain qcow2 disk in that
        # slot and no shared-memory backing - libvirt then refuses with
        # "'vhostuser' requires shared memory".
        base_driver, _, volume = self._foreign_machine_and_volume("default-pool")
        driver = _driver(tmp_path, storage_pool="default-pool")

        with pytest.raises(pool_base.VolumeAlreadyAttachedError):
            driver.attach_volume(volume)
        assert base_driver is not None  # keep the connection alive until here

    def test_without_a_storage_pool_configured_no_foreign_volumes_are_found(
        self, tmp_path
    ):
        # exordos_local_hyper deployments without --with-rawstor's core
        # bootstrap VM (e.g. `hypervisors init`) never configure
        # storage_pool - must not crash trying to look one up.
        driver = _driver(tmp_path)

        assert driver.list_volumes() == []


class TestCreateMachine:
    def test_domain_is_defined_with_shared_memory(self, tmp_path, monkeypatch):
        # Regression: libvirt refuses to attach a vhostuser disk to a
        # domain that wasn't defined with shared memory backing, and it
        # can't be added after the fact - so every machine of this pool
        # (all of them vhost-user-backed) must get it at create time.
        _no_op_systemctl(monkeypatch)
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
        driver.create_volume(root_vol)

        driver.create_machine(machine, [root_vol], [])

        domain = driver._client.lookupByUUIDString(str(machine.uuid))
        memory_backing = ET.fromstring(domain.XMLDesc()).find("memoryBacking")
        assert memory_backing is not None
        assert memory_backing.find("access").get("mode") == "shared"


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
