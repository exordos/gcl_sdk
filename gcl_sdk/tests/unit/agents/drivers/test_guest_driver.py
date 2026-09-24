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

import json
import stat
from unittest import mock
import uuid as sys_uuid

from gcl_sdk.agents.universal.cmd import universal_agent
from gcl_sdk.agents.universal.drivers import guest


def test_image_update_persists_node_key_for_autonomous_seed(
    tmp_path,
    monkeypatch,
):
    image_path = tmp_path / "universal-agent" / "image"
    agent_private_key_path = tmp_path / "universal-agent" / "private_key"
    persistent_dir = tmp_path / "persist"
    system_netplan_dir = tmp_path / "netplan"

    image_path.parent.mkdir()
    image_path.write_text("https://images.example/old.raw", encoding="utf-8")
    agent_private_key_path.write_text("existing-node-key", encoding="utf-8")
    system_netplan_dir.mkdir()

    monkeypatch.setattr(guest, "IMAGE_PATH", image_path)
    monkeypatch.setattr(
        guest.GuestMachineMetaModel,
        "AGENT_PRIVATE_KEY_PATH",
        agent_private_key_path,
    )
    monkeypatch.setattr(guest, "EXORDOS_DATA_DIR", persistent_dir)
    monkeypatch.setattr(guest, "NETPLAN_DIR", persistent_dir / "netplan")
    monkeypatch.setattr(guest, "UPDATE_JSON_PATH", persistent_dir / "update.json")
    monkeypatch.setattr(
        guest,
        "UPDATE_PRIVATE_KEY_PATH",
        persistent_dir / "private_key",
    )
    monkeypatch.setattr(guest, "SYSTEM_NETPLAN_DIR", system_netplan_dir)

    machine = guest.GuestMachineMetaModel(
        uuid=sys_uuid.uuid4(),
        image="https://images.example/new.raw",
        boot="hd0",
    )

    with (
        mock.patch.object(machine, "_update_grub_default") as update_grub,
        mock.patch.object(machine, "_reboot") as reboot,
    ):
        machine.update_on_dp()

    assert json.loads((persistent_dir / "update.json").read_text()) == {
        "target_image": "https://images.example/new.raw",
        "original_image": "https://images.example/old.raw",
    }
    persisted_key = persistent_dir / "private_key"
    assert persisted_key.read_text(encoding="utf-8") == "existing-node-key"
    assert stat.S_IMODE(persisted_key.stat().st_mode) == 0o600
    update_grub.assert_called_once_with()
    reboot.assert_called_once_with()


def test_missing_private_key_removes_stale_update_handoff(
    tmp_path,
    monkeypatch,
):
    persistent_dir = tmp_path / "persist"
    persistent_dir.mkdir()
    persisted_key = persistent_dir / "private_key"
    persisted_key.write_text("stale-node-key", encoding="utf-8")
    monkeypatch.setattr(
        guest.GuestMachineMetaModel,
        "AGENT_PRIVATE_KEY_PATH",
        tmp_path / "missing-private-key",
    )
    monkeypatch.setattr(guest, "EXORDOS_DATA_DIR", persistent_dir)
    monkeypatch.setattr(
        guest,
        "UPDATE_PRIVATE_KEY_PATH",
        persisted_key,
    )

    machine = guest.GuestMachineMetaModel(
        uuid=sys_uuid.uuid4(),
        image="https://images.example/new.raw",
        boot="hd0",
    )

    machine._save_private_key()

    assert not persisted_key.exists()


def test_guest_driver_binds_configured_private_key_path(tmp_path, monkeypatch):
    configured_private_key_path = tmp_path / "custom" / "node-key"
    monkeypatch.setattr(
        guest.GuestMachineCapabilityDriver,
        "GUEST_META_PATH",
        str(tmp_path / "guest-meta.json"),
    )

    driver = guest.GuestMachineCapabilityDriver(
        private_key_path=str(configured_private_key_path)
    )

    model = driver.__model_map__[guest.GUEST_MACHINE_KIND]
    assert model.AGENT_PRIVATE_KEY_PATH == configured_private_key_path


def test_driver_loader_passes_configured_private_key_path(tmp_path, monkeypatch):
    configured_private_key_path = tmp_path / "custom" / "node-key"
    config = mock.MagicMock()
    config.config_file = ()
    config.__getitem__.return_value.private_key_path = str(configured_private_key_path)
    monkeypatch.setattr(universal_agent, "CONF", config)
    monkeypatch.setattr(
        guest.GuestMachineCapabilityDriver,
        "GUEST_META_PATH",
        str(tmp_path / "guest-meta.json"),
    )

    driver = universal_agent.load_driver(guest.GuestMachineCapabilityDriver)

    model = driver.__model_map__[guest.GUEST_MACHINE_KIND]
    assert model.AGENT_PRIVATE_KEY_PATH == configured_private_key_path
