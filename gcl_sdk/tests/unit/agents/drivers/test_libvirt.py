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


def test_interface_xml_ovs_virtualport_carries_iface_id():
    xml = libvirt_driver.XMLLibvirtInstance.interface_xml(
        iface_type="bridge",
        source="br-int",
        mac="52:54:00:aa:bb:cc",
        interface_id="11111111-2222-3333-4444-555555555555",
    )
    iface = ET.fromstring(xml)
    assert iface.find("source").get("bridge") == "br-int"
    vport = iface.find("virtualport")
    assert vport is not None and vport.get("type") == "openvswitch"
    params = vport.find("parameters")
    assert params.get("interfaceid") == "11111111-2222-3333-4444-555555555555"


def test_an_overlay_port_is_plugged_parked_not_as_a_trunk():
    """Untagged, an OVS port is a trunk: from the moment libvirt plugs the
    tap until the evpn agent tags it, the guest could send into and hear
    every tenant's VLAN on br-int. It waits on a VLAN the agent never
    hands out."""
    xml = libvirt_driver.XMLLibvirtInstance.interface_xml(
        iface_type="bridge",
        source="br-int",
        mac="52:54:00:aa:bb:cc",
        interface_id="11111111-2222-3333-4444-555555555555",
    )
    tags = ET.fromstring(xml).findall("vlan/tag")
    assert [t.get("id") for t in tags] == ["4095"]
    assert libvirt_driver.OVS_PARKING_VLAN not in range(1, 4095)


def test_interface_xml_without_id_has_no_virtualport():
    xml = libvirt_driver.XMLLibvirtInstance.interface_xml(
        iface_type="bridge", source="br0", mac="52:54:00:aa:bb:cc"
    )
    assert ET.fromstring(xml).find("virtualport") is None
    assert ET.fromstring(xml).find("vlan") is None


def _driver_with(spec_kwargs, existing_networks=()):
    drv = object.__new__(libvirt_driver.LibvirtPoolDriver)
    drv._spec = pool_base.LibvirtPoolDriverSpec(
        network="exordos-core-net",
        storage_pool="default",
        connection_uri="qemu:///system",
        **spec_kwargs,
    )

    client = mock.MagicMock()

    def lookup(name):
        if name in existing_networks:
            return mock.MagicMock()
        raise libvirt_driver.libvirt.libvirtError("no network")

    client.networkLookupByName.side_effect = lookup
    drv._client_instance = client
    client.isAlive.return_value = 1
    return drv


def _port(source, overlay=False):
    port = pool_base.Port(
        project_id=sys_uuid.UUID("12345678-c625-4fee-81d5-f691897b8142"),
        source=source,
        mac=pool_base.Port.generate_mac(),
    )
    port.overlay = overlay
    return port


def test_iface_args_overlay_port_goes_to_br_int():
    """A port of an overlay network: bridge br-int + openvswitch
    virtualport (iface-id)."""

    drv = _driver_with({"ovs": True})
    port = _port("private", overlay=True)
    args = drv._iface_args(port)
    assert args["iface_type"] == "bridge"
    assert args["source"] == "br-int"
    assert args["interface_id"] == str(port.uuid)


def test_iface_args_boot_port_keeps_network_path():
    """A boot/flat port must NOT get a virtualport even on an ovs pool —
    it would break the netboot NIC."""

    drv = _driver_with({"ovs": True})
    port = _port("exordos-core-boot-net")
    args = drv._iface_args(port)
    assert args["iface_type"] == "network"
    assert args["source"] == "exordos-core-boot-net"
    assert args["interface_id"] is None


def test_iface_args_flat_subnet_without_libvirt_network_is_not_overlay():
    """The regression that replaced a guess with an answer.

    A subnet of a flat network that no libvirt network is named after used
    to be mistaken for an overlay port and plugged into br-int, where
    nothing is wired — silently, the guest simply unreachable. It has to
    take the classic path, so libvirt refuses the unknown network aloud.
    """

    drv = _driver_with({"ovs": True})
    port = _port("fip-pool")
    args = drv._iface_args(port)
    assert args["iface_type"] == "network"
    assert args["source"] == "fip-pool"
    assert args["interface_id"] is None


def test_iface_args_non_ovs_pool_unchanged():
    drv = _driver_with({})
    port = _port("private", overlay=True)
    args = drv._iface_args(port)
    assert args["iface_type"] == "network"
    assert args["interface_id"] is None


def test_a_classic_port_without_a_source_takes_the_fallback():
    drv = _driver_with({})
    port = _port(None)

    assert drv._iface_args(port)["source"] is None
    assert drv._iface_args(port, fallback_source="br-hot")["source"] == "br-hot"


def test_attaching_a_port_without_a_source_uses_the_pools_iface_source():
    drv = _driver_with({"iface_source": "br-hot"})
    domain = drv._client_instance.lookupByUUIDString.return_value
    machine = mock.MagicMock(uuid=sys_uuid.uuid4())

    drv.attach_port(machine, _port(None))

    iface = ET.fromstring(domain.attachDeviceFlags.call_args.args[0])
    assert iface.find("source").get("network") == "br-hot"


def _rendered_iface(drv, port, rom_on_host):
    with mock.patch.object(libvirt_driver.os.path, "exists", return_value=rom_on_host):
        args = drv._iface_args(port)
    return ET.fromstring(libvirt_driver.XMLLibvirtInstance.interface_xml(**args))


def test_a_flat_guest_of_an_sdn_pool_boots_from_the_pools_rom():
    """The iPXE ROM is an overlay guest's; a flat guest of the same pool
    renders what it did before the pool served an overlay."""
    port = _port("exordos-core-net")
    for spec in ({"ovs": True}, {}):
        assert _rendered_iface(_driver_with(spec), port, True).find("rom") is None

    drv = _driver_with({"ovs": True, "iface_rom_file": "/pool.rom"})
    rom = _rendered_iface(drv, port, True).find("rom")
    assert rom.get("file") == "/pool.rom"


def test_an_overlay_guest_boots_from_the_ipxe_rom_the_host_has():
    drv = _driver_with({"ovs": True, "iface_rom_file": "/pool.rom"})
    port = _port("private", overlay=True)

    rom = _rendered_iface(drv, port, True).find("rom")
    assert rom.get("file") == libvirt_driver.OVERLAY_IPXE_ROM
    assert _rendered_iface(drv, port, False).find("rom").get("file") == "/pool.rom"


def test_a_port_without_the_overlay_answer_takes_the_classic_path():
    """The agent rebuilds a port from a few fields and a placeholder
    subnet, so the answer has to travel with it. Absent, the classic
    path is the safe reading."""
    drv = _driver_with({"ovs": True})

    bare = pool_base.Port(
        project_id=sys_uuid.UUID("12345678-c625-4fee-81d5-f691897b8142"),
        source="private",
        mac=pool_base.Port.generate_mac(),
    )
    assert drv._iface_args(bare)["interface_id"] is None
