#    Copyright 2025 Genesis Corporation.
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

import logging
import os
import subprocess

from gcl_sdk.agents.universal import constants as c
from gcl_sdk.agents.universal.drivers import exceptions as driver_exc
from gcl_sdk.agents.universal.drivers import meta
from gcl_sdk.infra import constants as ic
from gcl_sdk.paas.dm import services as s_models

LOG = logging.getLogger(__name__)
SERVICE_TARGET_KIND = "service_agent_node"
SERVICES_DIR = "/etc/systemd/system/"
NAME_PREFIX = "exordos_srv"

SERVICE_TEMPLATE = """\
[Unit]
Description=Exordos Core: dynamic service {name}
After=network.target

[Service]
Type={service_type}
Restart={restart}
ExecStart=/usr/bin/bash -c '{command}'
User={user}
{group}
{pre}
{post}
RestartSec=5s
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
"""

TYPES_MAPPING = {
    "simple": "simple",
    "oneshot": "oneshot",
    "monopoly": "simple",
    "monopoly_oneshot": "oneshot",
}
CONDITIONS_MAPPING = {
    "before": {"service": "After", "shell": "ExecStartPre"},
    "after": {"service": "Before", "shell": "ExecStartPost"},
}


class Service(s_models.Service, meta.MetaDataPlaneModel):
    def get_my_name(self):
        return self.get_service_name(self.name, self.uuid)

    def get_service_name(self, name, uuid):
        return f"{NAME_PREFIX}_{name}_{uuid}.service"

    def get_meta_model_fields(self) -> set[str] | None:
        """Return a list of meta fields or None.

        Meta fields are the fields that cannot be fetched from
        the data plane or we just want to save them into the meta file.

        `None` means all fields are meta fields but it doesn't mean they
        won't be updated from the data plane.
        """

        # Keep all fields as meta fields for simplicity
        return {
            "uuid",
            "name",
            "path",
            "user",
            "group",
            "service_type",
            "before",
            "target_status",
            "after",
        }

    def _parse_conditions(self, conditions, when):
        res = []
        for cond in conditions:
            if cond.kind == "service":
                # Disabled, service relationships should be reworked
                # res.append(f"{CONDITIONS_MAPPING[when]["service"]}={self.get_service_name(cond.service, cond.service_name)}")
                continue
            cmd = cond.command.replace("'", "\\'")
            res.append(f"{CONDITIONS_MAPPING[when]['shell']}=+/usr/bin/bash -c '{cmd}'")
        return res

    def _gen_file_content(self):
        return SERVICE_TEMPLATE.format(
            name=self.name,
            service_type=TYPES_MAPPING[self.service_type.kind],
            restart=(
                "always"
                if not self.service_type.kind.endswith("oneshot")
                else "on-failure"
            ),
            command=self.path.replace("'", "\\'"),
            user=self.user or "root",
            group=f"Group={self.group}" if self.group else "",
            pre="\n".join(self._parse_conditions(self.before, "before")),
            post="\n".join(self._parse_conditions(self.after, "after")),
        )

    def dump_to_dp(self) -> None:
        with open(f"{SERVICES_DIR}{self.get_my_name()}", "w") as f:
            f.write(self._gen_file_content())

        subprocess.check_call(["systemctl", "daemon-reload"])
        if self.target_status == "enabled":
            subprocess.check_call(["systemctl", "enable", "--now", self.get_my_name()])
        else:
            subprocess.check_call(["systemctl", "disable", "--now", self.get_my_name()])
        self.status = ic.InstanceStatus.ACTIVE.value

    def restore_from_dp(self) -> None:
        try:
            subprocess.check_output(["systemctl", "is-active", self.get_my_name()])
            self.status = ic.InstanceStatus.ACTIVE.value
        except subprocess.CalledProcessError as e:
            # It's ok that oneshot is already finished and inactive
            if self.service_type.kind.endswith("oneshot"):
                if e.output == b"inactive\n":
                    self.status = ic.InstanceStatus.ACTIVE.value
            else:
                raise driver_exc.InvalidDataPlaneObjectError(
                    obj={"uuid": str(self.uuid)}
                )

        # Force file validation
        try:
            with open(f"{SERVICES_DIR}{self.get_my_name()}", "r") as f:
                if self._gen_file_content() != f.read():
                    raise driver_exc.InvalidDataPlaneObjectError(
                        obj={"uuid": str(self.uuid)}
                    )
        except FileNotFoundError:
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})

    def delete_from_dp(self) -> None:
        subprocess.check_call(["systemctl", "disable", "--now", self.get_my_name()])
        try:
            os.remove(f"{SERVICES_DIR}{self.get_my_name()}")
        except FileNotFoundError:
            pass
        subprocess.check_call(["systemctl", "daemon-reload"])

    def update_on_dp(self) -> None:
        """Update the resource on the data plane."""
        # The simplest implementation, just recreate.
        self.delete_from_dp()
        self.dump_to_dp()


class ServiceCapabilityDriver(meta.MetaFileStorageAgentDriver):
    SERVICE_META_PATH = os.path.join(c.WORK_DIR, "service_meta.json")

    __model_map__ = {SERVICE_TARGET_KIND: Service}

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, meta_file=self.SERVICE_META_PATH, **kwargs)
