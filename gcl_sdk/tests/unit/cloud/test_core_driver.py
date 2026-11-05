# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0

import dataclasses
import json
from unittest import mock
import uuid

from bazooka import exceptions
import pytest

from gcl_sdk.agents.universal.drivers import pool
from gcl_sdk.agents.universal.drivers.core_pool import CorePoolDriver
from gcl_sdk.agents.universal.drivers.core_pool import bootstrap_script
from gcl_sdk.cloud.bootstrap import agent_user_data
from gcl_sdk.cloud.drivers.core import CoreConfig
from gcl_sdk.cloud.drivers.core import CoreDriver


@pytest.fixture
def driver():
    config = CoreConfig(
        uuid.uuid4(),
        "https://core.example",
        uuid.uuid4(),
        (uuid.uuid4(),),
        {"username": "operator", "password": "secret"},
    )
    return CoreDriver(config, client=mock.Mock())


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://u:p@core/",
        "https://core/?token=secret",
        "https://core/#fragment",
    ],
)
def test_configuration_rejects_credential_and_non_http_urls(driver, url):
    with pytest.raises(ValueError):
        dataclasses.replace(driver.config, api_url=url)


def test_configuration_does_not_expose_auth_in_repr(driver):
    assert "secret" not in repr(driver.config)


def test_get_does_not_adopt_foreign_resource(driver):
    identity = uuid.uuid4()
    driver.client.get.return_value = {
        "uuid": str(driver.resource_id("node", identity)),
        "project_id": str(uuid.uuid4()),
        "description": driver.marker("node", identity),
    }
    with pytest.raises(ValueError, match="ownership"):
        driver.delete("node", identity)
    driver.client.delete.assert_not_called()


def test_matching_marker_does_not_adopt_wrong_uuid(driver):
    identity = uuid.uuid4()
    value = {
        "uuid": str(uuid.uuid4()),
        "project_id": driver.project,
        "description": driver.marker("node", identity),
    }
    assert driver.identity("node", value) is None


def test_ensure_retries_conflicting_create_without_duplicates(driver):
    identity = uuid.uuid4()
    value = {
        "uuid": str(driver.resource_id("node", identity)),
        "project_id": driver.project,
        "description": driver.marker("node", identity),
    }
    driver.client.get.side_effect = [exceptions.NotFoundError(mock.Mock()), value]
    driver.client.create.side_effect = exceptions.ConflictError(mock.Mock())
    assert driver.ensure("node", identity, name="workload") == value
    driver.client.create.assert_called_once()


def test_list_collects_all_pages_and_filters_unmanaged_rows(driver):
    values = []
    for _ in range(101):
        identity = uuid.uuid4()
        values.append(
            {
                "uuid": str(driver.resource_id("node", identity)),
                "project_id": driver.project,
                "description": driver.marker("node", identity),
            }
        )
    driver.client.filter.side_effect = [values[:100], values[100:]]
    assert driver.list("node") == values
    assert driver.client.filter.call_args.kwargs["page_marker"] == values[99]["uuid"]
    assert driver.client.filter.call_args.kwargs["project_id"] == driver.project


def test_api_error_is_not_interpreted_as_empty_inventory(driver):
    driver.client.filter.side_effect = exceptions.UnauthorizedError(mock.Mock())
    with pytest.raises(exceptions.UnauthorizedError):
        driver.list("node")


def test_update_does_not_write_when_already_converged(driver):
    identity = uuid.uuid4()
    driver.client.get.return_value = {
        "uuid": str(driver.resource_id("node", identity)),
        "project_id": driver.project,
        "description": driver.marker("node", identity),
        "name": "workload",
    }
    driver.update("node", identity, name="workload")
    driver.client.update.assert_not_called()


def test_bootstrap_preserves_provider_identity_and_agent(driver):
    machine = pool.Machine(
        project_id=uuid.uuid4(),
        uuid=uuid.uuid4(),
        name="workload",
        cores=1,
        ram=1024,
        image="https://images.example/os.raw.zst",
    )
    data = agent_user_data(
        uuid.uuid4(),
        "private-key",
        "https://orch.example",
        "https://status.example",
        "gcl_sdk==3.7.3",
    )
    script = bootstrap_script(driver.config.connection, machine, [], data)
    compile(script, "bootstrap", "exec")
    assert "systemctl disable" not in script
    assert "BindReadOnlyPaths=" in script
    assert "BindPaths=" in script
    assert "exordos-universal-agent.service" not in script
    config = {"body": {"content": script}}
    metadata = driver.metadata(config)
    assert "user_data" not in metadata["machine"]
    assert (
        json.loads(script.splitlines()[0].removeprefix("# exordos-provider "))
        == metadata
    )


def test_disk_is_pending_until_remote_node_materializes_it(driver):
    remote = mock.Mock()
    remote.list.return_value = []
    adapter = CorePoolDriver(mock.Mock(), remote)
    volume = pool.MachineVolume(
        project_id=uuid.uuid4(),
        uuid=uuid.uuid4(),
        name="root",
        machine=uuid.uuid4(),
        size=10,
    )
    pending = adapter.create_volume(volume)
    assert pending.status == "IN_PROGRESS"
    remote.ensure.assert_not_called()


def test_unattached_disk_is_rejected_before_mutation(driver):
    remote = mock.Mock()
    adapter = CorePoolDriver(mock.Mock(), remote)
    with pytest.raises(ValueError, match="belong"):
        adapter.create_volume(
            pool.MachineVolume(
                project_id=uuid.uuid4(), uuid=uuid.uuid4(), name="disk", size=10
            )
        )
    remote.ensure.assert_not_called()


def test_reserved_port_and_empty_data_disk_can_create_node(driver):
    remote = mock.Mock()
    remote.config.connection = driver.config.connection
    remote.project = driver.project
    remote.client.get.return_value = {
        "uuid": str(uuid.uuid4()),
        "project_id": driver.project,
        "network": str(uuid.uuid4()),
    }
    remote.get.return_value = {
        "uuid": str(uuid.uuid4()),
        "subnet": str(uuid.uuid4()),
        "status": "NEW",
        "ipv4": "10.10.0.2",
        "mac": "52:54:00:00:00:01",
    }
    owner = uuid.uuid4()
    machine = pool.Machine(
        uuid=uuid.uuid4(),
        project_id=owner,
        name="workload",
        cores=1,
        ram=1024,
        user_data=agent_user_data(
            uuid.uuid4(),
            "private-key",
            "https://orch.example",
            "https://status.example",
            "gcl_sdk==3.7.3",
        ),
    )
    disks = [
        pool.MachineVolume(
            uuid=uuid.uuid4(),
            project_id=owner,
            name="disk",
            machine=machine.uuid,
            size=8,
            index=index,
            image="https://images.example/os.raw.zst" if index == 0 else None,
        )
        for index in (0, 1)
    ]
    port = pool.Port(uuid=uuid.uuid4(), project_id=owner, mac="52:54:00:00:00:01")
    adapter = CorePoolDriver(mock.Mock(uuid=uuid.uuid4()), remote)
    adapter._machine = mock.Mock(return_value=mock.sentinel.created)
    assert adapter.create_machine(machine, disks, [port]) is mock.sentinel.created
    fields = remote.ensure.call_args.kwargs
    assert "image" not in fields["disk_spec"]["disks"][1]
    assert "private-key" in fields["bootstrap"]["content"]
    assert fields["ports"] == [remote.get.return_value["uuid"]]


def test_storage_inventory_describes_classes_not_physical_pool_pinning(driver):
    remote = mock.Mock()
    remote.resource_id.side_effect = driver.resource_id
    remote.hypervisors.return_value = [
        {
            "storage_pools": [
                {
                    "uuid": str(uuid.uuid4()),
                    "tier": "WARM",
                    "ephemeral": False,
                    "capacity_usable": size,
                    "capacity_provisioned": 2,
                    "available_actual": size - 2,
                }
                for size in (10, 20)
            ]
        }
    ]
    values = CorePoolDriver(mock.Mock(), remote).list_storage_pools()
    assert len(values) == 1
    assert values[0].name == "core:WARM:durable"
    assert values[0].capacity_usable == 30
    assert values[0].available_actual == 18
    assert values[0].capacity_provisioned == 4


def test_vm_resize_updates_cpu_and_memory_together(driver):
    remote = mock.Mock()
    machine = mock.Mock(uuid=uuid.uuid4())
    CorePoolDriver(mock.Mock(), remote).resize_machine(machine, 2, 2048)
    remote.update.assert_called_once_with("node", machine.uuid, cores=2, ram=2048)


def test_provider_http_logging_redacts_password_bootstrap_and_tokens(driver, caplog):
    import logging

    import requests

    provider = CoreDriver(driver.config)
    http = provider.client._http_client
    assert provider.client._auth._http_client is http
    session = http.SESSION(auth=None, verify_ssl=True)
    response = requests.Response()
    response.status_code = 200
    response.url = (
        "https://core.example/v1/iam/clients/default/actions/get_token/invoke"
    )
    response._content = b'{"access_token":"private-token-sentinel"}'
    response.headers["Content-Type"] = "application/json"
    with caplog.at_level(logging.INFO, logger="bazooka.client"):
        session.prepare_request(
            requests.Request(
                "POST",
                response.url,
                json={
                    "password": "private-password-sentinel",
                    "bootstrap": "private-bootstrap-sentinel",
                },
            )
        )
        session._log_response(response)
    assert "private-password-sentinel" not in caplog.text
    assert "private-bootstrap-sentinel" not in caplog.text
    assert "private-token-sentinel" not in caplog.text
    assert "SENSITIVE_DATA" in caplog.text
