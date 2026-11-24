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
from types import SimpleNamespace
from unittest.mock import MagicMock
import uuid as sys_uuid
from xml.etree import ElementTree as ET

import pytest

# The driver imports `libvirt` and `rawstor` python bindings at module
# level. They ship as optional extras (not installed in every dev/CI
# environment), so skip this module instead of failing collection.
pytest.importorskip("libvirt")
pytest.importorskip("rawstor").initialize(insecure=True)

from gcl_sdk.agents.universal.drivers import exordos_hyper  # noqa: E402
from gcl_sdk.agents.universal.drivers import libvirt as libvirt_driver  # noqa: E402
from gcl_sdk.agents.universal.drivers import pool as pool_base  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_tls_delivery(monkeypatch):
    from gcl_sdk.agents.universal.drivers import rawstor_tls

    monkeypatch.setattr(rawstor_tls, "Bindings", lambda _: exordos_hyper.rawstor)
    monkeypatch.setattr(
        exordos_hyper.ExordosLocalHyperDriver,
        "configure_rawstor_tls",
        lambda *args: None,
    )


def _driver(
    tmp_path, node=None, storage_pool=None
) -> exordos_hyper.ExordosLocalHyperDriver:
    # libvirt's built-in "test" driver simulates a hypervisor in-memory -
    # no real virtualization or daemon needed. This hypervisor has no
    # rawstor backing store of its own - a rawstor-backed volume is only
    # ever one scheduled onto a remote StorageCluster, addressed via
    # `volume.storage_location` (see _rawstor_location below).
    spec = pool_base.ExordosLocalHyperDriverSpec(
        connection_uri="test:///default",
        node=node or sys_uuid.uuid4(),
        storage_pool=[{"name": storage_pool}]
        if isinstance(storage_pool, str)
        else (storage_pool or []),
    )
    pool = pool_base.MachinePool(
        uuid=sys_uuid.uuid4(), name="rawstor-pool", driver_spec=spec
    )
    driver = exordos_hyper.ExordosLocalHyperDriver(pool)
    driver._vhost_drop_in_dir = lambda identifier: str(
        tmp_path / "drop-ins" / f"rawstor-vhost@{identifier}.service.d"
    )
    return driver


@pytest.fixture
def file_objects(monkeypatch):
    """Represent objects already provisioned by the Core agent."""
    targets = {}
    original = exordos_hyper.rawstor.Target
    monkeypatch.setattr(
        exordos_hyper.rawstor,
        "Target",
        lambda uri: targets[uri] if uri in targets else original(uri),
    )

    def provision(driver, volume):
        uri = driver._target_uri(volume.uuid, driver._rawstor_address(volume))
        target = MagicMock(uri=uri, account=str(volume.project_id))
        target.spec.return_value = SimpleNamespace(
            size=volume.size << 30, chunk_size=1 << 30
        )
        target.meta.return_value = [SimpleNamespace(account=str(volume.project_id))]
        targets[uri] = target
        return target

    return provision


def _rawstor_location(tmp_path) -> str:
    # A real, local, daemon-less rawstor backend (see pyrawstor/tests) -
    # stands in for the address a remote StorageCluster would carry.
    return f"file://{tmp_path}"


def _redirect_vhost_drop_in(monkeypatch, driver, tmp_path):
    """Keep the vhost drop-in file under tmp_path, not /etc/systemd/system."""
    drop_in_root = tmp_path / "drop-ins"
    monkeypatch.setattr(
        driver,
        "_vhost_drop_in_dir",
        lambda volume_uuid: str(
            drop_in_root
            / f"{exordos_hyper.VHOST_UNIT_TEMPLATE.format(volume_uuid)}.service.d"
        ),
    )


def _no_op_systemctl(monkeypatch):
    """Record systemctl invocations instead of running the real binary."""
    calls = []

    def fake_check_call(cmd, *a, **kw):
        calls.append(cmd)
        return 0

    monkeypatch.setattr(subprocess, "check_call", fake_check_call)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout="loaded\n"),
    )
    return calls


class TestVolumeUuidFromSocketPath:
    def test_target_uri(self, tmp_path):
        driver = _driver(tmp_path)
        volume_uuid = sys_uuid.uuid4()
        target_uri = driver._target_uri(volume_uuid, _rawstor_location(tmp_path))
        assert driver._uuid_from_socket_path(target_uri) == volume_uuid

    def test_socket_path(self, tmp_path):
        driver = _driver(tmp_path)
        volume_uuid = sys_uuid.uuid4()
        assert driver._uuid_from_socket_path(driver._socket_path(volume_uuid)) == (
            volume_uuid
        )

    def test_garbage_returns_none(self, tmp_path):
        driver = _driver(tmp_path)
        assert driver._uuid_from_socket_path("/run/rawstor/not-a-uuid.sock") is None


class TestRawstorAttachments:
    def test_index_counts_all_disks_not_just_vhostuser_ones(self, tmp_path):
        # Regression: a machine mixing a qcow2 disk and a rawstor
        # (vhostuser) disk must report each disk's index as its position
        # among *all* disks (matching the device letter
        # _add_volumes_to_domain gave it), not its position among disks
        # of its own backend alone - the qcow2 disk here occupies vda,
        # so the vhostuser disk at vdb must be index 1, not 0.
        driver = _driver(tmp_path)
        volume_uuid = sys_uuid.uuid4()

        root = ET.fromstring(
            f"""
            <domain>
              <devices>
                <disk type="file" device="disk">
                  <source file="/var/lib/libvirt/images/other.qcow2" />
                  <target dev="vda" bus="virtio" />
                </disk>
                <disk type="vhostuser" device="disk">
                  <source type="unix"
                          path="/run/rawstor/{volume_uuid}.sock" />
                  <target dev="vdb" bus="virtio" />
                </disk>
              </devices>
            </domain>
            """
        )

        attachments = driver._rawstor_attachments([("fake-domain", root)])

        assert attachments[volume_uuid] == ("fake-domain", 1)


class TestVolumeLifecycle:
    def test_mds_volume_attaches_existing_object(self, tmp_path, monkeypatch):
        _no_op_systemctl(monkeypatch)
        driver = _driver(tmp_path)
        _redirect_vhost_drop_in(monkeypatch, driver, tmp_path)
        volume = pool_base.MachineVolume(
            uuid=sys_uuid.uuid4(),
            project_id=sys_uuid.uuid4(),
            size=2,
            storage_location="mds://10.20.0.2:7776/",
        )
        target = MagicMock()
        target.spec.return_value = SimpleNamespace(size=2 << 30)
        factory = MagicMock(return_value=target)
        monkeypatch.setattr(exordos_hyper.rawstor, "Target", factory)
        registry = MagicMock()
        monkeypatch.setattr(exordos_hyper.rawstor, "Location", lambda uri: registry)
        created = driver.create_volume(volume)
        registry.create_account.assert_not_called()
        factory.assert_called_once_with(
            f"mds://{volume.project_id}@10.20.0.2:7776/{volume.uuid}"
        )
        target.create.assert_not_called()
        assert created.storage_location == "mds://10.20.0.2:7776/"
        assert (
            driver._read_remote_location(volume.uuid)
            == f"mds://{volume.project_id}@10.20.0.2:7776/"
        )
        with open(driver._vhost_drop_in_path(volume.uuid)) as f:
            assert (
                f'Environment=LOCATION=mds://{volume.project_id}@10.20.0.2:7776\nEnvironment="EXTRA_ARGS="\n'
                in f.read()
            )

    def test_hypervisor_does_not_apply_storage_policy(self, tmp_path, monkeypatch):
        _no_op_systemctl(monkeypatch)
        driver = _driver(tmp_path)
        _redirect_vhost_drop_in(monkeypatch, driver, tmp_path)
        volume = pool_base.MachineVolume(
            uuid=sys_uuid.uuid4(),
            project_id=sys_uuid.uuid4(),
            size=2,
            storage_location="mds://10.20.0.2:7776/",
            storage_policy={
                "mirrors": 2,
                "failure_domain": "rack",
            },
        )
        target = MagicMock()
        target.spec.return_value = SimpleNamespace(size=2 << 30)
        factory = MagicMock(return_value=target)
        monkeypatch.setattr(exordos_hyper.rawstor, "Target", factory)
        registry = MagicMock()
        monkeypatch.setattr(exordos_hyper.rawstor, "Location", lambda uri: registry)
        created = driver.create_volume(volume)
        registry.create_account.assert_not_called()
        factory.assert_called_once_with(
            f"mds://{volume.project_id}@10.20.0.2:7776/{volume.uuid}"
        )
        target.create.assert_not_called()
        assert created.storage_location == "mds://10.20.0.2:7776/"
        assert (
            driver._read_remote_location(volume.uuid)
            == f"mds://{volume.project_id}@10.20.0.2:7776/"
        )
        with open(driver._vhost_drop_in_path(volume.uuid)) as f:
            assert (
                f'Environment=LOCATION=mds://{volume.project_id}@10.20.0.2:7776\nEnvironment="EXTRA_ARGS="\n'
                in f.read()
            )

    def test_create_get_list_delete(self, tmp_path, monkeypatch, file_objects):
        _no_op_systemctl(monkeypatch)
        driver = _driver(tmp_path)
        _redirect_vhost_drop_in(monkeypatch, driver, tmp_path)

        volume = pool_base.MachineVolume(
            uuid=sys_uuid.uuid4(),
            project_id=sys_uuid.uuid4(),
            size=1,
            storage_location=_rawstor_location(tmp_path),
        )

        target = file_objects(driver, volume)
        created = driver.create_volume(volume)
        assert created.status == pool_base.VolumeStatus.ACTIVE.value

        fetched = driver.get_volume(volume.uuid)
        assert fetched.uuid == volume.uuid
        assert fetched.size == 1
        assert fetched.machine is None
        assert fetched.project_id == volume.project_id
        target = exordos_hyper.rawstor.Target(
            driver._target_uri(volume.uuid, driver._read_remote_location(volume.uuid))
        )
        assert target.meta()[0].account == str(volume.project_id)

        # list_volumes() only enumerates rawstor volumes attached to a
        # domain on this hypervisor (it has no way to enumerate a remote
        # StorageCluster's own inventory) - a created-but-unattached
        # volume is invisible to it, unlike get_volume() by uuid above.
        assert driver.list_volumes() == []

        driver.delete_volume(created)

        with pytest.raises(pool_base.VolumeNotFoundError):
            driver.get_volume(volume.uuid)

    def test_repeated_attachment_registration_is_idempotent(
        self, tmp_path, monkeypatch, file_objects
    ):
        _no_op_systemctl(monkeypatch)
        driver = _driver(tmp_path)
        _redirect_vhost_drop_in(monkeypatch, driver, tmp_path)

        volume = pool_base.MachineVolume(
            uuid=sys_uuid.uuid4(),
            project_id=sys_uuid.uuid4(),
            size=1,
            storage_location=_rawstor_location(tmp_path),
        )
        target = file_objects(driver, volume)
        driver.create_volume(volume)

        driver.create_volume(volume)
        target.create.assert_not_called()

    def test_get_missing_volume_raises_not_found(self, tmp_path):
        driver = _driver(tmp_path)

        with pytest.raises(pool_base.VolumeNotFoundError):
            driver.get_volume(sys_uuid.uuid4())

    def test_delete_missing_volume_is_idempotent(self, tmp_path, monkeypatch):
        _no_op_systemctl(monkeypatch)
        driver = _driver(tmp_path)
        _redirect_vhost_drop_in(monkeypatch, driver, tmp_path)

        volume = pool_base.MachineVolume(
            uuid=sys_uuid.uuid4(),
            project_id=sys_uuid.uuid4(),
            size=1,
            storage_location=_rawstor_location(tmp_path),
        )

        # Must not raise, even though it was never created.
        driver.delete_volume(volume)

    def test_delete_stops_vhost_and_leaves_object_for_core(
        self, tmp_path, monkeypatch, file_objects
    ):
        # rawstor-vhost is an attachment of the volume: deleting the
        # object without stopping it first would leave a dangling
        # backend process.
        calls = _no_op_systemctl(monkeypatch)
        driver = _driver(tmp_path)
        _redirect_vhost_drop_in(monkeypatch, driver, tmp_path)

        volume = pool_base.MachineVolume(
            uuid=sys_uuid.uuid4(),
            project_id=sys_uuid.uuid4(),
            size=1,
            storage_location=_rawstor_location(tmp_path),
        )
        target = file_objects(driver, volume)
        driver.create_volume(volume)
        calls.clear()

        driver.delete_volume(volume)
        target.remove.assert_not_called()

        # create_volume already recorded a drop-in for this volume, so
        # its removal also triggers a daemon-reload.
        assert calls == [
            ["systemctl", "disable", "--now", f"rawstor-vhost@{volume.uuid}"],
            ["systemctl", "daemon-reload"],
        ]

    def test_delete_aborts_if_the_vhost_unit_fails_to_stop(
        self, tmp_path, monkeypatch, file_objects
    ):
        # Regression: a swallowed disable failure used to let delete_volume
        # remove the object anyway, orphaning a still-enabled unit behind
        # it - a real failure to stop must abort the delete instead.
        _no_op_systemctl(monkeypatch)
        driver = _driver(tmp_path)
        _redirect_vhost_drop_in(monkeypatch, driver, tmp_path)
        volume = pool_base.MachineVolume(
            uuid=sys_uuid.uuid4(),
            project_id=sys_uuid.uuid4(),
            size=1,
            storage_location=_rawstor_location(tmp_path),
        )
        target = file_objects(driver, volume)
        driver.create_volume(volume)

        def fake_check_call(cmd, *a, **kw):
            raise subprocess.CalledProcessError(1, cmd)

        monkeypatch.setattr(subprocess, "check_call", fake_check_call)

        with pytest.raises(subprocess.CalledProcessError):
            driver.delete_volume(volume)

        # The object must still exist - the delete never got past the
        # failed vhost stop.
        assert driver.get_volume(volume.uuid).uuid == volume.uuid
        target.remove.assert_not_called()

    def test_start_vhost_raises_a_clear_error_when_unit_is_missing(
        self, tmp_path, monkeypatch
    ):
        # rawstor-vhost@.service only exists if the rawstor-vhost package
        # is installed (exordos hypervisors init --with-rawstor) - a
        # hypervisor set up without that flag must fail with an
        # actionable hint, not a bare CalledProcessError.
        driver = _driver(tmp_path)

        def fake_check_call(cmd, *a, **kw):
            raise subprocess.CalledProcessError(1, cmd)

        monkeypatch.setattr(subprocess, "check_call", fake_check_call)

        with pytest.raises(RuntimeError, match="rawstor-vhost package"):
            driver._start_vhost(sys_uuid.uuid4())


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
        base_driver, machine, volume = self._foreign_machine_and_volume("default-pool")
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
        # A hypervisor without a configured local storage_pool must not
        # crash trying to discover foreign qcow2 volumes.
        driver = _driver(tmp_path)

        assert driver.list_volumes() == []


class TestCreateMachine:
    def test_domain_is_defined_with_shared_memory(
        self, tmp_path, monkeypatch, file_objects
    ):
        # Regression: libvirt refuses to attach a vhostuser disk to a
        # domain that wasn't defined with shared memory backing, and it
        # can't be added after the fact - so every machine of this pool
        # (all of them vhost-user-backed) must get it at create time.
        _no_op_systemctl(monkeypatch)
        driver = _driver(tmp_path)
        _redirect_vhost_drop_in(monkeypatch, driver, tmp_path)
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
            storage_location=_rawstor_location(tmp_path),
        )
        file_objects(driver, root_vol)
        driver.create_volume(root_vol)

        driver.create_machine(machine, [root_vol], [])

        domain = driver._client.lookupByUUIDString(str(machine.uuid))
        memory_backing = ET.fromstring(domain.XMLDesc()).find("memoryBacking")
        assert memory_backing is not None
        assert memory_backing.find("access").get("mode") == "shared"


class TestDeleteMachine:
    def test_removes_all_volumes_and_stops_their_vhost_units(
        self, tmp_path, monkeypatch, file_objects
    ):
        # Regression: volume-to-machine attribution is read from the
        # domain's own XML, so deleting all the machine's volumes must
        # happen before the domain is undefined - otherwise the volumes
        # (and their still-running rawstor-vhost units) are silently
        # orphaned.
        calls = _no_op_systemctl(monkeypatch)
        driver = _driver(tmp_path)
        _redirect_vhost_drop_in(monkeypatch, driver, tmp_path)
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
            storage_location=_rawstor_location(tmp_path),
        )
        data_vol = pool_base.MachineVolume(
            uuid=sys_uuid.uuid4(),
            project_id=sys_uuid.uuid4(),
            size=2,
            index=1,
            machine=machine.uuid,
            storage_location=_rawstor_location(tmp_path),
        )
        for v in (root_vol, data_vol):
            file_objects(driver, v)
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
            uuid=sys_uuid.uuid4(),
            project_id=sys_uuid.uuid4(),
            size=1,
            storage_location=_rawstor_location(tmp_path),
        )

        with pytest.raises(pool_base.VolumeResizeNotSupportedError):
            driver.resize_volume(volume)


@pytest.mark.parametrize(
    "location",
    ["mds://host:7776/\nUser=root", 'mds://host:7776/"', "mds://host:7776/%i"],
)
def test_vhost_dropin_rejects_service_injection(tmp_path, location):
    driver = _driver(tmp_path)
    with pytest.raises(ValueError):
        driver._write_remote_location_drop_in(sys_uuid.uuid4(), location)


def test_local_disk_machine_is_also_prepared_for_remote_hotplug(tmp_path):
    driver = _driver(tmp_path)
    domain = MagicMock()
    machine = pool_base.Machine(
        uuid=sys_uuid.uuid4(),
        project_id=sys_uuid.uuid4(),
        name="local",
        cores=1,
        ram=512,
    )
    driver._add_volumes_to_domain(domain, machine, [])
    domain.set_shared_memory.assert_called_once()


def test_missing_vhost_template_does_not_block_object_cleanup(tmp_path, monkeypatch):
    driver = _driver(tmp_path)
    _redirect_vhost_drop_in(monkeypatch, driver, tmp_path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="not-found\n"
        ),
    )
    stop = MagicMock()
    monkeypatch.setattr(subprocess, "check_call", stop)
    driver._stop_vhost(sys_uuid.uuid4())
    stop.assert_not_called()


def test_qcow2_discovery_does_not_require_rawstor_binding(tmp_path, monkeypatch):
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "optional_rawstor_hyper", exordos_hyper.__file__
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "rawstor", None)
    spec.loader.exec_module(module)
    driver = module.ExordosLocalHyperDriver(
        pool_base.MachinePool(
            uuid=sys_uuid.uuid4(),
            name="local",
            driver_spec=pool_base.ExordosLocalHyperDriverSpec(
                connection_uri="test:///default", node=sys_uuid.uuid4(), storage_pool=[]
            ),
        )
    )
    assert driver.list_volumes() == []
    with pytest.raises(RuntimeError, match="Rawstor"):
        module._rawstor_module(sys_uuid.uuid4())


@pytest.mark.parametrize(
    "operation", ["list_pool_resources", "list_volumes", "get_qcow2", "get_rawstor"]
)
def test_mixed_disk_observation_reads_each_libvirt_snapshot_once(
    tmp_path, monkeypatch, request, operation
):
    base_driver, machine, local_volume = (
        TestForeignVolumes()._foreign_machine_and_volume("default-pool")
    )
    driver = _driver(tmp_path, storage_pool="default-pool")
    request.addfinalizer(base_driver._client.close)
    request.addfinalizer(driver._client.close)
    remote_uuid = sys_uuid.uuid4()
    domains = []
    for domain in driver._client.listAllDomains():
        if domain.UUIDString() != str(machine.uuid):
            continue
        observed = MagicMock(wraps=domain)
        root = ET.fromstring(domain.XMLDesc())
        if domain.UUIDString() == str(machine.uuid):
            disk = ET.SubElement(
                root.find("devices"), "disk", type="vhostuser", device="disk"
            )
            ET.SubElement(disk, "source", path=driver._socket_path(remote_uuid))
        observed.XMLDesc.return_value = ET.tostring(root, encoding="unicode")
        domains.append(observed)
    storage_pool = MagicMock(
        wraps=driver._client.storagePoolLookupByName("default-pool")
    )
    client = MagicMock(wraps=driver._client)
    client.listAllDomains.return_value = domains
    client.storagePoolLookupByName.return_value = storage_pool
    monkeypatch.setattr(driver, "_client_instance", client)
    monkeypatch.setattr(
        driver,
        "_read_remote_location",
        lambda uuid: "mds://core:7776/" if uuid == remote_uuid else None,
    )
    target = MagicMock()
    target.spec.return_value.size = 1 << 30
    monkeypatch.setattr(exordos_hyper.rawstor, "Target", lambda uri: target)

    for cycle in (1, 2):
        if operation == "list_pool_resources":
            _, _, machines, volumes = driver.list_pool_resources()
            assert machine.uuid in {model.uuid for model, _ in machines}
            assert storage_pool.info.call_count == cycle
        elif operation == "list_volumes":
            volumes = driver.list_volumes(machine)
        else:
            uuid = local_volume.uuid if operation == "get_qcow2" else remote_uuid
            volumes = [driver.get_volume(uuid)]
        by_uuid = {volume.uuid: volume for volume in volumes}
        expected = (
            {local_volume.uuid, remote_uuid}
            if operation.startswith("list_")
            else {uuid}
        )
        assert expected <= by_uuid.keys()
        for uuid in expected:
            assert by_uuid[uuid].machine == machine.uuid
            assert by_uuid[uuid].index == (0 if uuid == local_volume.uuid else 1)
        assert client.listAllDomains.call_count == cycle
        assert all(domain.XMLDesc.call_count == cycle for domain in domains)
        if operation != "get_rawstor":
            assert client.storagePoolLookupByName.call_count == cycle
            assert storage_pool.listAllVolumes.call_count == cycle
    assert base_driver is not None  # Keep the test hypervisor alive.


@pytest.fixture
def resize_environment(tmp_path, monkeypatch):
    driver = _driver(tmp_path)
    _redirect_vhost_drop_in(monkeypatch, driver, tmp_path)
    _no_op_systemctl(monkeypatch)
    monkeypatch.setattr(driver, "_domains_with_xml", lambda: ())
    volume = pool_base.MachineVolume(
        uuid=sys_uuid.uuid4(),
        project_id=sys_uuid.uuid4(),
        size=3,
        storage_location="mds://core:7776/",
    )
    driver._write_remote_location_drop_in(volume.uuid, driver._rawstor_address(volume))
    driver._write_observed_size(volume.uuid, 2)
    size = {"bytes": 3 << 30}
    target = MagicMock()
    target.spec.side_effect = lambda: SimpleNamespace(
        size=size["bytes"], chunk_size=1 << 30
    )
    target.resize.side_effect = lambda value: size.update(bytes=value)
    monkeypatch.setattr(exordos_hyper.rawstor, "Target", lambda uri: target)
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0))
    notify = MagicMock()
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "check_call", notify)
    return SimpleNamespace(
        driver=driver,
        volume=volume,
        target=target,
        size=size,
        run=run,
        notify=notify,
        pending=tmp_path
        / "drop-ins"
        / f"rawstor-vhost@{volume.uuid}.service.d"
        / "resize-pending",
    )


def test_resize_mds_disk_notifies_live_vhost_without_restart(resize_environment):
    env = resize_environment
    env.driver.resize_volume(env.volume)
    env.target.resize.assert_not_called()
    env.notify.assert_called_once_with(
        [
            "systemctl",
            "kill",
            "--kill-whom=main",
            "--signal=HUP",
            env.driver._vhost_unit(env.volume.uuid),
        ]
    )
    assert env.driver.get_volume(env.volume.uuid).size == 3
    assert not env.pending.exists()


def test_resize_inactive_vhost_does_not_start_it(resize_environment):
    env = resize_environment
    env.run.return_value.returncode = 3
    env.driver.resize_volume(env.volume)
    env.notify.assert_not_called()
    assert env.size["bytes"] == 3 << 30
    assert not env.pending.exists()


@pytest.mark.parametrize("size,chunk_size", [(1, 1 << 30), (3, 2 << 30)])
def test_resize_rejects_shrink_and_partial_chunks(resize_environment, size, chunk_size):
    env = resize_environment
    env.volume.size = size
    env.target.spec.side_effect = lambda: SimpleNamespace(
        size=2 << 30, chunk_size=chunk_size
    )
    with pytest.raises(ValueError, match="whole chunks"):
        env.driver.resize_volume(env.volume)
    env.target.resize.assert_not_called()
    assert not env.pending.exists()


@pytest.mark.parametrize("discovery", ["get", "list"])
def test_discovery_finishes_resize_after_failed_vhost_notification(
    resize_environment, tmp_path, monkeypatch, discovery
):
    env = resize_environment
    env.notify.side_effect = subprocess.CalledProcessError(1, ["systemctl", "kill"])
    with pytest.raises(subprocess.CalledProcessError):
        env.driver.resize_volume(env.volume)
    assert env.size["bytes"] == 3 << 30
    assert env.pending.exists()
    env.notify.side_effect = None
    restarted = _driver(tmp_path)
    _redirect_vhost_drop_in(monkeypatch, restarted, tmp_path)
    monkeypatch.setattr(restarted, "_domains_with_xml", lambda: ())
    if discovery == "get":
        actual = restarted.get_volume(env.volume.uuid)
    else:
        domain = SimpleNamespace(UUIDString=lambda: str(sys_uuid.uuid4()))
        monkeypatch.setattr(
            restarted,
            "_rawstor_attachments",
            lambda domains: {env.volume.uuid: (domain, 0)},
        )
        [actual] = restarted._list_rawstor_volumes(())
    assert actual.size == 3
    env.target.resize.assert_not_called()
    assert not env.pending.exists()


def test_hypervisor_waits_for_core_grow_before_notifying_vhost(resize_environment):
    env = resize_environment
    env.size["bytes"] = 2 << 30
    with pytest.raises(RuntimeError, match="Core agent"):
        env.driver.resize_volume(env.volume)
    env.target.resize.assert_not_called()
    env.notify.assert_not_called()
    env.size["bytes"] = 3 << 30
    assert env.driver.get_volume(env.volume.uuid).size == 3
    env.notify.assert_called_once()


def test_resize_retries_when_systemd_state_is_unavailable(resize_environment):
    env = resize_environment
    env.run.return_value.returncode = 1
    with pytest.raises(subprocess.CalledProcessError):
        env.driver.resize_volume(env.volume)
    assert env.pending.exists()
    env.notify.assert_not_called()
    env.run.return_value.returncode = 0
    assert env.driver.get_volume(env.volume.uuid).size == 3
    assert not env.pending.exists()


def test_concurrent_discovery_keeps_resized_volume(resize_environment, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    env = resize_environment
    env.pending.write_text(str(3 << 30))
    barrier = Barrier(2)

    original_spec = env.target.spec.side_effect

    def spec():
        barrier.wait(timeout=5)
        return original_spec()

    env.target.spec.side_effect = spec
    domain = SimpleNamespace(UUIDString=lambda: str(sys_uuid.uuid4()))
    monkeypatch.setattr(
        env.driver,
        "_rawstor_attachments",
        lambda domains: {env.volume.uuid: (domain, 0)},
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda _: env.driver._list_rawstor_volumes(()), range(2))
        )
    assert [len(result) for result in results] == [1, 1]
    assert all(result[0].size == 3 for result in results)
    assert not env.pending.exists()


def _power_domain(driver, monkeypatch, *, active, shared, volume_uuid=None):
    memory = (
        '<memoryBacking><source type="memfd"/><access mode="shared"/></memoryBacking>'
        if shared
        else ""
    )
    disk = (
        f'<disk type="vhostuser" device="disk"><source type="unix" path="{driver._socket_path(volume_uuid)}"/></disk>'
        if volume_uuid
        else ""
    )
    xml = f"<domain><name>legacy</name><metadata><marker>keep</marker></metadata>{memory}<devices>{disk}</devices></domain>"
    domain = MagicMock()
    domain.XMLDesc.return_value = xml
    domain.isActive.return_value = active
    domain.state.return_value = (exordos_hyper.libvirt.VIR_DOMAIN_SHUTOFF, 0)
    client = MagicMock()
    client.lookupByUUIDString.return_value = domain
    client.defineXML.return_value = domain
    monkeypatch.setattr(driver, "_client_instance", client, raising=False)
    return domain, client


@pytest.mark.parametrize("shared", [False, True])
def test_cold_start_prepares_legacy_memory_and_starts_backend_first(
    tmp_path, monkeypatch, shared
):
    driver = _driver(tmp_path)
    volume_uuid = sys_uuid.uuid4()
    domain, client = _power_domain(
        driver, monkeypatch, active=False, shared=shared, volume_uuid=volume_uuid
    )
    monkeypatch.setattr(
        driver, "_read_remote_location", lambda uuid: "mds://core:7776/"
    )
    order = []
    monkeypatch.setattr(driver, "_start_vhost", lambda uuid: order.append(uuid))
    domain.create.side_effect = lambda: order.append("qemu")
    driver.start_machine(SimpleNamespace(uuid=sys_uuid.uuid4()))
    assert order == [volume_uuid, "qemu"]
    if not shared:
        root = ET.fromstring(client.defineXML.call_args.args[0])
        assert root.find("memoryBacking/source").get("type") == "memfd"
        assert root.find("memoryBacking/access").get("mode") == "shared"
        assert root.find("metadata/marker").text == "keep"
    else:
        client.defineXML.assert_not_called()


def test_backend_start_failure_prevents_qemu_start(tmp_path, monkeypatch):
    driver = _driver(tmp_path)
    volume_uuid = sys_uuid.uuid4()
    domain, _ = _power_domain(
        driver, monkeypatch, active=False, shared=True, volume_uuid=volume_uuid
    )
    monkeypatch.setattr(
        driver, "_read_remote_location", lambda uuid: "mds://core:7776/"
    )
    monkeypatch.setattr(
        driver,
        "_start_vhost",
        MagicMock(side_effect=RuntimeError("backend unavailable")),
    )
    with pytest.raises(RuntimeError, match="backend unavailable"):
        driver.start_machine(SimpleNamespace(uuid=sys_uuid.uuid4()))
    domain.create.assert_not_called()


def test_missing_backend_configuration_does_not_start_default_target(
    tmp_path, monkeypatch
):
    driver = _driver(tmp_path)
    domain, _ = _power_domain(
        driver, monkeypatch, active=False, shared=True, volume_uuid=sys_uuid.uuid4()
    )
    monkeypatch.setattr(driver, "_read_remote_location", lambda uuid: None)
    backend = MagicMock()
    monkeypatch.setattr(driver, "_start_vhost", backend)
    with pytest.raises(RuntimeError, match="Missing Rawstor backend configuration"):
        driver.start_machine(SimpleNamespace(uuid=sys_uuid.uuid4()))
    backend.assert_not_called()
    domain.create.assert_not_called()


@pytest.mark.parametrize("active,shared", [(False, False), (True, False), (True, True)])
def test_hotplug_upgrades_only_stopped_legacy_domains(
    tmp_path, monkeypatch, active, shared
):
    driver = _driver(tmp_path)
    domain, client = _power_domain(driver, monkeypatch, active=active, shared=shared)
    backend = MagicMock()
    monkeypatch.setattr(driver, "_start_vhost", backend)
    volume = pool_base.MachineVolume(
        uuid=sys_uuid.uuid4(),
        project_id=sys_uuid.uuid4(),
        machine=sys_uuid.uuid4(),
        storage_location="mds://core:7776/",
    )
    if active and not shared:
        with pytest.raises(RuntimeError, match="Shut down and start"):
            driver.attach_volume(volume)
        backend.assert_not_called()
        client.defineXML.assert_not_called()
        domain.attachDeviceFlags.assert_not_called()
        return
    driver.attach_volume(volume)
    flags = domain.attachDeviceFlags.call_args.args[1]
    assert flags == (
        exordos_hyper.libvirt.VIR_DOMAIN_AFFECT_CONFIG
        | (exordos_hyper.libvirt.VIR_DOMAIN_AFFECT_LIVE if active else 0)
    )
    assert client.defineXML.called is (not shared)


def test_paused_vm_restarts_backend_before_resume(tmp_path, monkeypatch):
    driver = _driver(tmp_path)
    volume_uuid = sys_uuid.uuid4()
    domain, client = _power_domain(
        driver, monkeypatch, active=True, shared=True, volume_uuid=volume_uuid
    )
    domain.state.return_value = (exordos_hyper.libvirt.VIR_DOMAIN_PAUSED, 0)
    monkeypatch.setattr(
        driver, "_read_remote_location", lambda uuid: "mds://core:7776/"
    )
    order = []
    monkeypatch.setattr(driver, "_start_vhost", lambda uuid: order.append(uuid))
    domain.resume.side_effect = lambda: order.append("resume")
    driver.start_machine(SimpleNamespace(uuid=sys_uuid.uuid4()))
    assert order == [volume_uuid, "resume"]
    client.defineXML.assert_not_called()
    domain.create.assert_not_called()


def test_start_ignores_unrelated_vhost_socket(tmp_path, monkeypatch):
    driver = _driver(tmp_path)
    domain, _ = _power_domain(
        driver, monkeypatch, active=False, shared=True, volume_uuid=sys_uuid.uuid4()
    )
    domain.XMLDesc.return_value = domain.XMLDesc.return_value.replace(
        "/run/rawstor/", "/run/other/"
    )
    backend = MagicMock()
    monkeypatch.setattr(driver, "_start_vhost", backend)
    driver.start_machine(SimpleNamespace(uuid=sys_uuid.uuid4()))
    backend.assert_not_called()
    domain.create.assert_called_once()


@pytest.mark.parametrize("active", [False, True])
def test_detach_uses_live_flags_only_for_running_domains(tmp_path, monkeypatch, active):
    driver = _driver(tmp_path)
    volume_uuid = sys_uuid.uuid4()
    domain, _ = _power_domain(
        driver, monkeypatch, active=active, shared=True, volume_uuid=volume_uuid
    )
    stopped = MagicMock()
    monkeypatch.setattr(driver, "_stop_vhost", stopped)
    volume = pool_base.MachineVolume(
        uuid=volume_uuid,
        project_id=sys_uuid.uuid4(),
        machine=sys_uuid.uuid4(),
        storage_location="mds://core:7776/",
    )
    driver.detach_volume(volume)
    flags = domain.detachDeviceFlags.call_args.args[1]
    assert flags == (
        exordos_hyper.libvirt.VIR_DOMAIN_AFFECT_CONFIG
        | (exordos_hyper.libvirt.VIR_DOMAIN_AFFECT_LIVE if active else 0)
    )
    stopped.assert_called_once_with(volume_uuid)


@pytest.mark.parametrize(
    "endpoint", ["mds://core:7776/", "ost://host:7777", "file:///data/ost"]
)
def test_project_id_becomes_account_in_every_rawstor_uri(tmp_path, endpoint):
    driver = _driver(tmp_path)
    volume = pool_base.MachineVolume(
        project_id=sys_uuid.uuid4(), storage_location=endpoint
    )
    address = driver._rawstor_address(volume)
    assert exordos_hyper.rawstor.Target(
        driver._target_uri(volume.uuid, address)
    ).account == str(volume.project_id)
    assert driver._project_from_location(address) == volume.project_id


def test_nil_project_cannot_silently_become_default_account(tmp_path, monkeypatch):
    driver = _driver(tmp_path)
    registry = MagicMock()
    monkeypatch.setattr(exordos_hyper.rawstor, "Location", registry)
    volume = pool_base.MachineVolume(
        project_id=sys_uuid.UUID(int=0), storage_location="mds://core:7776/"
    )
    with pytest.raises(ValueError, match="non-nil project_id"):
        driver.create_volume(volume)
    registry.assert_not_called()


def test_hypervisor_never_creates_accounts_or_objects(tmp_path, monkeypatch):
    driver = _driver(tmp_path)
    _no_op_systemctl(monkeypatch)
    _redirect_vhost_drop_in(monkeypatch, driver, tmp_path)
    project = sys_uuid.uuid4()
    volume = pool_base.MachineVolume(
        project_id=project, storage_location="mds://core:7776/", size=1
    )
    registry = MagicMock()

    target = MagicMock()
    target.spec.return_value = SimpleNamespace(size=1 << 30)
    monkeypatch.setattr(exordos_hyper.rawstor, "Location", lambda uri: registry)
    factory = MagicMock(return_value=target)
    monkeypatch.setattr(exordos_hyper.rawstor, "Target", factory)
    driver.create_volume(volume)
    registry.create_account.assert_not_called()
    target.create.assert_not_called()
    factory.assert_called_once_with(f"mds://{project}@core:7776/{volume.uuid}")
    restored = _driver(tmp_path)
    _redirect_vhost_drop_in(monkeypatch, restored, tmp_path)
    recovered = restored.get_volume(volume.uuid)
    assert recovered.project_id == project


def test_remote_client_uses_volume_control_credentials(monkeypatch):
    from gcl_sdk.agents.universal.drivers import rawstor_tls

    factory = MagicMock()
    monkeypatch.setattr(rawstor_tls, "Bindings", factory)
    volume = sys_uuid.uuid4()
    exordos_hyper._rawstor_module(volume)
    factory.assert_called_once_with(
        rawstor_tls.TLS_DIR / ("control-volume-" + str(volume))
    )
