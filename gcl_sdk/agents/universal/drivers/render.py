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

import grp
import logging
import os
import pwd
import subprocess

from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.dm import types_dynamic

from gcl_sdk.agents.universal import constants as c
from gcl_sdk.agents.universal.drivers import exceptions as driver_exc
from gcl_sdk.agents.universal.drivers import meta

LOG = logging.getLogger(__name__)


class AbstractRenderHooks:
    def on_change(self) -> None:
        raise NotImplementedError()


class OnChangeNoAction(types_dynamic.AbstractKindModel, AbstractRenderHooks):
    KIND = "no_action"

    def on_change(self) -> None:
        # Do nothing
        pass


class OnChangeShell(types_dynamic.AbstractKindModel, AbstractRenderHooks):
    KIND = "shell"

    command = properties.property(
        types.String(max_length=262144), required=True, default=""
    )

    def on_change(self) -> None:
        subprocess.check_output(self.command, shell=True)


class Render(meta.MetaDataPlaneModel):
    """Render of the configuration file."""

    path = properties.property(
        types.String(min_length=1, max_length=512),
        required=True,
    )
    mode = properties.property(types.String(max_length=4), default="0644")
    owner = properties.property(
        types.String(max_length=128),
        default="root",
    )
    group = properties.property(
        types.String(max_length=128),
        default="root",
    )
    on_change = properties.property(
        types_dynamic.KindModelSelectorType(
            types_dynamic.KindModelType(OnChangeNoAction),
            types_dynamic.KindModelType(OnChangeShell),
        ),
    )
    content = properties.property(types.AllowNone(types.String()), default=None)

    def get_meta_model_fields(self) -> set[str] | None:
        """Return a list of meta fields or None.

        Meta fields are the fields that cannot be fetched from
        the data plane or we just want to save them into the meta file.

        `None` means all fields are meta fields but it doesn't mean they
        won't be updated from the data plane.
        """
        return {"uuid", "on_change", "path"}

    def dump_to_dp(self) -> None:
        """Save the render to the file system."""
        if self.content is None:
            raise ValueError("Render content is empty")

        # Create the directory if it doesn't exist
        if not os.path.exists(os.path.dirname(self.path)):
            os.makedirs(os.path.dirname(self.path))

        # Save the content
        with open(self.path, "w") as f:
            f.write(self.content)

        # Set the file mode, owner and group
        mode = int(self.mode, base=8)

        try:
            owner = pwd.getpwnam(self.owner).pw_uid
        except KeyError:
            raise ValueError(f"User {self.owner} does not exist")

        try:
            group = grp.getgrnam(self.group).gr_gid
        except KeyError:
            raise ValueError(f"Group {self.group} does not exist")

        os.chmod(self.path, mode)
        os.chown(self.path, owner, group)

        self.on_change.on_change()

        LOG.info("Render saved to %s", self.path)

    def restore_from_dp(self) -> None:
        """Load the render from the file system."""
        if not os.path.exists(self.path):
            resource = self.to_ua_resource("render")
            raise driver_exc.ResourceNotFound(resource=resource)

        with open(self.path) as f:
            self.content = f.read()

        # Read the file mode, owner and group
        stat = os.stat(self.path)
        self.mode = f"0{oct(stat.st_mode)[-3:]}"
        self.owner = pwd.getpwuid(stat.st_uid).pw_name
        self.group = grp.getgrgid(stat.st_gid).gr_name

    def delete_from_dp(self) -> None:
        """Delete the resource from the data plane."""
        if os.path.exists(self.path):
            os.remove(self.path)

    def update_on_dp(self) -> None:
        """Update the resource on the data plane."""
        # The simplest implementation, just recreate.
        self.delete_from_dp()
        self.dump_to_dp()


class RenderAgentDriver(meta.MetaFileStorageAgentDriver):
    RENDER_META_PATH = os.path.join(c.WORK_DIR, "render_meta.json")

    __model_map__ = {"render": Render}

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, meta_file=self.RENDER_META_PATH, **kwargs)
