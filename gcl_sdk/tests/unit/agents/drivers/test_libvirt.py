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
from xml.dom import minidom
from xml.etree import ElementTree as ET

import pytest

# The libvirt driver imports the `libvirt` python bindings at module level.
# They ship as the optional `gcl_sdk[libvirt]` extra, so skip this module
# instead of failing collection when it's not installed.
pytest.importorskip("libvirt")

import libvirt  # noqa: E402

from gcl_sdk.agents.universal.drivers import libvirt as libvirt_driver  # noqa: E402
from gcl_sdk.agents.universal.drivers import pool as pool_base  # noqa: E402


def _local_driver() -> libvirt_driver.LibvirtPoolDriver:
    # libvirt's built-in "test" driver simulates a hypervisor in-memory -
    # no real virtualization or daemon needed, so real libvirt calls
    # (lookupByUUIDString, etc.) can be exercised end-to-end.
    spec = pool_base.LibvirtPoolDriverSpec(connection_uri="test:///default")
    pool = pool_base.MachinePool(
        uuid=sys_uuid.uuid4(), name="test-pool", driver_spec=spec
    )
    return libvirt_driver.LibvirtPoolDriver(pool)


def _multi_pool_driver(storage_pool) -> libvirt_driver.LibvirtPoolDriver:
    spec = pool_base.ExordosLocalHyperDriverSpec(
        connection_uri="test:///default",
        node=sys_uuid.uuid4(),
        storage_pool=storage_pool,
    )
    pool = pool_base.MachinePool(
        uuid=sys_uuid.uuid4(), name="test-pool", driver_spec=spec
    )
    return libvirt_driver.LibvirtPoolDriver(pool)


class TestStoragePoolBackwardCompat:
    """ExordosLocalHyperDriverSpec.storage_pool also accepts a bare pool
    name string (the format an exordos_core that predates per-pool
    speed/ephemeral tagging still sends), not only the new named-pool
    list - see StoragePoolListOrLegacyName.
    """

    def test_legacy_string_is_treated_as_the_sole_pool(self):
        driver = _multi_pool_driver("default-pool")

        assert driver._storage_pool_names() == ["default-pool"]
        assert driver._storage_pool_attributes("default-pool") == ("warm", False)

        volume = pool_base.MachineVolume(
            uuid=sys_uuid.uuid4(), name="vol", size=1, project_id=sys_uuid.uuid4()
        )
        assert driver._storage_pool_name_for(volume) == "default-pool"

    def test_new_list_format_keeps_per_pool_attributes(self):
        driver = _multi_pool_driver(
            [
                {"name": "hot-pool", "speed": "hot", "ephemeral": True},
                {"name": "cold-pool"},
            ]
        )

        assert driver._storage_pool_names() == ["hot-pool", "cold-pool"]
        assert driver._storage_pool_attributes("hot-pool") == ("hot", True)
        # Attributes omitted from an entry fall back to the same defaults
        # as the legacy string format.
        assert driver._storage_pool_attributes("cold-pool") == ("warm", False)

        volume = pool_base.MachineVolume(
            uuid=sys_uuid.uuid4(),
            name="vol",
            size=1,
            project_id=sys_uuid.uuid4(),
            storage_pool="hot-pool",
        )
        assert driver._storage_pool_name_for(volume) == "hot-pool"


def _define_second_pool(driver: libvirt_driver.LibvirtPoolDriver) -> None:
    # libvirt's "test" driver keeps its state for the life of the process,
    # not per connection - a pool defined by an earlier test is still
    # there, so this must be idempotent.
    try:
        vir_pool = driver._client.storagePoolLookupByName("second-pool")
    except libvirt.libvirtError:
        xml = """
        <pool type='dir'>
          <name>second-pool</name>
          <target>
            <path>/second-pool</path>
          </target>
        </pool>
        """
        vir_pool = driver._client.storagePoolDefineXML(xml)

    if not vir_pool.isActive():
        vir_pool.create()


class TestGetVolumeStoragePool:
    """Regression: a volume discovered in a non-default pool (via
    `list_volumes`/`get_volume`) used to come back with `storage_pool`
    left as None, so a later attach/detach/resize on it would raise
    ValueError in a multi-pool configuration (`_storage_pool_name_for`
    can't guess which of several pools it belongs to). Both `list_volumes`
    and `get_volume` must stamp the pool it was actually found in.
    """

    def _driver_with_second_pool(self) -> libvirt_driver.LibvirtPoolDriver:
        driver = _multi_pool_driver(
            [{"name": "default-pool"}, {"name": "second-pool"}]
        )
        _define_second_pool(driver)
        return driver

    def test_get_volume_reports_the_pool_it_was_found_in(self):
        driver = self._driver_with_second_pool()
        volume_uuid = sys_uuid.uuid4()
        created = driver.create_volume(
            pool_base.MachineVolume(
                uuid=volume_uuid,
                name=str(volume_uuid),
                size=1,
                project_id=sys_uuid.uuid4(),
                storage_pool="second-pool",
            )
        )
        assert created.storage_pool == "second-pool"

        fetched = driver.get_volume(volume_uuid)

        assert fetched.storage_pool == "second-pool"
        # Would raise ValueError before the fix: with storage_pool unset
        # and two pools configured, there's no way to tell which one.
        assert driver._storage_pool_name_for(fetched) == "second-pool"

    def test_list_volumes_reports_the_pool_each_volume_was_found_in(self):
        driver = self._driver_with_second_pool()
        volume_uuid = sys_uuid.uuid4()
        driver.create_volume(
            pool_base.MachineVolume(
                uuid=volume_uuid,
                name=str(volume_uuid),
                size=1,
                project_id=sys_uuid.uuid4(),
                storage_pool="second-pool",
            )
        )

        listed = {v.uuid: v for v in driver.list_volumes()}

        assert listed[volume_uuid].storage_pool == "second-pool"


class TestDeleteMachine:
    def test_is_idempotent_when_the_domain_is_already_gone(self):
        driver = _local_driver()
        machine = pool_base.Machine(
            uuid=sys_uuid.uuid4(),
            project_id=sys_uuid.uuid4(),
            name="never-existed",
            cores=1,
            ram=512,
        )

        # Must not raise, even though no such domain was ever defined.
        driver.delete_machine(machine, delete_volumes=False)

    def test_volume_cleanup_still_runs_when_the_domain_is_already_gone(self):
        driver = _local_driver()
        machine = pool_base.Machine(
            uuid=sys_uuid.uuid4(),
            project_id=sys_uuid.uuid4(),
            name="never-existed",
            cores=1,
            ram=512,
        )

        # The missing-domain path must fall through to volume cleanup,
        # not skip it.
        with mock.patch.object(
            driver, "list_volumes", return_value=[]
        ) as mock_list_volumes:
            driver.delete_machine(machine, delete_volumes=True)

        mock_list_volumes.assert_called_once_with(machine)

    def test_removes_the_machines_volume(self):
        # Regression: volume-to-machine attribution is read from the
        # domain's own XML, so the volume must be looked up before the
        # domain is undefined - otherwise cleanup silently finds nothing
        # and the volume (and its disk) is orphaned forever.
        spec = pool_base.LibvirtPoolDriverSpec(
            connection_uri="test:///default", storage_pool="default-pool"
        )
        pool = pool_base.MachinePool(
            uuid=sys_uuid.uuid4(), name="test-pool", driver_spec=spec
        )
        driver = libvirt_driver.LibvirtPoolDriver(pool)
        machine = pool_base.Machine(
            uuid=sys_uuid.uuid4(),
            project_id=sys_uuid.uuid4(),
            name="vm1",
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
        volume = driver.create_volume(volume)
        port = pool_base.Port(
            uuid=sys_uuid.uuid4(),
            project_id=sys_uuid.uuid4(),
            mac="52:54:00:11:22:33",
            source="default",
            status="ACTIVE",
        )
        driver.create_machine(machine, [volume], [port])

        driver.delete_machine(machine, delete_volumes=True)

        storage_pool = driver._client.storagePoolLookupByName("default-pool")
        assert list(storage_pool.listAllVolumes()) == []


class TestRemoveDirectChildren:
    def test_removes_only_direct_children_leaving_nested_matches_alone(self):
        # getElementsByTagName searches the whole subtree recursively -
        # a naive removeChild(node) on a match found deeper in the tree
        # (not a direct child of root) raises NotFoundErr.
        doc = minidom.parseString("<root><a>direct</a><b><a>nested</a></b></root>")
        root = doc.firstChild

        libvirt_driver.XMLLibvirtInstance._remove_direct_children(root, "a")

        assert root.getElementsByTagName("a") == doc.getElementsByTagName("b")[
            0
        ].getElementsByTagName("a")
        assert len(doc.getElementsByTagName("a")) == 1
        assert doc.getElementsByTagName("a")[0].firstChild.data == "nested"

    def test_leaves_other_tag_names_alone(self):
        doc = minidom.parseString("<root><a>1</a><c>2</c></root>")
        root = doc.firstChild

        libvirt_driver.XMLLibvirtInstance._remove_direct_children(root, "a")

        assert len(doc.getElementsByTagName("a")) == 0
        assert len(doc.getElementsByTagName("c")) == 1

    def test_re_setting_a_tag_with_a_same_named_nested_element_does_not_crash(self):
        # Regression: domain_set_vcpu/domain_set_memory/etc. re-set their
        # tag on every call - this must not crash even if some unrelated
        # nested element happens to share the tag name.
        domain = libvirt_driver.XMLLibvirtInstance(libvirt_driver.domain_template)
        devices = ET.fromstring(domain.xml).find("devices")
        assert devices is not None  # sanity: domain_template has one

        domain.set_vcpu(2)
        domain.set_vcpu(4)
        domain.set_memory(1024)
        domain.set_memory(2048)

        element = ET.fromstring(domain.xml)
        assert element.find(".//vcpu").text == "4"
        assert element.find(".//currentMemory").text == "2048"


class TestVhostuserDisk:
    def test_disk_xml_shape(self):
        # Shape validated against libvirt's RelaxNG schema
        # (diskSourceVhostUser in domaincommon.rng): type='unix' source
        # with no `mode` attribute (that only applies to network
        # interfaces, not disk sources) plus an optional <reconnect>.
        xml = libvirt_driver.XMLLibvirtInstance.vhostuser_disk_xml(
            "/run/rawstor/00000000-0000-0000-0000-000000000001.sock",
            device="vdb",
        )
        disk = ET.fromstring(xml)

        assert disk.get("type") == "vhostuser"
        assert disk.get("device") == "disk"
        assert disk.find("driver").get("name") == "qemu"

        source = disk.find("source")
        assert source.get("type") == "unix"
        assert (
            source.get("path")
            == "/run/rawstor/00000000-0000-0000-0000-000000000001.sock"
        )
        assert source.get("mode") is None

        reconnect = source.find("reconnect")
        assert reconnect.get("enabled") == "yes"

        target = disk.find("target")
        assert target.get("dev") == "vdb"
        assert target.get("bus") == "virtio"

    def test_domain_add_vhostuser_disk_appends_to_devices(self):
        domain = libvirt_driver.XMLLibvirtInstance(libvirt_driver.domain_template)

        domain.add_vhostuser_disk(
            "/run/rawstor/00000000-0000-0000-0000-000000000002.sock", device="vdc"
        )

        disks = ET.fromstring(domain.xml).findall(".//devices/disk")
        assert len(disks) == 1
        assert disks[0].get("type") == "vhostuser"
        assert disks[0].find("target").get("dev") == "vdc"

    def test_set_shared_memory_adds_memory_backing(self):
        # Regression: libvirt refuses to attach a vhostuser disk
        # ("'vhostuser' requires shared memory") unless the domain was
        # defined with this element - it can't be added after the fact.
        domain = libvirt_driver.XMLLibvirtInstance(libvirt_driver.domain_template)

        domain.set_shared_memory()

        memory_backing = ET.fromstring(domain.xml).find("memoryBacking")
        assert memory_backing is not None
        assert memory_backing.find("access").get("mode") == "shared"
        # Regression: without an explicit memfd source, libvirt backs the
        # shared region with a plain file under its own memory_backing_dir
        # (ordinary disk unless the host happens to point that at tmpfs) -
        # guest RAM would then be reclaimable/writeback-able like any
        # other file-backed page, even with no swap configured.
        assert memory_backing.find("source").get("type") == "memfd"

    def test_set_shared_memory_is_idempotent(self):
        domain = libvirt_driver.XMLLibvirtInstance(libvirt_driver.domain_template)

        domain.set_shared_memory()
        domain.set_shared_memory()

        memory_backings = ET.fromstring(domain.xml).findall("memoryBacking")
        assert len(memory_backings) == 1


def test_domain_console_logs_to_file():
    log_path = "/var/log/libvirt/qemu/test-vm.console.log"

    domain = libvirt_driver.XMLLibvirtInstance(libvirt_driver.domain_template)
    domain.set_console_log(log_path)

    console = ET.fromstring(domain.xml).find(".//devices/console")
    assert console is not None

    log = console.find("log")
    assert log is not None

    assert console.get("type") == "pty"
    assert log.get("file") == log_path
    assert log.get("append") == "on"
