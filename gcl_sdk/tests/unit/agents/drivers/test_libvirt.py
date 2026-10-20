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
from gcl_sdk.infra import constants as ic  # noqa: E402


def _local_driver() -> libvirt_driver.LibvirtPoolDriver:
    # libvirt's built-in "test" driver simulates a hypervisor in-memory -
    # no real virtualization or daemon needed, so real libvirt calls
    # (lookupByUUIDString, etc.) can be exercised end-to-end.
    spec = pool_base.LibvirtPoolDriverSpec(
        connection_uri="test:///default", storage_pool="default-pool"
    )
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


class TestStoragePoolConfiguration:
    def test_plain_libvirt_spec_keeps_a_single_named_pool(self):
        driver = _local_driver()
        assert driver._storage_pool_names() == ["default-pool"]
        assert driver._storage_pool_attributes("default-pool") == ("WARM", True)

    def test_new_list_format_keeps_per_pool_attributes(self):
        driver = _multi_pool_driver(
            [
                {
                    "name": "hot-pool",
                    "tier": ic.DiskTier.HOT.value,
                    "ephemeral": True,
                },
                {"name": "cold-pool"},
            ]
        )

        assert driver._storage_pool_names() == ["hot-pool", "cold-pool"]
        assert driver._storage_pool_attributes("hot-pool") == (
            ic.DiskTier.HOT.value,
            True,
        )
        # Attributes omitted from an entry fall back to the same defaults
        # as a single libvirt pool.
        assert driver._storage_pool_attributes("cold-pool") == (
            ic.DiskTier.WARM.value,
            True,
        )

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
        driver = _multi_pool_driver([{"name": "default-pool"}, {"name": "second-pool"}])
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


class TestResizeMachine:
    @pytest.fixture
    def resize_domain(self):
        driver = _local_driver()
        machine = pool_base.Machine(
            uuid=sys_uuid.uuid4(),
            project_id=sys_uuid.uuid4(),
            name="cassi-resize",
            cores=1,
            ram=512,
        )
        xml = libvirt_driver.XMLLibvirtInstance(libvirt_driver.domain_template)
        xml.set_uuid(machine.uuid)
        xml.set_name(driver._machine2domain_name(machine))
        xml.set_vcpu(machine.cores)
        xml.set_memory(machine.ram)
        xml.set_image("test-image")
        xml.add_disk("/test-root.qcow2", "vda")
        xml.add_disk("/test-data.qcow2", "vdc")
        xml.add_interface("network", source="default", mac="52:54:00:11:22:55")
        domain = mock.Mock()
        domain.isActive.return_value = 1
        client = mock.Mock()
        client.lookupByUUIDString.return_value = domain
        client.defineXML.return_value = domain
        driver._client_instance = client
        with (
            mock.patch.object(driver, "_list_interfaces", return_value=[]),
            mock.patch.object(driver, "list_volumes", return_value=[]),
        ):
            yield driver, machine, domain, client, xml

    @pytest.mark.parametrize("resource,value", [("ram", 1024), ("cores", 2)])
    @pytest.mark.parametrize("firmware", ["bios", "uefi", "uefi-source"])
    @pytest.mark.parametrize("active", [False, True])
    def test_resize_preserves_persistent_configuration(
        self, resize_domain, resource, value, firmware, active
    ):
        driver, machine, domain, client, xml = resize_domain
        element = ET.fromstring(xml.xml)
        if firmware != "bios":
            # QEMU rejects plain undefine for persistent UEFI/NVRAM.
            domain.undefine.side_effect = libvirt.libvirtError(
                "cannot undefine domain with nvram"
            )
            os = element.find("os")
            os.set("firmware", "efi")
            ET.SubElement(
                os, "loader", readonly="yes", secure="yes", type="pflash"
            ).text = "/test-firmware.fd"
            nvram = ET.SubElement(os, "nvram", template="/test-vars.fd", format="raw")
            if firmware == "uefi-source":
                nvram.set("type", "file")
                ET.SubElement(nvram, "source", file="/test-nvram.fd")
            else:
                nvram.text = "/test-nvram.fd"
        # Keep the namespace prefix used by the SDK's metadata helpers.
        ET.register_namespace("genesis", libvirt_driver.GENESIS_NS)
        original = ET.tostring(element, encoding="unicode")
        domain.XMLDesc.return_value = original
        domain.isActive.return_value = int(active)
        calls = mock.Mock()
        calls.attach_mock(client.defineXML, "define")
        calls.attach_mock(domain.destroy, "destroy")
        calls.attach_mock(domain.create, "start")

        getattr(driver, f"set_machine_{resource}")(machine, value)

        domain.XMLDesc.assert_called_once_with(
            libvirt.VIR_DOMAIN_XML_INACTIVE | libvirt.VIR_DOMAIN_XML_SECURE
        )
        domain.undefine.assert_not_called()
        domain.undefineFlags.assert_not_called()
        assert calls.mock_calls == [mock.call.define(mock.ANY)] + (
            [mock.call.destroy()] if active else []
        ) + [mock.call.start()]
        updated = ET.fromstring(client.defineXML.call_args.args[0])
        assert getattr(machine, resource) == value
        if resource == "ram":
            assert updated.find("memory").text == str(value)
            assert updated.find("memory").get("unit") == "MiB"
            assert updated.find("currentMemory").text == str(value)
            assert updated.find(f".//{{{libvirt_driver.GENESIS_NS}}}mem").text == str(
                value
            )
            tags = ["memory", "currentMemory", f"{{{libvirt_driver.GENESIS_NS}}}mem"]
        else:
            assert updated.find("vcpu").text == str(value)
            assert updated.find(f".//{{{libvirt_driver.GENESIS_NS}}}vcpu").text == str(
                value
            )
            tags = ["vcpu", f"{{{libvirt_driver.GENESIS_NS}}}vcpu"]
        # Everything except the requested resource and its SDK metadata
        # must survive, including firmware, disk order and device addresses.
        for root in (element, updated):
            for parent in root.iter():
                for child in list(parent):
                    if child.tag in tags:
                        parent.remove(child)
        assert ET.tostring(updated) == ET.tostring(element)

    @pytest.mark.parametrize("resource,value", [("ram", 1024), ("cores", 2)])
    def test_invalid_definition_does_not_stop_guest(
        self, resize_domain, resource, value
    ):
        driver, machine, domain, client, xml = resize_domain
        domain.XMLDesc.return_value = xml.xml
        client.defineXML.side_effect = libvirt.libvirtError("invalid configuration")
        original = getattr(machine, resource)

        with pytest.raises(libvirt.libvirtError, match="invalid configuration"):
            getattr(driver, f"set_machine_{resource}")(machine, value)

        domain.destroy.assert_not_called()
        domain.create.assert_not_called()
        domain.undefine.assert_not_called()
        assert getattr(machine, resource) == original

    def test_stop_failure_is_not_ignored(self, resize_domain):
        driver, machine, domain, client, xml = resize_domain
        domain.XMLDesc.return_value = xml.xml
        domain.destroy.side_effect = libvirt.libvirtError("cannot stop domain")

        with pytest.raises(libvirt.libvirtError, match="cannot stop domain"):
            driver.set_machine_ram(machine, 1024)

        domain.create.assert_not_called()
        domain.undefine.assert_not_called()
        assert machine.ram == 512

    @pytest.mark.parametrize("resource,value", [("ram", 1024), ("cores", 2)])
    def test_libvirt_round_trip_preserves_pending_config(self, resource, value):
        driver = _local_driver()
        machine = pool_base.Machine(
            uuid=sys_uuid.uuid4(),
            project_id=sys_uuid.uuid4(),
            name="cassi-resize-round-trip",
            cores=1,
            ram=512,
        )
        driver.create_machine(machine, volumes=[], ports=[])
        domain = driver._client.lookupByUUIDString(str(machine.uuid))
        try:
            domain.setAutostart(False)
            persistent = ET.fromstring(domain.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE))
            ET.SubElement(persistent, "description").text = "pending configuration"
            driver._client.defineXML(ET.tostring(persistent, encoding="unicode"))
            assert ET.fromstring(domain.XMLDesc()).find("description") is None

            getattr(driver, f"set_machine_{resource}")(machine, value)

            updated = driver._client.lookupByUUIDString(str(machine.uuid))
            assert updated.isActive()
            assert updated.isPersistent()
            assert not updated.autostart()
            assert (
                ET.fromstring(updated.XMLDesc()).find("description").text
                == "pending configuration"
            )
            actual, _ = driver.get_machine(machine.uuid)
            assert getattr(actual, resource) == value
            assert actual.uuid == machine.uuid
        finally:
            driver.delete_machine(machine, delete_volumes=False)


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


def test_recreate_preserves_global_disk_order_across_pools():
    driver = _multi_pool_driver([{"name": "default-pool"}, {"name": "second-pool"}])
    _define_second_pool(driver)
    machine = pool_base.Machine(
        uuid=sys_uuid.uuid4(),
        project_id=sys_uuid.uuid4(),
        name="multi-pool-order",
        cores=1,
        ram=512,
    )
    root_uuid, data_uuid = sys_uuid.uuid4(), sys_uuid.uuid4()
    root = driver.create_volume(
        pool_base.MachineVolume(
            uuid=root_uuid,
            name=str(root_uuid),
            project_id=machine.project_id,
            size=1,
            storage_pool="second-pool",
            index=0,
        )
    )
    data = driver.create_volume(
        pool_base.MachineVolume(
            uuid=data_uuid,
            name=str(data_uuid),
            project_id=machine.project_id,
            size=1,
            storage_pool="default-pool",
            index=1,
        )
    )
    port = pool_base.Port(
        uuid=sys_uuid.uuid4(),
        project_id=machine.project_id,
        mac="52:54:00:11:22:44",
        source="default",
        status="ACTIVE",
    )
    driver.create_machine(machine, [root, data], [port])

    def disk_paths():
        xml = ET.fromstring(
            driver._client.lookupByUUIDString(str(machine.uuid)).XMLDesc()
        )
        return [
            (disk.find("target").get("dev"), disk.find("source").get("file"))
            for disk in xml.findall("./devices/disk")
            if disk.get("device") == "disk"
        ]

    try:
        before = disk_paths()
        listed = list(driver.list_volumes(machine))
        assert [(volume.uuid, volume.index) for volume in listed] == [
            (root.uuid, 0),
            (data.uuid, 1),
        ]
        driver.recreate_machine(machine, ports=[port])
        assert disk_paths() == before
    finally:
        driver.delete_machine(machine, delete_volumes=True)
