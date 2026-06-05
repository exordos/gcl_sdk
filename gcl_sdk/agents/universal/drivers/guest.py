#    Copyright 2025-2026 Genesis Corporation.
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

import json
import logging
import os
import subprocess
import pathlib

import yaml

from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.dm import types_network

from gcl_sdk.agents.universal.drivers import meta
from gcl_sdk.common import utils as common_utils
from gcl_sdk.agents.universal import constants as c

LOG = logging.getLogger(__name__)
GUEST_MACHINE_KIND = "guest_machine"

EXORDOS_DATA_DIR = pathlib.Path("/persist")
NETPLAN_DIR = EXORDOS_DATA_DIR / "netplan"
UPDATE_JSON_PATH = EXORDOS_DATA_DIR / "update.json"
SYSTEM_NETPLAN_DIR = pathlib.Path("/etc/netplan")
GRUB_DEFAULT_PATH = pathlib.Path("/etc/default/grub.d/50-cloudimg-settings.cfg")
IMAGE_PATH = pathlib.Path(c.WORK_DIR) / "image"


class GuestMachineMetaModel(meta.MetaDataPlaneModel):
    """Guest machine meta model."""

    GRUB_AUTONOMOUS_ENTRY = "Autonomous update mode"
    GRUB_DEFAULT_TIMEOUT = 5

    image = properties.property(types.String(), required=True)
    boot = properties.property(types.String(), required=True)
    hostname = properties.property(
        types.AllowNone(types_network.Hostname()), default=None
    )
    block_devices = properties.property(types.Dict(), default=dict)
    net_devices = properties.property(types.Dict(), default=dict)
    pci_devices = properties.property(types.Dict(), default=dict)

    status = properties.property(
        types.Enum([s.value for s in c.InstanceStatus]),
        default=c.InstanceStatus.ACTIVE.value,
    )

    def _set_hostname(self, hostname: str) -> None:
        """Set hostname in the system."""
        subprocess.check_call(["hostnamectl", "hostname", hostname])

    def _get_hostname(self) -> str:
        """Return hostname from the system."""
        return (
            subprocess.check_output(["hostnamectl", "hostname"]).decode("utf-8").strip()
        )

    def get_meta_model_fields(self) -> set[str] | None:
        """Return a list of meta fields or None.

        Meta fields are the fields that cannot be fetched from
        the data plane or we just want to save them into the meta file.

        `None` means all fields are meta fields but it doesn't mean they
        won't be updated from the data plane.
        """
        return {"uuid", "image", "boot", "hostname"}

    def dump_to_dp(self) -> None:
        """Apply the guest settings to the data plane."""
        if self.hostname:
            self._set_hostname(self.hostname)

    def restore_from_dp(self) -> None:
        """Load the guest settings from the data plane."""
        # If no hostname specified, use the one from the system
        if self.hostname:
            self.hostname = self._get_hostname()

        # Save the original image to be able to compare it on update
        if not IMAGE_PATH.exists():
            with open(IMAGE_PATH, "w", opener=common_utils.rw_owner_opener) as f:
                f.write(self.image)

    def delete_from_dp(self) -> None:
        """It's not applicable for the guest machine."""
        # Just do nothing.
        pass

    def _save_network_settings(self) -> None:
        """Save and convert netplan settings to JSON files.

        Reads all YAML files from /etc/netplan/, converts them to JSON,
        and saves to /persist/netplan/.
        """

        # Clean up old netplan configs
        if NETPLAN_DIR.exists():
            for f in NETPLAN_DIR.glob("*"):
                f.unlink()

        # Create target directory if it doesn't exist
        NETPLAN_DIR.mkdir(parents=True, exist_ok=True)

        if not SYSTEM_NETPLAN_DIR.exists():
            LOG.warning("System netplan directory not found: %s", SYSTEM_NETPLAN_DIR)
            return

        # Read all .yaml/.yml files from system netplan directory
        netplan_files = list(SYSTEM_NETPLAN_DIR.glob("*.yaml"))
        netplan_files.extend(SYSTEM_NETPLAN_DIR.glob("*.yml"))

        if not netplan_files:
            LOG.warning("No netplan files found in %s", SYSTEM_NETPLAN_DIR)
            return

        for netplan_file in netplan_files:
            # Read YAML content
            content = netplan_file.read_text(encoding="utf-8")
            # Parse YAML to Python dict
            yaml_data = yaml.safe_load(content) or {}
            # Convert to JSON and save
            json_filename = netplan_file.stem + ".json"
            json_path = NETPLAN_DIR / json_filename
            json_path.write_text(json.dumps(yaml_data, indent=2), encoding="utf-8")
            LOG.info("Converted %s to %s", netplan_file, json_path)

        LOG.info("Netplan settings saved to %s", NETPLAN_DIR)

    def _save_target_image(self) -> None:
        """Save the target image to update.json.

        Creates /persist/update.json with target image information.
        """
        # Read the original image from the file
        if IMAGE_PATH.exists():
            with open(IMAGE_PATH, "r") as f:
                orig_image = f.read().strip()
        else:
            raise FileNotFoundError(f"Image file not found: {IMAGE_PATH}")

        # Create target directory if it doesn't exist
        EXORDOS_DATA_DIR.mkdir(parents=True, exist_ok=True)

        update_data = {
            "target_image": self.image,
            "original_image": orig_image,
        }
        UPDATE_JSON_PATH.write_text(json.dumps(update_data, indent=2), encoding="utf-8")
        LOG.info("Update info saved to %s", UPDATE_JSON_PATH)

    def _update_grub_default(self) -> None:
        """Update the grub default boot item to 'Autonomous update mode'.

        Sets GRUB_DEFAULT by menu entry name (not fragile numeric index) and
        ensures GRUB_TIMEOUT is non-zero so the menu is actually displayed.
        Updates /etc/default/grub.d/50-cloudimg-settings.cfg and regenerates
        grub configuration.
        """
        # Update GRUB_DEFAULT using the entry name so it is index-independent
        result = subprocess.run(
            f'grep -q "^GRUB_DEFAULT=" {GRUB_DEFAULT_PATH}',
            shell=True,
            check=False,
        )
        grub_default_value = f'"{self.GRUB_AUTONOMOUS_ENTRY}"'
        if result.returncode == 0:
            subprocess.check_call(
                f'sed -i "s/^GRUB_DEFAULT=.*/GRUB_DEFAULT={grub_default_value}/" {GRUB_DEFAULT_PATH}',
                shell=True,
            )
            LOG.info("GRUB_DEFAULT updated to %s", grub_default_value)
        else:
            subprocess.check_call(
                f'echo "GRUB_DEFAULT={grub_default_value}" >> {GRUB_DEFAULT_PATH}',
                shell=True,
            )
            LOG.info("GRUB_DEFAULT added with value %s", grub_default_value)

        # Ensure GRUB_TIMEOUT is non-zero so the menu is visible on boot
        result = subprocess.run(
            f'grep -q "^GRUB_TIMEOUT=" {GRUB_DEFAULT_PATH}',
            shell=True,
            check=False,
        )
        if result.returncode == 0:
            subprocess.check_call(
                f'sed -i "s/^GRUB_TIMEOUT=.*/GRUB_TIMEOUT={self.GRUB_DEFAULT_TIMEOUT}/" {GRUB_DEFAULT_PATH}',
                shell=True,
            )
        else:
            subprocess.check_call(
                f'echo "GRUB_TIMEOUT={self.GRUB_DEFAULT_TIMEOUT}" >> {GRUB_DEFAULT_PATH}',
                shell=True,
            )
        LOG.info("GRUB_TIMEOUT set to %d", self.GRUB_DEFAULT_TIMEOUT)

        # Regenerate grub config
        subprocess.check_call(["update-grub"])
        LOG.info("GRUB configuration regenerated successfully")

    def _reboot(self) -> None:
        """Reboot the system.

        Initiates a system reboot to boot into the new image.
        """
        LOG.info("Initiating system reboot for image update")
        subprocess.check_call("reboot && sleep 600", shell=True)

    def update_on_dp(self) -> None:
        """Update the resource on the data plane."""

        # Read the original image from the file
        if IMAGE_PATH.exists():
            with open(IMAGE_PATH, "r") as f:
                orig_image = f.read().strip()
        else:
            orig_image = None

        # Image changed, need to start the update procedure
        if orig_image and orig_image != self.image:
            LOG.info("Image change detected: '%s' -> '%s'", orig_image, self.image)
            # - save and convert network settings
            self._save_network_settings()
            # - save the target image
            self._save_target_image()
            # - update the grub default item
            self._update_grub_default()
            # - reboot
            self._reboot()
            return

        # The simplest implementation, just recreate.
        self.dump_to_dp()


class GuestMachineCapabilityDriver(meta.MetaFileStorageAgentDriver):
    GUEST_META_PATH = os.path.join(c.WORK_DIR, "guest_meta.json")

    __model_map__ = {GUEST_MACHINE_KIND: GuestMachineMetaModel}

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, meta_file=self.GUEST_META_PATH, **kwargs)
