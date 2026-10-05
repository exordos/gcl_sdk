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
from __future__ import annotations

from pathlib import Path
import subprocess

from restalchemy.dm import properties
from restalchemy.dm import types

from gcl_sdk.agents.universal.drivers import meta
from gcl_sdk.agents.universal.drivers import storage_capacity
from gcl_sdk.agents.universal.drivers.rawstor_cluster import _write_if_changed

OST_UNIT_DIR = Path("/etc/systemd/system")
OST_UNIT_TEMPLATE = """[Unit]
Description=Rawstor OST for Exordos storage node {uuid}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=rawstor
Group=rawstor
ExecStart=/usr/bin/rawstor-ost --bind={bind_address} {location}
Restart=always
RestartSec=5
ProtectSystem=strict
ReadWritePaths={path}
ProtectHome=true
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
"""


class MetaStorageNode(meta.MetaCoordinatorDataPlaneModel):
    cluster = properties.property(types.UUID(), required=True)
    agent = properties.property(types.AllowNone(types.UUID()), default=None)
    failure_domain_path = properties.property(types.String(max_length=255), default="")
    endpoint = properties.property(types.String(max_length=2048), required=True)
    location = properties.property(types.String(max_length=2048), required=True)
    bind_address = properties.property(types.String(max_length=255), required=True)
    status = properties.property(
        types.Enum(["NEW", "IN_PROGRESS", "ACTIVE", "ERROR"]), default="NEW"
    )

    @property
    def unit_name(self):
        return f"rawstor-ost@{self.uuid}.service"

    def restore_from_dp(self, **kwargs):
        path = storage_capacity.validate_ost_configuration(
            self.location, self.bind_address
        )
        # The backing directory survives resource deletion. Never erase OST data.
        subprocess.run(
            ["install", "-d", "-o", "rawstor", "-g", "rawstor", "-m", "0750", path],
            check=True,
        )
        changed = _write_if_changed(
            OST_UNIT_DIR / self.unit_name,
            OST_UNIT_TEMPLATE.format(
                uuid=self.uuid,
                location=self.location,
                bind_address=self.bind_address,
                path=path,
            ),
        )
        if changed:
            subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", "--now", self.unit_name], check=True)
        if changed:
            subprocess.run(["systemctl", "restart", self.unit_name], check=True)
        import rawstor

        # A running unit alone is insufficient: verify the advertised OST responds.
        rawstor.Location(self.endpoint).info()
        self.status = "ACTIVE"

    def dump_to_dp(self, **kwargs):
        self.restore_from_dp(**kwargs)

    def update_on_dp(self, **kwargs):
        self.restore_from_dp(**kwargs)

    def delete_from_dp(self, **kwargs):
        unit = OST_UNIT_DIR / self.unit_name
        if unit.exists():
            subprocess.run(
                ["systemctl", "disable", "--now", self.unit_name], check=True
            )
            unit.unlink()
        elif (
            subprocess.run(
                ["systemctl", "is-active", "--quiet", self.unit_name], check=False
            ).returncode
            == 0
        ):
            subprocess.run(["systemctl", "stop", self.unit_name], check=True)
        subprocess.run(["systemctl", "daemon-reload"], check=True)


class StorageNodeAgentDriver(meta.MetaCoordinatorAgentDriver):
    __model_map__ = {"storage_node": MetaStorageNode}
    __coordinator_map__ = {"storage_node": {}}

    def delete(self, resource):
        # The MDS has already acknowledged removal. Do not require a healthy
        # endpoint, or restart the OST, just to stop a failed/unreachable daemon.
        model = MetaStorageNode.from_ua_resource(resource)
        model.delete_from_dp()
        self._delete_from_meta(resource.kind, resource.uuid)
        self._coordinator_storage[resource.kind].pop(resource.uuid, None)
