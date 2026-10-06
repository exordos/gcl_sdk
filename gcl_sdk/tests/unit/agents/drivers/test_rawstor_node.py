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

import subprocess
from unittest.mock import patch
from uuid import uuid4

import pytest

from gcl_sdk.agents.universal.drivers import storage_capacity


def test_ost_configuration_accepts_ipv6_and_dedicated_directory():
    assert (
        storage_capacity.validate_ost_configuration("file:///data/ost1", "[::]:7777")
        == "/data/ost1"
    )


@pytest.mark.parametrize(
    "location,bind",
    [
        ("file:///", "0.0.0.0:7777"),
        ("file:///data/../ost", "0.0.0.0:7777"),
        ("file:///data/%i", "0.0.0.0:7777"),
        ("file:///data/ost", "0.0.0.0:7777\nRestart=no"),
        ("file:///data/ost", "0.0.0.0:65536"),
        ("file://host/data/ost", "0.0.0.0:7777"),
    ],
)
def test_ost_configuration_rejects_invalid_service_arguments(location, bind):
    with pytest.raises(ValueError):
        storage_capacity.validate_ost_configuration(location, bind)


@pytest.fixture
def node(tmp_path, monkeypatch):
    pytest.importorskip("rawstor")
    from gcl_sdk.agents.universal.drivers import rawstor_node

    monkeypatch.setattr(rawstor_node, "OST_UNIT_DIR", tmp_path / "units")
    monkeypatch.setattr(rawstor_node, "OST_CONFIG_DIR", tmp_path / "config")
    model = rawstor_node.MetaStorageNode(
        uuid=uuid4(),
        cluster=uuid4(),
        endpoint="ost://host:7777",
        location=f"file://{tmp_path}/data",
        bind_address="0.0.0.0:7777",
    )
    return model, rawstor_node


def test_reconcile_starts_ost_and_checks_advertised_endpoint(node):
    model, module = node
    with (
        patch.object(module.subprocess, "run") as run,
        patch("rawstor.Location") as location,
    ):
        model.dump_to_dp()
        assert model.status == "ACTIVE"
        location.assert_called_once_with(model.endpoint)
        location.return_value.info.assert_called_once()
        config = (module.OST_CONFIG_DIR / f"{model.uuid}.conf").read_text()
        assert f"BIND_ADDR=0.0.0.0:7777\nLOCATION={model.location}\n" == config
        assert not (module.OST_UNIT_DIR / model.unit_name).exists()
        commands = [c.args[0] for c in run.call_args_list]
        assert ["systemctl", "enable", "--now", model.unit_name] in commands
        run.reset_mock()
        model.restore_from_dp()
        assert ["systemctl", "restart", model.unit_name] not in [
            c.args[0] for c in run.call_args_list
        ]


def test_unreachable_ost_is_not_reported_ready_and_can_retry(node):
    model, module = node
    with patch.object(module.subprocess, "run"), patch("rawstor.Location") as location:
        location.return_value.info.side_effect = OSError("not ready")
        with pytest.raises(OSError):
            model.dump_to_dp()
        assert model.status == "NEW"
        location.return_value.info.side_effect = None
        model.restore_from_dp()
        assert model.status == "ACTIVE"


def test_failed_service_start_is_not_reported_ready(node):
    model, module = node
    with patch.object(
        module.subprocess,
        "run",
        side_effect=subprocess.CalledProcessError(1, "systemctl"),
    ):
        with pytest.raises(subprocess.CalledProcessError):
            model.dump_to_dp()
    assert model.status == "NEW"


def test_update_restarts_changed_bind_and_delete_preserves_backing_data(node):
    model, module = node
    from pathlib import Path

    backing = Path(model.location.removeprefix("file://"))
    backing.mkdir()
    data = backing / "data"
    data.write_text("keep")
    with patch.object(module.subprocess, "run") as run, patch("rawstor.Location"):
        model.dump_to_dp()
        run.reset_mock()
        model.bind_address = "0.0.0.0:7778"
        model.endpoint = "ost://host:7778"
        model.update_on_dp()
        assert ["systemctl", "restart", model.unit_name] in [
            c.args[0] for c in run.call_args_list
        ]
        model.delete_from_dp()
    assert not (module.OST_UNIT_DIR / model.unit_name).exists()
    assert not (module.OST_CONFIG_DIR / f"{model.uuid}.conf").exists()
    assert data.read_text() == "keep"


@pytest.mark.parametrize("backing", ["file", "zfs"])
def test_native_ost_launch_from_reconciled_config(node, monkeypatch, tmp_path, backing):
    import os
    import socket
    import time

    binary = os.environ.get("RAWSTOR_TEST_OST")
    if not binary:
        pytest.skip("Set RAWSTOR_TEST_OST to run the native OST lifecycle")
    model, module = node
    if backing == "zfs":
        location = os.environ.get("RAWSTOR_TEST_ZFS_LOCATION")
        if not location:
            pytest.skip("Set RAWSTOR_TEST_ZFS_LOCATION to an isolated test dataset")
        model.location = location
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    model.bind_address = f"127.0.0.1:{port}"
    model.endpoint = f"ost://127.0.0.1:{port}"
    processes = []
    log = (tmp_path / "ost.log").open("w")
    real_run = subprocess.run

    def system_command(args, **kwargs):
        if args[0] == "zfs":
            return real_run(args, **kwargs)
        elif args[0] == "install":
            from pathlib import Path

            Path(args[-1]).mkdir(exist_ok=True)
        elif args[1] == "enable" and not processes:
            config = dict(
                line.split("=", 1)
                for line in (module.OST_CONFIG_DIR / f"{model.uuid}.conf")
                .read_text()
                .splitlines()
            )
            # Use the same bind and backing URI consumed by the packaged template.
            argv = [
                binary,
                "--bind",
                config["BIND_ADDR"],
                "--workers",
                "1",
                "--queue-size",
                "256",
                config["LOCATION"],
            ]
            processes.append(
                subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
            )
        elif args[1] == "disable":
            for process in processes:
                process.terminate()
                process.wait(timeout=5)

    monkeypatch.setattr(module.subprocess, "run", system_command)
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                model.dump_to_dp()
                break
            except OSError:
                assert not processes or processes[0].poll() is None
                assert time.monotonic() < deadline
                time.sleep(0.02)
        assert model.status == "ACTIVE"
        import rawstor

        assert rawstor.Location(model.endpoint).info().total > 0
        if backing == "zfs":
            target = rawstor.Location(model.endpoint).create(size=8 << 20, width=1)
            assert target.spec().size == 8 << 20
            snapshot = target.create_version()
            assert snapshot.spec().size == 8 << 20
            snapshot.remove()
            target.remove()
        model.delete_from_dp()
        assert processes[0].poll() is not None
        assert not (module.OST_UNIT_DIR / model.unit_name).exists()
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        log.close()


def test_agent_delete_does_not_contact_or_restart_an_unreachable_ost(node, tmp_path):
    model, module = node
    driver = module.StorageNodeAgentDriver(meta_file=str(tmp_path / "meta.json"))
    driver.start()
    resource = model.to_ua_resource("storage_node")
    with (
        patch.object(module.subprocess, "run") as run,
        patch("rawstor.Location") as location,
    ):
        location.return_value.info.side_effect = OSError("unreachable")
        driver.delete(resource)
    location.assert_not_called()
    commands = [call.args[0] for call in run.call_args_list]
    assert not any("enable" in command or "restart" in command for command in commands)


@pytest.mark.parametrize(
    "location,expected",
    [
        ("zfs://tank", "tank"),
        ("zfs://tank/ost1", "tank/ost1"),
        ("zfs://pool_1/rawstor/ost-1", "pool_1/rawstor/ost-1"),
    ],
)
def test_ost_configuration_accepts_zfs(location, expected):
    assert (
        storage_capacity.validate_ost_configuration(location, "0.0.0.0:7777")
        == expected
    )


@pytest.mark.parametrize(
    "location",
    [
        "ZFS://tank/ost",
        "zfs:///tank",
        "zfs://tank/../ost",
        "zfs://tank/ost/",
        "zfs://tank//ost",
        "zfs://user@tank/ost",
        "zfs://tank/ost@snapshot",
        "zfs://tank/ost%20one",
    ],
)
def test_ost_configuration_rejects_invalid_zfs(location):
    with pytest.raises(ValueError):
        storage_capacity.validate_ost_configuration(location, "0.0.0.0:7777")


def test_zfs_reconcile_uses_existing_dataset_and_privileged_template_dropin(node):
    model, module = node
    model.location = "zfs://tank/ost1"
    with patch.object(module.subprocess, "run") as run, patch("rawstor.Location"):
        model.dump_to_dp()
        commands = [call.args[0] for call in run.call_args_list]
        assert ["zfs", "list", "-H", "-o", "name", "tank/ost1"] in commands
        assert not any(
            command[0] == "install" or "create" in command for command in commands
        )
        dropin = (
            module.OST_UNIT_DIR / f"{model.unit_name}.d" / "exordos.conf"
        ).read_text()
        assert "User=root" in dropin and "Group=root" in dropin
        assert not (module.OST_UNIT_DIR / model.unit_name).exists()
        run.reset_mock()
        model.delete_from_dp()
        assert not any(
            command.args[0][0] in ("zfs", "zpool") for command in run.call_args_list
        )
