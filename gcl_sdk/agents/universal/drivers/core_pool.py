# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0

"""Use another Core's nodes as aggregate VM and disk resources."""

import copy
import json
import uuid

import yaml

from gcl_sdk.agents.universal.drivers import pool as models


def bootstrap_script(connection, machine, volumes, user_data):
    """Keep the provider agent's identity and state outside the workload agent."""
    metadata = {
        "machine": machine.dump_to_simple_view(),
        "volumes": [v.dump_to_simple_view() for v in volumes],
    }
    metadata["machine"].pop("user_data", None)
    data = yaml.safe_load(user_data)
    files = {f["path"]: f["content"] for f in data["write_files"]}
    node = str(uuid.UUID(files["/var/lib/exordos/node-id"].strip()))
    root = "/var/lib/exordos/core-provider/" + str(connection)
    config = files["/etc/exordos/agent.conf"].replace(
        "/var/lib/exordos/universal_agent/private_key", root + "/private_key"
    )
    key = files["/var/lib/exordos/universal_agent/private_key"]
    base_paths = {
        "/opt/exordos/install-agent",
        "/var/lib/exordos/node-id",
        "/var/lib/exordos/universal_agent/private_key",
        "/etc/exordos/agent.conf",
        "/etc/systemd/system/exordos-agent.service",
    }
    extra_files = [
        (f["path"], f["content"], int(f.get("permissions", "0600"), 8))
        for f in data["write_files"]
        if f["path"] not in base_paths
    ]
    transport_commands = [
        cmd
        for cmd in data.get("runcmd", [])
        if isinstance(cmd, list)
        and cmd
        and cmd[0] == "systemctl"
        and "exordos-agent.service" not in cmd
    ]
    unit_name = "exordos-provider-" + str(connection) + ".service"
    unit = f"""[Unit]
Description=Exordos workload agent
Wants=network-online.target
After=network-online.target
[Service]
ExecStart=/usr/bin/exordos-universal-agent --config-file {root}/agent.conf
Restart=always
RestartSec=5
BindReadOnlyPaths={root}/node-id:/var/lib/exordos/node-id
BindPaths={root}/state:/var/lib/exordos/universal_agent
[Install]
WantedBy=multi-user.target
"""
    return (
        "# exordos-provider " + json.dumps(metadata, separators=(",", ":")) + "\n"
        "import os, pathlib, subprocess\n"
        f"root = pathlib.Path({root!r})\n"
        "root.mkdir(parents=True, exist_ok=True, mode=0o700)\n"
        "(root / 'state').mkdir(exist_ok=True, mode=0o700)\n"
        f"files = {{'node-id': {node!r}, 'private_key': {key!r}, 'agent.conf': {config!r}}}\n"
        "for name, content in files.items():\n"
        "    path = root / name\n"
        "    with open(path, 'w', opener=lambda p, f: os.open(p, f, 0o600)) as stream:\n"
        "        stream.write(content)\n"
        f"for name, content, mode in {extra_files!r}:\n"
        "    path = pathlib.Path(name)\n"
        "    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)\n"
        "    path.write_text(content)\n"
        "    path.chmod(mode)\n"
        f"pathlib.Path('/etc/systemd/system/' + {unit_name!r}).write_text({unit!r})\n"
        "subprocess.run(['systemctl', 'daemon-reload'], check=True)\n"
        f"for command in {transport_commands!r}:\n"
        "    subprocess.run(command, check=True)\n"
        f"subprocess.run(['systemctl', 'enable', '--now', {unit_name!r}], check=True)\n"
    )


class CorePoolDriver(models.AbstractPoolDriver):
    native_provisioning = True

    def __init__(self, pool, provider):
        super().__init__()
        self.pool = pool
        self.cloud = provider

    def get_pool_info(self):
        hosts = self.cloud.hypervisors()
        return models.MachinePool(
            uuid=self.pool.uuid,
            driver_spec=self.pool.driver_spec,
            status="ACTIVE",
            **{
                field: sum(h[field] for h in hosts)
                for field in ("avail_cores", "avail_ram", "all_cores", "all_ram")
            },
        )

    def list_storage_pools(self):
        classes = {}
        for host in self.cloud.hypervisors():
            for storage in host.get("storage_pools", []):
                tier, ephemeral = (
                    storage.get("tier", "WARM"),
                    storage.get("ephemeral", True),
                )
                key = f"core:{tier}:{'ephemeral' if ephemeral else 'durable'}"
                value = classes.setdefault(
                    key,
                    {
                        "uuid": str(self.cloud.resource_id("storage", key)),
                        "name": key,
                        "kind": "thin_storage_pool",
                        "pool_type": "core",
                        "tier": tier,
                        "ephemeral": ephemeral,
                        "capacity_usable": 0,
                        "capacity_provisioned": 0,
                        "available_actual": 0,
                    },
                )
                value["capacity_usable"] += storage["capacity_usable"]
                value["capacity_provisioned"] += storage["capacity_provisioned"]
                value["available_actual"] = max(
                    value["available_actual"], storage["available_actual"]
                )
        return [
            models.ThinStoragePool.restore_from_simple_view(**value)
            for value in classes.values()
        ]

    def _config(self, machine):
        return self.cloud.get("config", machine)

    def _machine(self, node):
        identity = self.cloud.identity("node", node)
        config = self._config(identity)
        if config is None:
            raise ValueError("Remote machine bootstrap configuration is missing")
        target = self.cloud.metadata(config)["machine"]
        machine = models.Machine.restore_from_simple_view(**target)
        machine.cores, machine.ram = node["cores"], node["ram"]
        machine.name = node["name"]
        machine.status = node["status"]
        ports = []
        for port in self.cloud.list("port", node=node["uuid"]):
            if port.get("status") != "ACTIVE":
                machine.status = "IN_PROGRESS"
            if port.get("mac"):
                ports.append(
                    models.Port(
                        uuid=self.cloud.identity("port", port),
                        project_id=machine.project_id,
                        mac=port["mac"],
                        source=port["uuid"],
                        status=port["status"],
                    )
                )
        if config["status"] != "ACTIVE":
            machine.status = "IN_PROGRESS"
        return machine, tuple(ports)

    def list_machines(self):
        return [self._machine(n) for n in self.cloud.list("node") if self._belongs(n)]

    def _belongs(self, node):
        config = self._config(self.cloud.identity("node", node))
        if config is None:
            raise ValueError("Remote machine bootstrap configuration is missing")
        return self.cloud.metadata(config)["machine"].get("pool") == str(self.pool.uuid)

    def get_machine(self, machine):
        node = self.cloud.get("node", machine)
        if node is None:
            raise models.MachineNotFoundError(machine=machine)
        return self._machine(node)

    def list_pool_resources(self):
        return (
            self.get_pool_info(),
            self.list_storage_pools(),
            self.list_machines(),
            self.list_volumes(),
        )

    def create_machine(self, machine, volumes, ports):
        machine.pool = self.pool.uuid
        volumes = sorted(volumes, key=lambda v: v.index)
        ports = list(ports)
        if not volumes or volumes[0].index != 0 or not ports:
            raise ValueError("A Core machine requires a root disk and network ports")
        external_ports = [self.cloud.get("port", p.uuid) for p in ports]
        if any(
            p is None or not p.get("ipv4") or not p.get("mac") for p in external_ports
        ):
            raise ValueError("Remote ports are not ready")
        sub = self.cloud.client.get(
            self.cloud.path("subnet"), uuid.UUID(external_ports[0]["subnet"])
        )
        if str(sub["project_id"]) != self.cloud.project:
            raise ValueError("Remote subnet belongs to another project")
        disks = [
            {
                "size": v.size,
                **({"image": v.image} if v.image is not None else {}),
                "label": "root" if v.index == 0 else str(v.uuid),
                "tier": v.tier,
                "ephemeral": v.ephemeral,
            }
            for v in volumes
        ]
        script = bootstrap_script(
            self.cloud.config.connection, machine, volumes, machine.user_data
        )
        path = (
            "/var/lib/exordos/core-provider-"
            + str(self.cloud.config.connection)
            + ".py"
        )
        node = self.cloud.ensure(
            "node",
            machine.uuid,
            name=machine.name,
            cores=machine.cores,
            ram=machine.ram,
            disk_spec={"kind": "disks", "disks": disks},
            default_network={"network": sub["network"]},
            ports=[p["uuid"] for p in external_ports],
            bootstrap={
                "description": self.cloud.marker("config", machine.uuid),
                "path": path,
                "content": script,
                "command": f"/usr/bin/python3 {path}",
            },
        )
        return self._machine(node)

    def delete_machine(self, machine, delete_volumes=True):
        if not delete_volumes:
            raise ValueError(
                "Deleting a Core node while retaining its disks is unsupported"
            )
        self.cloud.delete("node", machine.uuid)

    def _volumes(self):
        for node in self.cloud.list("node"):
            if not self._belongs(node):
                continue
            identity = self.cloud.identity("node", node)
            config = self._config(identity)
            if config is None:
                continue
            targets = self.cloud.metadata(config)["volumes"]
            observed = self.cloud.client.filter(
                self.cloud.path("volume"),
                node=self.cloud.path("node") + node["uuid"],
                project_id=self.cloud.project,
            )
            for target in targets:
                suffix = "root-volume" if target["index"] == 0 else target["uuid"]
                remote_id = str(uuid.uuid5(uuid.UUID(node["uuid"]), suffix))
                actual = next((v for v in observed if v["uuid"] == remote_id), None)
                if actual is not None:
                    volume = models.MachineVolume.restore_from_simple_view(**target)
                    volume.size, volume.status = actual["size"], actual["status"]
                    yield volume, actual, node, config

    def list_volumes(self, machine=None):
        return [
            v
            for v, *_ in self._volumes()
            if machine is None or v.machine == machine.uuid
        ]

    def get_volume(self, volume):
        for item in self.list_volumes():
            if item.uuid == volume:
                return item
        raise models.VolumeNotFoundError(volume=volume)

    def create_volume(self, volume):
        if volume.machine is None:
            raise ValueError("Core provider disks must belong to a machine")
        # Core creates the disks from the node's disk specification atomically
        # with its VM intent. Until then this is pending, not allocated storage.
        try:
            return self.get_volume(volume.uuid)
        except models.VolumeNotFoundError:
            pending = models.MachineVolume.restore_from_simple_view(
                **volume.dump_to_simple_view()
            )
            pending.status = "IN_PROGRESS"
            return pending

    def delete_volume(self, volume):
        if any(v.uuid == volume.uuid for v in self.list_volumes()):
            raise ValueError(
                "Remove a Core provider disk through its node disk specification"
            )

    def resize_volume(self, volume):
        for observed, remote, node, config in self._volumes():
            if observed.uuid == volume.uuid:
                if volume.size < observed.size:
                    raise ValueError("Shrinking a provider disk is unsupported")
                spec = copy.deepcopy(node["disk_spec"])
                spec["disks"][volume.index]["size"] = volume.size
                self.cloud.update(
                    "node", self.cloud.identity("node", node), disk_spec=spec
                )
                return
        raise models.VolumeNotFoundError(volume=volume.uuid)

    def attach_volume(self, volume):
        current = self.get_volume(volume.uuid)
        if current.machine != volume.machine:
            raise ValueError("Moving Core provider disks between nodes is unsupported")

    def detach_volume(self, volume):
        raise ValueError("Detaching Core provider disks is unsupported")

    def attach_port(self, machine, port):
        node = self.cloud.get("node", machine.uuid)
        self.cloud.update("port", port.uuid, node=node["uuid"])

    def detach_port(self, machine, port):
        self.cloud.update("port", port.uuid, node=None)

    def set_machine_cores(self, machine, cores):
        self.cloud.update("node", machine.uuid, cores=cores)

    def resize_machine(self, machine, cores, ram):
        self.cloud.update("node", machine.uuid, cores=cores, ram=ram)

    def set_machine_ram(self, machine, ram):
        self.cloud.update("node", machine.uuid, ram=ram)

    def rename_machine(self, machine, name):
        self.cloud.update("node", machine.uuid, name=name)

    def recreate_machine(self, machine, ports=None):
        raise ValueError(
            "Core provider image changes require explicit node replacement"
        )

    def reset_machine(self, machine):
        raise ValueError("Remote Core does not expose VM power operations")

    def shutdown_machine(self, machine, force=False):
        raise ValueError("Remote Core does not expose VM power operations")

    def start_machine(self, machine):
        raise ValueError("Remote Core does not expose VM power operations")
