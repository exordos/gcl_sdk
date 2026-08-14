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

import uuid as sys_uuid
from unittest import mock
from xml.dom import minidom
from xml.etree import ElementTree as ET

import pytest

# The libvirt driver imports the `libvirt` python bindings at module level.
# They ship as the optional `gcl_sdk[libvirt]` extra, so skip this module
# instead of failing collection when it's not installed.
pytest.importorskip("libvirt")

from gcl_sdk.agents.universal.drivers import libvirt as libvirt_driver  # noqa: E402
from gcl_sdk.agents.universal.drivers import pool as pool_base  # noqa: E402


def _local_driver() -> libvirt_driver.LibvirtPoolDriver:
    # libvirt's built-in "test" driver simulates a hypervisor in-memory -
    # no real virtualization or daemon needed, so real libvirt calls
    # (lookupByUUIDString, etc.) can be exercised end-to-end.
    spec = pool_base.LibvirtPoolDriverSpec(connection_uri="test:///default")
    pool = pool_base.MachinePool(uuid=sys_uuid.uuid4(), name="test-pool", driver_spec=spec)
    return libvirt_driver.LibvirtPoolDriver(pool)


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


class TestListMachines:
    def test_a_foreign_domain_without_genesis_metadata_is_skipped(self):
        # Regression: a hypervisor this driver manages can also run domains
        # it never created (exordos_local_hyper pools share a node with
        # whatever else is running on it). Such domains have no
        # genesis:genesis metadata block at all, so parsing them the same
        # way as our own must not raise - they must be filtered out instead.
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
        driver.create_machine(machine, [], [])

        foreign_uuid = sys_uuid.uuid4()
        driver._client.defineXML(
            f"""
            <domain type="kvm">
              <name>foreign-vm</name>
              <uuid>{foreign_uuid}</uuid>
              <memory unit="MiB">512</memory>
              <currentMemory unit="MiB">512</currentMemory>
              <vcpu>1</vcpu>
              <os>
                <type arch="x86_64" machine="q35">hvm</type>
              </os>
            </domain>
            """
        )

        machines = driver.list_machines()

        machine_uuids = {m.uuid for m, _ in machines}
        assert machine_uuids == {machine.uuid}


class TestRemoveDirectChildren:
    def test_removes_only_direct_children_leaving_nested_matches_alone(self):
        # getElementsByTagName searches the whole subtree recursively -
        # a naive removeChild(node) on a match found deeper in the tree
        # (not a direct child of root) raises NotFoundErr.
        doc = minidom.parseString(
            "<root><a>direct</a><b><a>nested</a></b></root>"
        )
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
