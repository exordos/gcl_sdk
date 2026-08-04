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

import functools
import logging
import operator
import os
import subprocess
import time
import typing as tp
import uuid as sys_uuid
from xml.etree import ElementTree as ET

import libvirt
import rawstor

from gcl_sdk.agents.universal.drivers import libvirt as libvirt_driver
from gcl_sdk.agents.universal.drivers import pool as pool_base

LOG = logging.getLogger(__name__)

# Matches `RuntimeDirectory=rawstor` and `--socket-path=/run/rawstor/%i.sock`
# in librawstor's rawstor-vhost@.service unit.
SOCKET_DIR = "/run/rawstor"
VHOST_UNIT_TEMPLATE = "rawstor-vhost@{}"

SOCKET_WAIT_TIMEOUT = 5.0
SOCKET_WAIT_INTERVAL = 0.1

# rawstor has no capacity/stats API yet, so the pool's usable capacity is
# a fixed placeholder for now (mirrors StoragePoolType.oversubscription_ratio
# in libvirt.py: a driver-internal constant, not a per-pool driver_spec
# field the CLI/API would have to configure).
RAWSTOR_CAPACITY_GB = 100


class ExordosLocalHyperDriver(libvirt_driver.LibvirtPoolDriver):
    """Pool driver for a local hypervisor backed by rawstor volumes.

    Machine lifecycle (domains, ports) is unchanged from `LibvirtPoolDriver`.
    Only volumes differ: instead of qcow2/zvol files in a libvirt storage
    pool, each volume is a rawstor object exposed to the domain over a
    vhost-user-blk unix socket served by a per-volume `rawstor-vhost@<uuid>`
    systemd instance.
    """

    def __init__(self, pool: pool_base.MachinePool, dry_run: bool = False):
        super().__init__(pool, dry_run=dry_run)
        if not isinstance(self._spec, pool_base.ExordosLocalHyperDriverSpec):
            raise ValueError(f"Unsupported driver spec kind: {self._spec.KIND!r}")

    @functools.cached_property
    def _location(self) -> rawstor.Location:
        return rawstor.Location(self._spec.rawstor_location)

    def _target_uri(self, volume_uuid: sys_uuid.UUID) -> str:
        return f"{self._spec.rawstor_location}/{volume_uuid}"

    def _socket_path(self, volume_uuid: sys_uuid.UUID) -> str:
        return f"{SOCKET_DIR}/{volume_uuid}.sock"

    def _vhost_unit(self, volume_uuid: sys_uuid.UUID) -> str:
        return VHOST_UNIT_TEMPLATE.format(volume_uuid)

    def _uuid_from_socket_path(self, path: str) -> tp.Optional[sys_uuid.UUID]:
        name = path.rsplit("/", 1)[-1]
        if name.endswith(".sock"):
            name = name[: -len(".sock")]
        try:
            return sys_uuid.UUID(name)
        except ValueError:
            return None

    def _start_vhost(self, volume_uuid: sys_uuid.UUID) -> None:
        subprocess.check_call(
            ["systemctl", "enable", "--now", self._vhost_unit(volume_uuid)]
        )
        self._wait_for_socket(self._socket_path(volume_uuid))

    def _wait_for_socket(self, socket_path: str) -> None:
        deadline = time.monotonic() + SOCKET_WAIT_TIMEOUT
        while not os.path.exists(socket_path):
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"rawstor-vhost socket {socket_path} did not appear "
                    f"within {SOCKET_WAIT_TIMEOUT}s"
                )
            time.sleep(SOCKET_WAIT_INTERVAL)

    def _stop_vhost(self, volume_uuid: sys_uuid.UUID) -> None:
        try:
            subprocess.check_call(
                ["systemctl", "disable", "--now", self._vhost_unit(volume_uuid)]
            )
        except subprocess.CalledProcessError:
            LOG.debug(
                "Failed to stop rawstor-vhost for volume %s, "
                "perhaps it wasn't running",
                volume_uuid,
            )

    def _find_rawstor_disk(
        self, domain: ET.Element, volume_uuid: sys_uuid.UUID
    ) -> tp.Optional[ET.Element]:
        socket_path = self._socket_path(volume_uuid)
        for disk in domain.findall(".//devices/disk"):
            if disk.get("type") != "vhostuser":
                continue
            source = disk.find("source")
            if source is not None and source.get("path") == socket_path:
                return disk
        return None

    def _domains_with_xml(
        self,
    ) -> tp.Tuple[tp.Tuple[libvirt.virDomain, ET.Element], ...]:
        return tuple(
            (d, ET.fromstring(d.XMLDesc())) for d in self._client.listAllDomains()
        )

    def _rawstor_attachments(
        self,
        domains: tp.Collection[tp.Tuple[libvirt.virDomain, ET.Element]],
    ) -> tp.Dict[sys_uuid.UUID, tp.Tuple[libvirt.virDomain, int]]:
        """Map volume uuid -> (domain, disk index) for attached rawstor disks."""
        result = {}

        for domain, root in domains:
            idx = 0
            for disk in root.findall(".//devices/disk"):
                if disk.get("device") != "disk" or disk.get("type") != "vhostuser":
                    continue

                source = disk.find("source")
                path = source.get("path") if source is not None else None
                volume_uuid = self._uuid_from_socket_path(path) if path else None
                if volume_uuid is None:
                    LOG.warning(
                        "Unable to detect rawstor volume for disk %s",
                        ET.tostring(disk),
                    )
                    continue

                result[volume_uuid] = (domain, idx)
                idx += 1

        return result

    def _target_to_volume(
        self,
        target: "rawstor.Target",
        attachments: tp.Dict[sys_uuid.UUID, tp.Tuple[libvirt.virDomain, int]],
    ) -> tp.Optional[pool_base.MachineVolume]:
        volume_uuid = self._uuid_from_socket_path(target.uri)
        if volume_uuid is None:
            LOG.warning("Unable to detect volume uuid for rawstor target %s", target)
            return None

        try:
            spec = target.spec()
        except FileNotFoundError:
            return None

        domain, idx = attachments.get(volume_uuid, (None, None))
        machine_uuid = None if domain is None else sys_uuid.UUID(domain.UUIDString())

        return pool_base.MachineVolume(
            uuid=volume_uuid,
            machine=machine_uuid,
            name=str(volume_uuid),
            project_id=pool_base.SYSTEM_PROJECT_ID,
            size=spec.size >> 30,  # in GB
            index=idx if idx is not None else libvirt_driver.MAX_VOLUME_INDEX,
            status=pool_base.VolumeStatus.ACTIVE.value,
        )

    def _list_rawstor_volumes(
        self,
        domains: tp.Collection[tp.Tuple[libvirt.virDomain, ET.Element]],
    ) -> tp.List[pool_base.MachineVolume]:
        attachments = self._rawstor_attachments(domains)
        volumes = []

        for target in self._location:
            volume = self._target_to_volume(target, attachments)
            if volume is not None:
                volumes.append(volume)

        return volumes

    def _build_storage_pool(
        self, volumes: tp.Collection[pool_base.MachineVolume]
    ) -> pool_base.ThinStoragePool:
        storage_pool = pool_base.ThinStoragePool(
            uuid=self._pool.uuid,
            name=f"rawstor-{self._spec.node}",
            capacity_usable=RAWSTOR_CAPACITY_GB,
            pool_type="rawstor",
            oversubscription_ratio=1.0,
        )

        for volume in volumes:
            storage_pool.allocate_capacity(volume.size)

        return storage_pool

    def _add_volumes_to_domain(
        self,
        domain: "libvirt_driver.XMLLibvirtInstance",
        machine: pool_base.Machine,
        volumes: tp.Iterable[pool_base.MachineVolume],
        legacy_machine: bool = False,
    ) -> None:
        # Every volume of this pool is a vhost-user disk (attached now or
        # later via attach_volume), so the domain always needs shared
        # memory - it can't be added after the domain is defined.
        domain.set_shared_memory()

        for i, volume in enumerate(volumes):
            self._start_vhost(volume.uuid)
            domain.add_vhostuser_disk(
                socket_path=self._socket_path(volume.uuid),
                device="vd" + chr(ord("a") + i),
                bus="virtio",
            )

    def _list_foreign_volumes(
        self,
        domains: tp.Collection[tp.Tuple[libvirt.virDomain, ET.Element]],
    ) -> tp.List[pool_base.MachineVolume]:
        """List volumes on the plain libvirt storage pool, not rawstor.

        A machine can be adopted into this pool without its disks ever
        going through rawstor - the core bootstrap VM is created directly
        by the CLI on the plain libvirt storage pool, before this pool
        (or any agent) exists. Recognizing those disks here means the
        reconciler treats them as already satisfied instead of creating a
        rawstor volume and vhost-attaching it on top of them.
        """
        if not self._spec.storage_pool:
            return []

        try:
            storage_pool = self._client.storagePoolLookupByName(
                self._spec.storage_pool
            )
        except libvirt.libvirtError:
            return []

        return self._list_volumes(domains, storage_pool.listAllVolumes())

    def list_pool_resources(
        self,
    ) -> tp.Tuple[
        pool_base.MachinePool,
        tp.Collection[pool_base.AbstractStoragePool],
        tp.Collection[tp.Tuple[pool_base.Machine, tp.Tuple[pool_base.Port, ...]]],
        tp.Collection[pool_base.MachineVolume],
    ]:
        pool = self.get_pool_info()
        domains = self._domains_with_xml()
        machines = self._list_machines(domains)
        rawstor_volumes = self._list_rawstor_volumes(domains)
        foreign_volumes = self._list_foreign_volumes(domains)
        storage_pool = self._build_storage_pool(rawstor_volumes)

        return pool, (storage_pool,), machines, rawstor_volumes + foreign_volumes

    def list_volumes(
        self, machine: tp.Optional[pool_base.Machine] = None
    ) -> tp.Iterable[pool_base.MachineVolume]:
        domains = self._domains_with_xml()
        volumes = self._list_rawstor_volumes(domains) + self._list_foreign_volumes(
            domains
        )

        if machine is None:
            return volumes

        # Return volumes sorted by index for specific machine.
        # Otherwise volumes can be shuffled during machine recreation.
        machine_volumes = [v for v in volumes if v.machine == machine.uuid]
        machine_volumes.sort(key=operator.attrgetter("index"))
        return machine_volumes

    def get_volume(self, volume: sys_uuid.UUID) -> pool_base.MachineVolume:
        target = rawstor.Target(self._target_uri(volume))
        try:
            spec = target.spec()
        except FileNotFoundError:
            pass
        else:
            domains = self._domains_with_xml()
            attachments = self._rawstor_attachments(domains)
            domain, idx = attachments.get(volume, (None, None))
            machine_uuid = (
                None if domain is None else sys_uuid.UUID(domain.UUIDString())
            )

            return pool_base.MachineVolume(
                uuid=volume,
                machine=machine_uuid,
                name=str(volume),
                project_id=pool_base.SYSTEM_PROJECT_ID,
                size=spec.size >> 30,  # in GB
                index=idx if idx is not None else libvirt_driver.MAX_VOLUME_INDEX,
                status=pool_base.VolumeStatus.ACTIVE.value,
            )

        # Not a rawstor object - maybe a foreign (pre-existing) libvirt
        # volume, e.g. the core bootstrap VM's original qcow2 disks.
        domains = self._domains_with_xml()
        for foreign in self._list_foreign_volumes(domains):
            if foreign.uuid == volume:
                return foreign

        raise pool_base.VolumeNotFoundError(volume=volume)

    @libvirt_driver.dry_run_decorator()
    def create_volume(self, volume: pool_base.MachineVolume) -> pool_base.MachineVolume:
        target = rawstor.Target(self._target_uri(volume.uuid))
        try:
            target.create(size=volume.size << 30)
        except FileExistsError:
            raise pool_base.VolumeAlreadyExistsError(volume=volume.uuid)

        volume.status = pool_base.VolumeStatus.ACTIVE.value
        LOG.debug("The rawstor volume %s has been created", volume.uuid)
        return volume

    @libvirt_driver.dry_run_decorator()
    def delete_volume(self, volume: pool_base.MachineVolume) -> None:
        # The vhost-user backend is an attachment of the volume: don't
        # leave it running against an object that's about to disappear.
        # Idempotent/best-effort since the volume may already have been
        # detached (or never attached at all).
        self._stop_vhost(volume.uuid)

        target = rawstor.Target(self._target_uri(volume.uuid))
        try:
            target.remove()
        except FileNotFoundError:
            LOG.warning("The rawstor volume %s has not been found", volume.uuid)
            return

        LOG.debug("The rawstor volume %s has been deleted", volume.uuid)

    @libvirt_driver.dry_run_decorator()
    def attach_volume(self, volume: pool_base.MachineVolume) -> None:
        """Attach the volume."""
        if volume.machine is None:
            raise ValueError("Cannot attach volume without machine")

        try:
            domain = self._client.lookupByUUIDString(str(volume.machine))
        except libvirt.libvirtError as e:
            if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                raise pool_base.MachineNotFoundError(machine=volume.machine)
            raise

        domain_element = ET.fromstring(domain.XMLDesc())
        if self._find_rawstor_disk(domain_element, volume.uuid) is not None:
            raise pool_base.VolumeAlreadyAttachedError(
                volume=volume.uuid, machine=volume.machine
            )

        # A pre-existing (non-rawstor) disk may already occupy this
        # volume's slot - e.g. the core bootstrap VM's own qcow2 disks,
        # adopted by this pool without ever being migrated to rawstor.
        # Nothing to attach.
        domains = self._domains_with_xml()
        if any(v.uuid == volume.uuid for v in self._list_foreign_volumes(domains)):
            raise pool_base.VolumeAlreadyAttachedError(
                volume=volume.uuid, machine=volume.machine
            )

        self._start_vhost(volume.uuid)

        devices = len(domain_element.findall(".//devices/disk"))
        device_name = "vd" + chr(ord("a") + devices)
        disk_xml = libvirt_driver.XMLLibvirtInstance.vhostuser_disk_xml(
            self._socket_path(volume.uuid), device_name
        )

        flags = libvirt.VIR_DOMAIN_AFFECT_LIVE | libvirt.VIR_DOMAIN_AFFECT_CONFIG
        try:
            domain.attachDeviceFlags(disk_xml, flags)
        except libvirt.libvirtError as e:
            if e.get_error_code() == libvirt.VIR_ERR_OPERATION_INVALID:
                raise pool_base.VolumeAlreadyAttachedError(
                    volume=volume.uuid, machine=volume.machine
                )
            raise

    @libvirt_driver.dry_run_decorator()
    def detach_volume(self, volume: pool_base.MachineVolume) -> None:
        """Detach the volume."""
        if volume.machine is None:
            raise ValueError("Cannot detach volume without machine")

        try:
            domain = self._client.lookupByUUIDString(str(volume.machine))
        except libvirt.libvirtError as e:
            if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                raise pool_base.MachineNotFoundError(machine=volume.machine)
            raise

        domain_element = ET.fromstring(domain.XMLDesc())
        disk = self._find_rawstor_disk(domain_element, volume.uuid)
        if disk is None:
            raise pool_base.VolumeNotAttachedError(
                volume=volume.uuid, machine=volume.machine
            )

        flags = libvirt.VIR_DOMAIN_AFFECT_LIVE | libvirt.VIR_DOMAIN_AFFECT_CONFIG
        try:
            domain.detachDeviceFlags(ET.tostring(disk, "unicode"), flags)
        except libvirt.libvirtError as e:
            if e.get_error_code() == libvirt.VIR_ERR_OPERATION_INVALID:
                raise pool_base.VolumeNotAttachedError(
                    volume=volume.uuid, machine=volume.machine
                )
            raise

        self._stop_vhost(volume.uuid)

    @libvirt_driver.dry_run_decorator()
    def resize_volume(self, volume: pool_base.MachineVolume) -> None:
        """Resize the volume."""
        # rawstor has no resize API.
        raise pool_base.VolumeResizeNotSupportedError(volume=volume.uuid)

    def list_storage_pools(self) -> tp.List[pool_base.ThinStoragePool]:
        """List storage pools."""
        return [self._build_storage_pool(self.list_volumes())]
