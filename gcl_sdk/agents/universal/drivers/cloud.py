# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0

import uuid

from gcl_sdk.agents.universal.drivers import pool as models
from gcl_sdk.cloud.drivers import base


class ControllerPoolAgentDriver(models.PoolAgentDriver):
    def get_capabilities(self):
        return super().get_capabilities() + ["cloud_pool"]

    def list(self, capability):
        return [] if capability == "cloud_pool" else super().list(capability)


class CloudPoolDriver(models.AbstractPoolDriver):
    """Translate pool resources into provider operations on the controller."""

    native_provisioning = True

    def __init__(self, pool, dry_run=False):
        if pool.driver_spec.KIND != "cloud":
            raise ValueError("Not a cloud pool")
        if dry_run:
            raise ValueError("Cloud pool mutations require an active driver")
        super().__init__(dry_run=dry_run)
        self.pool = pool
        self.spec = pool.driver_spec
        self.cloud = base.load_driver(
            base.CloudConfig(
                connection=self.spec.connection,
                cloud=self.spec.cloud,
                driver=self.spec.driver,
                region=self.spec.region,
            )
        )

    def _machine(self, server):
        flavor = server.flavor
        if "vcpus" not in flavor:
            flavor = self.cloud.api.compute.get_flavor(
                flavor.get("id") or flavor["original_name"]
            )
        state = {"ACTIVE": "ACTIVE", "SHUTOFF": "IDLE", "ERROR": "ERROR"}.get(
            server.status, "IN_PROGRESS"
        )
        machine = models.Machine(
            uuid=self.cloud.identity(server, "server"),
            name=server.name,
            project_id=uuid.UUID(server.metadata["project"]),
            pool=self.pool.uuid,
            cores=flavor["vcpus"],
            ram=flavor["ram"],
            status=state,
            image=server.metadata["image"],
            boot="hd0",
        )
        ports = tuple(
            self._port(p)
            for p in self.cloud.api.network.ports(device_id=server.id)
            if self.cloud.identity(p, "port")
        )
        return machine, ports

    def _port(self, port):
        return models.Port(
            uuid=self.cloud.identity(port, "port"),
            project_id=models.SYSTEM_PROJECT_ID,
            mac=port.mac_address,
            source=port.id,
            status="ACTIVE",
        )

    def _volume(self, volume):
        metadata = volume.metadata
        attachments = volume.attachments or []
        machine = None
        if attachments:
            server = self.cloud.api.compute.get_server(attachments[0]["server_id"])
            machine = self.cloud.identity(server, "server")
        return models.MachineVolume(
            uuid=self.cloud.identity(volume, "volume"),
            name=volume.name,
            pool=self.pool.uuid,
            size=volume.size,
            machine=machine,
            project_id=uuid.UUID(metadata["project"]),
            image=metadata.get("image") or None,
            index=int(metadata.get("index", 0)),
            storage_pool=self.spec.volume_type,
            status="ACTIVE"
            if volume.status in {"available", "in-use"}
            else "ERROR"
            if volume.status.startswith("error")
            else "IN_PROGRESS",
        )

    def get_pool_info(self):
        compute = self.cloud.api.compute.get_limits().absolute
        total_cores = compute.max_total_cores
        total_ram = compute.max_total_ram_size
        return models.MachinePool(
            uuid=self.pool.uuid,
            driver_spec=self.spec,
            status="ACTIVE",
            all_cores=total_cores,
            all_ram=total_ram,
            avail_cores=max(0, total_cores - compute.total_cores_used),
            avail_ram=max(0, total_ram - compute.total_ram_used),
        )

    def list_storage_pools(self):
        limits = self.cloud.api.block_storage.get_limits().absolute
        total = limits.max_total_volume_gigabytes
        used = limits.total_gigabytes_used
        return [
            models.ThinStoragePool(
                uuid=uuid.uuid5(
                    self.spec.connection, "cinder:" + self.spec.volume_type
                ),
                name=self.spec.volume_type,
                pool_type="cinder",
                capacity_usable=total,
                capacity_provisioned=used,
                available_actual=max(0, total - used),
                ephemeral=False,
            )
        ]

    def list_pool_resources(self):
        return (
            self.get_pool_info(),
            self.list_storage_pools(),
            self.list_machines(),
            self.list_volumes(),
        )

    def list_machines(self):
        return [self._machine(s) for s in self.cloud.servers(self.pool.uuid)]

    def get_machine(self, machine):
        server = self.cloud.server(machine, self.pool.uuid)
        if server is None:
            raise models.MachineNotFoundError(machine=machine)
        return self._machine(server)

    def create_machine(self, machine, volumes, ports):
        provider_ports = []
        for port in ports:
            external = self.cloud.api.network.get_port(port.source)
            if self.cloud.identity(external, "port") != port.uuid:
                raise ValueError("Cloud port identity mismatch")
            provider_ports.append(external)
        provider_volumes = [
            self.cloud.volume(v.uuid, self.pool.uuid)
            for v in sorted(volumes, key=lambda v: v.index)
        ]
        if any(v is None or v.status != "available" for v in provider_volumes):
            raise ValueError("Cloud volumes are not available")
        server = self.cloud.ensure_server(
            machine.uuid,
            name=machine.name,
            pool=self.pool.uuid,
            project=machine.project_id,
            cores=machine.cores,
            ram=machine.ram,
            image=machine.image,
            ports=provider_ports,
            volumes=provider_volumes,
            user_data=machine.user_data,
            key_name=self.spec.key_name,
        )
        return self._machine(self.cloud.api.compute.get_server(server.id))

    def delete_machine(self, machine, delete_volumes=True):
        server = self.cloud.server(machine.uuid, self.pool.uuid)
        volumes = list(self.list_volumes(machine)) if delete_volumes else []
        if server:
            self.cloud.delete_server(server)
        for volume in volumes:
            self.delete_volume(volume)

    def list_volumes(self, machine=None):
        volumes = [self._volume(v) for v in self.cloud.volumes(self.pool.uuid)]
        return [v for v in volumes if machine is None or v.machine == machine.uuid]

    def get_volume(self, volume):
        external = self.cloud.volume(volume, self.pool.uuid)
        if external is None:
            raise models.VolumeNotFoundError(volume=volume)
        return self._volume(external)

    def create_volume(self, volume):
        external = self.cloud.ensure_volume(
            volume.uuid,
            name=volume.name,
            size=volume.size,
            pool=self.pool.uuid,
            project=volume.project_id,
            image=volume.image if volume.index == 0 else None,
            volume_type=self.spec.volume_type,
            index=volume.index,
        )
        return self._volume(external)

    def delete_volume(self, volume):
        external = self.cloud.volume(volume.uuid, self.pool.uuid)
        if external:
            self.cloud.delete_volume(external)

    def resize_volume(self, volume):
        external = self.cloud.volume(volume.uuid, self.pool.uuid)
        if external is None:
            raise models.VolumeNotFoundError(volume=volume.uuid)
        if external.size > volume.size:
            raise ValueError("Cinder volumes cannot shrink")
        if external.size < volume.size:
            self.cloud.api.block_storage.extend_volume(external.id, size=volume.size)

    def attach_volume(self, volume):
        server = self.cloud.server(volume.machine, self.pool.uuid)
        external = self.cloud.volume(volume.uuid, self.pool.uuid)
        if server is None or external is None:
            raise ValueError("Cloud attachment dependency is not ready")
        if any(a["server_id"] == server.id for a in external.attachments):
            return
        if external.attachments:
            raise ValueError("Volume is attached to another server")
        self.cloud.api.compute.create_volume_attachment(server.id, volumeId=external.id)

    def detach_volume(self, volume):
        external = self.cloud.volume(volume.uuid, self.pool.uuid)
        if external:
            for attachment in external.attachments:
                server = self.cloud.api.compute.get_server(attachment["server_id"])
                self.cloud._require(server, "server")
                self.cloud.api.compute.delete_volume_attachment(
                    server=server.id, volume=external.id
                )

    def attach_port(self, machine, port):
        server = self.cloud.server(machine.uuid, self.pool.uuid)
        external = self.cloud.api.network.get_port(port.source)
        if self.cloud.identity(external, "port") != port.uuid:
            raise ValueError("Cloud port identity mismatch")
        if external.device_id == server.id:
            return
        if external.device_id:
            raise ValueError("Port is attached to another server")
        self.cloud.api.compute.create_server_interface(server.id, port_id=external.id)

    def detach_port(self, machine, port):
        server = self.cloud.server(machine.uuid, self.pool.uuid)
        external = self.cloud.api.network.get_port(port.source)
        if self.cloud.identity(external, "port") != port.uuid:
            raise ValueError("Cloud port identity mismatch")
        if server and external.device_id == server.id:
            self.cloud.api.compute.delete_server_interface(external.id, server.id)

    def resize_machine(self, machine, cores, ram):
        server = self.cloud.server(machine.uuid, self.pool.uuid)
        target = self.cloud.flavor(cores, ram)
        self.cloud.api.compute.resize_server(server.id, flavor=target.id)
        server = self.cloud.api.compute.wait_for_server(
            server, status="VERIFY_RESIZE", wait=300
        )
        self.cloud.api.compute.confirm_server_resize(server.id)

    def set_machine_cores(self, machine, cores):
        self.resize_machine(machine, cores, machine.ram)

    def set_machine_ram(self, machine, ram):
        self.resize_machine(machine, machine.cores, ram)

    def reset_machine(self, machine):
        self.cloud.api.compute.reboot_server(
            self.cloud.server(machine.uuid, self.pool.uuid).id
        )

    def recreate_machine(self, machine, ports=None):
        raise ValueError("Cloud image changes require explicit node replacement")

    def rename_machine(self, machine, name):
        self.cloud.api.compute.update_server(
            self.cloud.server(machine.uuid, self.pool.uuid).id, name=name
        )

    def shutdown_machine(self, machine, force=False):
        self.cloud.api.compute.stop_server(
            self.cloud.server(machine.uuid, self.pool.uuid).id
        )

    def start_machine(self, machine):
        self.cloud.api.compute.start_server(
            self.cloud.server(machine.uuid, self.pool.uuid).id
        )
