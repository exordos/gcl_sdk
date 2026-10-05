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
        unit = (module.OST_UNIT_DIR / model.unit_name).read_text()
        assert f"--bind=0.0.0.0:7777 {model.location}" in unit
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
    assert data.read_text() == "keep"


def test_native_ost_launch_from_reconciled_unit(node, monkeypatch, tmp_path):
    import os
    import shlex
    import socket
    import time

    binary = os.environ.get("RAWSTOR_TEST_OST")
    if not binary:
        pytest.skip("Set RAWSTOR_TEST_OST to run the native OST lifecycle")
    model, module = node
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    model.bind_address = f"127.0.0.1:{port}"
    model.endpoint = f"ost://127.0.0.1:{port}"
    processes = []
    log = (tmp_path / "ost.log").open("w")

    def system_command(args, **kwargs):
        if args[0] == "install":
            from pathlib import Path

            Path(args[-1]).mkdir(exist_ok=True)
        elif args[1] == "enable" and not processes:
            unit = (module.OST_UNIT_DIR / model.unit_name).read_text()
            command = next(
                line.removeprefix("ExecStart=")
                for line in unit.splitlines()
                if line.startswith("ExecStart=")
            )
            # Keep the test daemon small; use the unit's real bind and backing URI.
            argv = shlex.split(command)
            argv[0] = binary
            argv[1:1] = ["--workers", "1", "--queue-size", "256"]
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
