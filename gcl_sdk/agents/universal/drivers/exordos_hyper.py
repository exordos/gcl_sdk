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
from gcl_sdk.infra import constants as ic

LOG = logging.getLogger(__name__)

# Matches `RuntimeDirectory=rawstor` and `--socket-path=/run/rawstor/%i.sock`
# in librawstor's rawstor-vhost@.service unit.
SOCKET_DIR = "/run/rawstor"
VHOST_UNIT_TEMPLATE = "rawstor-vhost@{}"

SOCKET_WAIT_TIMEOUT = 5.0
SOCKET_WAIT_INTERVAL = 0.1


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
    def _locations(self) -> tp.Dict[str, rawstor.Location]:
        return {}

    def _rawstor_pool_names(self) -> tp.List[str]:
        return [entry["name"] for entry in self._spec.rawstor_pools]

    def _rawstor_pool_entry(self, name: str) -> dict:
        for entry in self._spec.rawstor_pools:
            if entry["name"] == name:
                return entry
        raise ValueError(f"Unknown rawstor pool {name!r}")

    def _location_for(self, name: str) -> rawstor.Location:
        if name not in self._locations:
            self._locations[name] = rawstor.Location(
                self._rawstor_pool_entry(name)["location"]
            )
        return self._locations[name]

    def _resolve_rawstor_pool(
        self, volume: pool_base.MachineVolume, probe: bool = False
    ) -> tp.Optional[str]:
        """Name of the rawstor pool `volume` is (or will be) on, or None
        if it belongs to a qcow2 pool instead.

        Backend choice is derived purely from which named pool a disk was
        scheduled onto - see ExordosLocalHyperDriverSpec.rawstor_pools -
        not from an explicit per-disk field.

        If `volume.storage_pool` isn't set (e.g. a pre-existing foreign
        disk, or a volume built outside the scheduler) and `probe` is
        set, check whether a rawstor object for it actually exists in any
        configured rawstor pool instead of guessing - used by operations
        on a volume that's expected to already exist (attach/detach/
        delete/resize), as opposed to create_volume, where it can't be.
        """
        if volume.storage_pool is not None:
            if volume.storage_pool in self._rawstor_pool_names():
                return volume.storage_pool
            return None

        if probe:
            rawstor_names = self._rawstor_pool_names()
            for name in rawstor_names:
                try:
                    rawstor.Target(self._target_uri(volume.uuid, name)).spec()
                except FileNotFoundError:
                    continue
                return name

            # Not found as a rawstor object anywhere. If rawstor is the
            # only backend configured at all, it must belong here anyway
            # (not created yet, or already deleted/detached) - let the
            # rawstor-specific logic below handle "doesn't exist" itself,
            # idempotently, rather than guessing it might be qcow2.
            if not self._storage_pool_names() and len(rawstor_names) == 1:
                return rawstor_names[0]

            return None

        # The object doesn't exist yet (create_volume) - only unambiguous
        # if exactly one pool of either kind is configured, same as
        # LibvirtPoolDriver's own single-pool fallback.
        rawstor_names = self._rawstor_pool_names()
        qcow2_names = self._storage_pool_names()
        if len(rawstor_names) + len(qcow2_names) == 1:
            return rawstor_names[0] if rawstor_names else None

        raise ValueError(
            f"Volume {volume.uuid} has no storage pool assigned and "
            f"{len(rawstor_names) + len(qcow2_names)} storage pools "
            f"are configured"
        )

    def _target_uri(self, volume_uuid: sys_uuid.UUID, pool_name: str) -> str:
        return f"{self._rawstor_pool_entry(pool_name)['location']}/{volume_uuid}"

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
        # `disable --now` on a unit that was never enabled is a no-op
        # (exit 0), so a nonzero exit here is a real failure to stop the
        # backend, not "it wasn't running" - letting it propagate is what
        # keeps delete_volume from removing the object out from under a
        # backend that's still (or still enabled to be) attached to it.
        subprocess.check_call(
            ["systemctl", "disable", "--now", self._vhost_unit(volume_uuid)]
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
        storage_pool: str,
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
            storage_pool=storage_pool,
        )

    def _list_rawstor_volumes(
        self,
        domains: tp.Collection[tp.Tuple[libvirt.virDomain, ET.Element]],
    ) -> tp.List[pool_base.MachineVolume]:
        attachments = self._rawstor_attachments(domains)
        volumes = []

        for name in self._rawstor_pool_names():
            for target in self._location_for(name):
                volume = self._target_to_volume(target, attachments, storage_pool=name)
                if volume is not None:
                    volumes.append(volume)

        return volumes

    def _build_storage_pool(
        self, name: str, volumes: tp.Collection[pool_base.MachineVolume]
    ) -> pool_base.ThinStoragePool:
        entry = self._rawstor_pool_entry(name)
        info = self._location_for(name).info()
        storage_pool = pool_base.ThinStoragePool(
            uuid=sys_uuid.uuid5(self._pool.uuid, name),
            name=name,
            capacity_usable=info.total >> 30,  # GB
            available_actual=(info.total - info.used) >> 30,  # GB
            pool_type="rawstor",
            oversubscription_ratio=1.0,
            speed=entry.get("speed", ic.DiskSpeed.WARM.value),
            ephemeral=entry.get("ephemeral", False),
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
        volumes = tuple(volumes)
        rawstor_pools = {
            v.uuid: self._resolve_rawstor_pool(v, probe=True) for v in volumes
        }

        if any(rawstor_pools.values()):
            # A rawstor volume is a vhost-user disk (attached now or later
            # via attach_volume), so the domain needs shared memory for
            # it - it can't be added after the domain is defined.
            domain.set_shared_memory()

        pool_info_cache: tp.Dict[str, tp.Tuple["libvirt_driver.StoragePoolType", str]] = {}
        for i, volume in enumerate(volumes):
            device = "vd" + chr(ord("a") + i)

            if rawstor_pools[volume.uuid] is not None:
                self._start_vhost(volume.uuid)
                domain.add_vhostuser_disk(
                    socket_path=self._socket_path(volume.uuid),
                    device=device,
                    bus="virtio",
                )
                continue

            pool_name = self._storage_pool_name_for(volume)
            if pool_name not in pool_info_cache:
                storage_pool = self._client.storagePoolLookupByName(pool_name)
                storage_pool_xml = ET.fromstring(storage_pool.XMLDesc())
                pool_info_cache[pool_name] = (
                    libvirt_driver.StoragePoolType(storage_pool_xml.get("type")),
                    storage_pool_xml.find("target").find("path").text,
                )
            pool_type, pool_path = pool_info_cache[pool_name]

            if not legacy_machine:
                domain.add_disk(
                    image_path=f"{pool_path}/{pool_type.volume_name(volume.name)}",
                    device=device,
                    bus="virtio",
                )
            else:
                # TODO(akremenetsky): Remove this snippet one day
                legacy_volume_name = pool_type.legacy_volume_name(
                    volume.name, machine.uuid
                )
                domain.add_disk(
                    image_path=f"{pool_path}/{legacy_volume_name}",
                    device=device,
                    bus="virtio",
                )

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
        volumes_by_pool: tp.Dict[str, tp.List[pool_base.MachineVolume]] = {}
        for v in rawstor_volumes:
            volumes_by_pool.setdefault(v.storage_pool, []).append(v)

        storage_pools = [
            self._build_storage_pool(name, volumes_by_pool.get(name, []))
            for name in self._rawstor_pool_names()
        ]
        volumes = list(rawstor_volumes)

        if self._storage_pool_names():
            # Some volumes of this pool are qcow2-backed (including any
            # pre-existing disk adopted from before this pool existed,
            # e.g. the core bootstrap VM's) - LibvirtPoolDriver already
            # knows how to discover those.
            _, qcow2_pools, _, qcow2_volumes = super().list_pool_resources()
            storage_pools.extend(qcow2_pools)
            volumes.extend(qcow2_volumes)

        return pool, tuple(storage_pools), machines, volumes

    def list_volumes(
        self, machine: tp.Optional[pool_base.Machine] = None
    ) -> tp.Iterable[pool_base.MachineVolume]:
        domains = self._domains_with_xml()
        volumes = self._list_rawstor_volumes(domains)

        if self._storage_pool_names():
            volumes = volumes + list(super().list_volumes())

        if machine is None:
            return volumes

        # Return volumes sorted by index for specific machine.
        # Otherwise volumes can be shuffled during machine recreation.
        machine_volumes = [v for v in volumes if v.machine == machine.uuid]
        machine_volumes.sort(key=operator.attrgetter("index"))
        return machine_volumes

    def get_volume(self, volume: sys_uuid.UUID) -> pool_base.MachineVolume:
        domains = self._domains_with_xml()
        attachments = self._rawstor_attachments(domains)

        for name in self._rawstor_pool_names():
            target = rawstor.Target(self._target_uri(volume, name))
            try:
                spec = target.spec()
            except FileNotFoundError:
                continue

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
                storage_pool=name,
            )

        # Not a rawstor object - maybe a qcow2 volume (including a
        # pre-existing disk adopted from before this pool existed, e.g.
        # the core bootstrap VM's).
        if self._storage_pool_names():
            try:
                return super().get_volume(volume)
            except pool_base.VolumeNotFoundError:
                pass

        raise pool_base.VolumeNotFoundError(volume=volume)

    @libvirt_driver.dry_run_decorator()
    def create_volume(self, volume: pool_base.MachineVolume) -> pool_base.MachineVolume:
        pool_name = self._resolve_rawstor_pool(volume)
        if pool_name is None:
            return super().create_volume(volume)

        target = rawstor.Target(self._target_uri(volume.uuid, pool_name))
        try:
            target.create(size=volume.size << 30)
        except FileExistsError:
            raise pool_base.VolumeAlreadyExistsError(volume=volume.uuid)

        volume.status = pool_base.VolumeStatus.ACTIVE.value
        LOG.debug("The rawstor volume %s has been created", volume.uuid)
        return volume

    @libvirt_driver.dry_run_decorator()
    def delete_volume(self, volume: pool_base.MachineVolume) -> None:
        pool_name = self._resolve_rawstor_pool(volume, probe=True)
        if pool_name is None:
            return super().delete_volume(volume)

        # The vhost-user backend is an attachment of the volume: don't
        # leave it running against an object that's about to disappear.
        # Idempotent - disabling a unit that was never enabled (the
        # volume may already have been detached, or never attached at
        # all) is a no-op - but a real failure to stop it must abort the
        # delete rather than orphan an enabled unit.
        self._stop_vhost(volume.uuid)

        target = rawstor.Target(self._target_uri(volume.uuid, pool_name))
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

        if self._resolve_rawstor_pool(volume, probe=True) is None:
            return super().attach_volume(volume)

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

        if self._resolve_rawstor_pool(volume, probe=True) is None:
            return super().detach_volume(volume)

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
        if self._resolve_rawstor_pool(volume, probe=True) is None:
            return super().resize_volume(volume)

        # rawstor has no resize API.
        raise pool_base.VolumeResizeNotSupportedError(volume=volume.uuid)

    def list_storage_pools(self) -> tp.List[pool_base.ThinStoragePool]:
        """List storage pools."""
        domains = self._domains_with_xml()
        rawstor_volumes = self._list_rawstor_volumes(domains)
        volumes_by_pool: tp.Dict[str, tp.List[pool_base.MachineVolume]] = {}
        for v in rawstor_volumes:
            volumes_by_pool.setdefault(v.storage_pool, []).append(v)

        return [
            self._build_storage_pool(name, volumes_by_pool.get(name, []))
            for name in self._rawstor_pool_names()
        ]
