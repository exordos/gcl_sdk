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

from restalchemy.dm import properties
from restalchemy.dm import types

from gcl_sdk.agents.universal import constants as c
from gcl_sdk.agents.universal.drivers import exceptions
from gcl_sdk.agents.universal.drivers import meta

LOG = logging.getLogger(__name__)
AUTHORIZED_KEYS_PATH = ".ssh/authorized_keys"
SSH_KEY_TARGET_KIND = "ssh_key_target"


class SSHKey(meta.MetaDataPlaneModel):
    """SSH key model."""

    HOME = "/home"

    user = properties.property(types.String(min_length=1, max_length=64))
    authorized_keys = properties.property(
        types.String(min_length=1, max_length=256),
        default=AUTHORIZED_KEYS_PATH,
    )
    target_public_key = properties.property(
        types.String(max_length=10240),
        default="",
    )

    def get_meta_model_fields(self) -> set[str] | None:
        """Return a list of meta fields or None.

        Meta fields are the fields that cannot be fetched from
        the data plane or we just want to save them into the meta file.

        `None` means all fields are meta fields but it doesn't mean they
        won't be updated from the data plane.
        """

        # Keep all fields as meta fields for simplicity
        return {"uuid", "user", "authorized_keys", "target_public_key"}

    def _target_key_line(self) -> str:
        """Return the public key without serialized line terminators."""
        return self.target_public_key.rstrip("\r\n")

    def dump_to_dp(self) -> None:
        """Save the key to the data plane."""

        # Check if authorized_keys file exists
        authorized_keys = os.path.join(self.HOME, self.user, self.authorized_keys)
        if not os.path.exists(authorized_keys):
            os.makedirs(os.path.dirname(authorized_keys), exist_ok=True)

            # Set the file mode, owner and group
            mode = int("0600", base=8)

            try:
                owner = pwd.getpwnam(self.user).pw_uid
            except KeyError:
                raise ValueError(f"User {self.user} does not exist")

            try:
                group = grp.getgrnam(self.user).gr_gid
            except KeyError:
                raise ValueError(f"Group {self.user} does not exist")

            with open(authorized_keys, "w") as f:
                f.write("")

            os.chmod(authorized_keys, mode)
            os.chown(authorized_keys, owner, group)

        # Add the key if it doesn't exist
        with open(authorized_keys, "r") as f:
            content = f.read()
        target_public_key = self._target_key_line()
        if target_public_key not in content.splitlines():
            with open(authorized_keys, "a") as f:
                f.write(f"{target_public_key}\n")

    def restore_from_dp(self) -> None:
        """Load the key from the file system."""
        authorized_keys = os.path.join(self.HOME, self.user, self.authorized_keys)
        if not os.path.exists(authorized_keys):
            resource = self.to_ua_resource(SSH_KEY_TARGET_KIND)
            raise exceptions.ResourceNotFound(resource=resource)

        with open(authorized_keys, "r") as f:
            content = f.read()
        target_public_key = self._target_key_line()
        for key in content.splitlines():
            if key == target_public_key:
                return

        resource = self.to_ua_resource(SSH_KEY_TARGET_KIND)
        raise exceptions.ResourceNotFound(resource=resource)

    def delete_from_dp(self) -> None:
        """Delete the key from the data plane."""
        authorized_keys = os.path.join(self.HOME, self.user, self.authorized_keys)
        if not os.path.exists(authorized_keys):
            return

        with open(authorized_keys, "r") as f:
            content = f.read()
        target_public_key = self._target_key_line()
        with open(authorized_keys, "w") as f:
            for key in content.splitlines():
                if not key:
                    continue
                if key == target_public_key:
                    continue
                f.write(f"{key}\n")

    def update_on_dp(self) -> None:
        """Update the resource on the data plane."""
        # The simplest implementation, just recreate.
        self.delete_from_dp()
        self.dump_to_dp()


class SSHKeyCapabilityDriver(meta.MetaFileStorageAgentDriver):
    SSH_KEY_META_PATH = os.path.join(c.WORK_DIR, "ssh_key_meta.json")

    __model_map__ = {SSH_KEY_TARGET_KIND: SSHKey}

    def __init__(self, *args, meta_file=None, **kwargs) -> None:
        if meta_file is None:
            meta_file = self.SSH_KEY_META_PATH
        super().__init__(*args, meta_file=meta_file, **kwargs)
