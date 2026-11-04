# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0

from unittest import mock
from types import SimpleNamespace
import uuid

from openstack.network.v2.network import Network
from openstack.network.v2.port import Port
from openstack.network.v2.security_group import SecurityGroup
from openstack.network.v2.security_group_rule import SecurityGroupRule
from openstack.network.v2.subnet import Subnet
import pytest
import yaml

from gcl_sdk.cloud import bootstrap
from gcl_sdk.cloud.drivers.base import CloudConfig
from gcl_sdk.cloud.drivers.openstack import OpenStackDriver
from gcl_sdk.agents.universal.drivers import cloud as cloud_pool
from gcl_sdk.agents.universal.drivers import pool as pool_models


@pytest.mark.parametrize("cores,ram", [(-1, -1), (-1, 4096), (8, -1), (8, 4096), (1, 512)])
def test_pool_quota_projection_preserves_unlimited_and_finite_capacity(cores, ram):
    driver = cloud_pool.CloudPoolDriver.__new__(cloud_pool.CloudPoolDriver)
    driver.pool = SimpleNamespace(uuid=uuid.uuid4())
    driver.spec = pool_models.CloudPoolDriverSpec(connection=uuid.uuid4())
    driver.cloud = mock.Mock()
    driver.cloud.api.compute.get_limits.return_value.absolute = SimpleNamespace(
        max_total_cores=cores, max_total_ram_size=ram,
        total_cores_used=2, total_ram_used=1024,
    )
    actual = driver.get_pool_info()
    for quota, used, total, available in (
        (cores, 2, actual.all_cores, actual.avail_cores),
        (ram, 1024, actual.all_ram, actual.avail_ram),
    ):
        if quota == -1:
            assert available >= 4096
            assert total - available == used
            assert total <= 2**31 - 1
        else:
            assert total == quota
            assert available == max(0, quota - used)


@pytest.mark.parametrize("quota", [-1, 100, 1])
def test_cinder_quota_supports_volume_placement(quota):
    driver = cloud_pool.CloudPoolDriver.__new__(cloud_pool.CloudPoolDriver)
    driver.spec = pool_models.CloudPoolDriverSpec(connection=uuid.uuid4())
    driver.cloud = mock.Mock()
    driver.cloud.api.block_storage.get_limits.return_value.absolute = SimpleNamespace(
        max_total_volume_gigabytes=quota, total_gigabytes_used=1,
    )
    storage = driver.list_storage_pools()[0]
    if quota == -1:
        assert storage.available >= 4096
        assert storage.available_actual == storage.available
    else:
        assert storage.capacity_usable == quota
        assert storage.available_actual == max(0, quota - 1)
    selected = pool_models.select_storage_pool([storage], storage.tier, False, 8)
    assert (selected is storage) == (quota == -1 or quota - 1 >= 8)


@pytest.fixture
def driver():
    return OpenStackDriver(
        CloudConfig(
            uuid.uuid4(),
            "https://identity.example/v3",
            {
                "application_credential_id": "test",
                "application_credential_secret": "secret",
            },
        ),
        connection=mock.Mock(),
    )


def test_connection_cannot_adopt_or_delete_another_connections_network(driver):
    other = OpenStackDriver(
        CloudConfig(
            uuid.uuid4(),
            "https://identity.example/v3",
            {
                "application_credential_id": "test",
                "application_credential_secret": "secret",
            },
        ),
        connection=driver.api,
    )
    network = Network(
        id=str(uuid.uuid4()), description=other.marker("network", uuid.uuid4())
    )
    driver.api.network.networks.return_value = [network]
    assert driver.networks() == []
    with pytest.raises(ValueError, match="unmanaged"):
        driver.delete_network(network)
    driver.api.network.delete_network.assert_not_called()


def test_network_retries_recover_the_provider_object(driver):
    resource = uuid.uuid4()
    network = Network(
        id=str(uuid.uuid4()), description=driver.marker("network", resource)
    )
    driver.api.network.networks.return_value = [network]
    assert driver.ensure_network(resource, "renamed").id == network.id
    driver.api.network.create_network.assert_not_called()


def test_duplicate_identity_is_an_error_instead_of_arbitrary_adoption(driver):
    resource = uuid.uuid4()
    network = Network(
        id=str(uuid.uuid4()), description=driver.marker("network", resource)
    )
    driver.api.network.networks.return_value = [network, network]
    with pytest.raises(RuntimeError, match="Duplicate"):
        driver.ensure_network(resource, "name")


def test_foreign_ports_are_not_returned_even_in_a_managed_network(driver):
    network = Network(
        id=str(uuid.uuid4()), description=driver.marker("network", uuid.uuid4())
    )
    owned = Port(id=str(uuid.uuid4()), description=driver.marker("port", uuid.uuid4()))
    unrelated = Port(id=str(uuid.uuid4()), description="customer port")
    driver.api.network.ports.return_value = [owned, unrelated]
    assert driver.ports(network) == [owned]


def test_port_retry_never_allocates_a_second_address(driver):
    network = Network(
        id=str(uuid.uuid4()), description=driver.marker("network", uuid.uuid4())
    )
    subnet = Subnet(
        id=str(uuid.uuid4()),
        network_id=network.id,
        description=driver.marker("subnet", uuid.uuid4()),
    )
    resource = uuid.uuid4()
    port = Port(id=str(uuid.uuid4()), description=driver.marker("port", resource))
    driver.api.network.get_network.return_value = network
    driver.api.network.ports.return_value = [port]
    assert driver.ensure_port(resource, subnet) is port
    driver.api.network.create_port.assert_not_called()


def test_provider_default_egress_is_equivalent_to_explicit_any_address(driver):
    resource = uuid.uuid4()
    group = SecurityGroup(
        id=str(uuid.uuid4()), description=driver.marker("security_group", resource)
    )
    rule = SecurityGroupRule(
        id=str(uuid.uuid4()),
        direction="egress",
        ethertype="IPv4",
        remote_ip_prefix=None,
    )
    driver.api.network.security_groups.return_value = [group]
    driver.api.network.security_group_rules.return_value = [rule]
    driver.security_group(
        resource,
        [{"direction": "egress", "ethertype": "IPv4", "remote_ip_prefix": "0.0.0.0/0"}],
    )
    driver.api.network.create_security_group_rule.assert_not_called()
    driver.api.network.delete_security_group_rule.assert_not_called()


def test_bootstrap_uses_explicit_identity_and_keeps_key_out_of_command_lines():
    node = uuid.uuid4()
    data = yaml.safe_load(
        bootstrap.agent_user_data(
            node,
            "private-key",
            "https://core.example/orch/",
            "https://core.example/status/",
            "gcl_sdk==3.7.0",
            hostname="cloud-node",
        )
    )
    files = {f["path"]: f for f in data["write_files"]}
    assert files["/var/lib/exordos/node-id"]["content"].strip() == str(node)
    from gcl_sdk.agents.universal import constants as agent_constants

    assert files[agent_constants.PRIVATE_KEY_PATH]["permissions"] == "0600"
    assert "private-key" not in str(data["runcmd"])
    assert "169.254.169.254" not in str(data)


def test_cloud_pool_is_registered_under_its_driver_kind():
    import importlib.metadata

    entries = importlib.metadata.entry_points(group="gcl_sdk_machine_pool_driver")
    assert (
        next(entry for entry in entries if entry.name == "cloud").load().__name__
        == "CloudPoolDriver"
    )


def test_volume_detach_addresses_the_server_before_the_volume():
    from types import SimpleNamespace

    from gcl_sdk.agents.universal.drivers.cloud import CloudPoolDriver

    adapter = CloudPoolDriver.__new__(CloudPoolDriver)
    adapter.pool = SimpleNamespace(uuid=uuid.uuid4())
    adapter.cloud = mock.Mock()
    provider_volume = SimpleNamespace(
        id=str(uuid.uuid4()), attachments=[{"server_id": str(uuid.uuid4())}]
    )
    adapter.cloud.volume.return_value = provider_volume
    adapter.cloud.api.compute.get_server.return_value = SimpleNamespace(
        id=provider_volume.attachments[0]["server_id"]
    )
    adapter.detach_volume(SimpleNamespace(uuid=uuid.uuid4()))
    adapter.cloud.api.compute.delete_volume_attachment.assert_called_once_with(
        server=provider_volume.attachments[0]["server_id"], volume=provider_volume.id
    )


def test_cloud_bootstrap_retries_installation_through_systemd():
    data = yaml.safe_load(
        bootstrap.agent_user_data(
            uuid.uuid4(),
            "private-key",
            "https://core.example/orch",
            "https://core.example/status",
            "https://packages.example/sdk.whl",
        )
    )
    files = {item["path"]: item for item in data["write_files"]}
    unit = files["/etc/systemd/system/exordos-agent.service"]["content"]
    assert "ExecStartPre=/opt/exordos/install-agent" in unit
    assert "Restart=always" in unit
    installer = files["/opt/exordos/install-agent"]
    assert installer["permissions"] == "0700"
    assert (
        "if [ ! -x /opt/exordos/venv/bin/exordos-universal-agent ]"
        in installer["content"]
    )
    assert "private-key" not in installer["content"]
    assert "https://packages.example/sdk.whl" in installer["content"]


def test_cloud_extensions_reject_incompatible_provider_apis(monkeypatch):
    from gcl_sdk.cloud.drivers import base

    extension = mock.Mock(name="incompatible")
    extension.name = "incompatible"
    extension.load.return_value = object
    monkeypatch.setattr(
        base.importlib.metadata, "entry_points", lambda **kwargs: [extension]
    )
    with pytest.raises(TypeError, match="OpenStackDriver subclass"):
        base.load_driver(
            CloudConfig(
                uuid.uuid4(),
                "https://identity.example/v3",
                {
                    "application_credential_id": "test",
                    "application_credential_secret": "secret",
                },
                driver="incompatible",
            )
        )


def test_cloud_installer_recovers_from_a_failed_download_and_then_skips(tmp_path):
    import os
    import subprocess

    data = yaml.safe_load(
        bootstrap.agent_user_data(
            uuid.uuid4(),
            "key",
            "https://core.example/orch",
            "https://core.example/status",
            "sdk.whl",
        )
    )
    installer = next(
        f["content"]
        for f in data["write_files"]
        if f["path"] == "/opt/exordos/install-agent"
    )
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    commands = tmp_path / "commands"
    commands.mkdir()
    (commands / "python3").write_text("#!/bin/sh\nexit 0\n")
    (commands / "python3").chmod(0o700)
    pip = venv / "bin/pip"
    pip.write_text("""#!/bin/sh
set -eu
echo attempt >> "$EXO_TEST_ROOT/attempts"
if [ ! -f "$EXO_TEST_ROOT/download-failed" ]; then
    touch "$EXO_TEST_ROOT/download-failed"
    exit 17
fi
touch "$EXO_TEST_ROOT/venv/bin/exordos-universal-agent"
chmod 700 "$EXO_TEST_ROOT/venv/bin/exordos-universal-agent"
""")
    pip.chmod(0o700)
    script = tmp_path / "installer"
    script.write_text(installer.replace("/opt/exordos/venv", str(venv)))
    environment = dict(
        os.environ,
        PATH=str(commands) + os.pathsep + os.environ["PATH"],
        EXO_TEST_ROOT=str(tmp_path),
    )
    assert subprocess.run(["/bin/sh", str(script)], env=environment).returncode == 17
    assert subprocess.run(["/bin/sh", str(script)], env=environment).returncode == 0
    assert subprocess.run(["/bin/sh", str(script)], env=environment).returncode == 0
    assert (tmp_path / "attempts").read_text().splitlines() == ["attempt", "attempt"]


@pytest.mark.parametrize("package", ["", "  \t"])
def test_bootstrap_rejects_an_empty_package(package):
    with pytest.raises(ValueError, match="nonempty"):
        bootstrap.agent_user_data(
            uuid.uuid4(),
            "key",
            "https://core.example/orch",
            "https://core.example/status",
            package,
        )


def test_agent_endpoint_rejects_password_with_an_empty_username():
    with pytest.raises(ValueError, match="without credentials"):
        bootstrap.agent_user_data(
            uuid.uuid4(),
            "key",
            "https://:password@core.example/orch",
            "https://core.example/status",
            "sdk.whl",
        )


def test_failed_resource_creation_does_not_log_cloud_bootstrap_secrets(caplog):
    from gcl_sdk.agents.universal.dm import models
    from gcl_sdk.agents.universal.services import agent

    secret = "test-private-node-key-must-not-appear-in-error-logs"
    resource = models.Resource.from_value(
        {"uuid": str(uuid.uuid4()), "user_data": secret},
        "pool_machine",
        frozenset({"uuid", "user_data"}),
    )
    service = agent.UniversalAgentService.__new__(agent.UniversalAgentService)
    service._create_resource = mock.Mock(side_effect=ValueError("provider unavailable"))
    driver = mock.Mock()
    driver.list.return_value = []
    assert service._actualize_capability(driver, "pool_machine", [resource]) == []
    assert secret not in caplog.text
    assert str(resource.uuid) in caplog.text


def test_nova_bootstrap_is_gzipped_and_oversized_data_never_reaches_provider(driver):
    import base64
    import gzip
    import secrets
    from types import SimpleNamespace

    driver.server = mock.Mock(return_value=None)
    driver.flavor = mock.Mock(return_value=SimpleNamespace(id="flavor"))
    arguments = dict(
        name="gateway",
        pool=uuid.uuid4(),
        project=uuid.uuid4(),
        cores=1,
        ram=1024,
        image="image",
        ports=[],
    )
    content = "#cloud-config\n" + "compressible bootstrap\n" * 10000
    driver.ensure_server(uuid.uuid4(), user_data=content, **arguments)
    encoded = driver.api.compute.create_server.call_args.kwargs["user_data"]
    assert len(encoded) < 65535
    assert gzip.decompress(base64.b64decode(encoded)).decode() == content
    driver.api.compute.create_server.reset_mock()
    oversized = secrets.token_hex(100000)
    size = len(base64.b64encode(gzip.compress(oversized.encode(), mtime=0)))
    with pytest.raises(ValueError, match=f"{size} base64 characters, maximum 65535"):
        driver.ensure_server(uuid.uuid4(), user_data=oversized, **arguments)
    driver.api.compute.create_server.assert_not_called()


def test_nova_accepts_bootstrap_for_more_than_sixteen_endpoints(driver):
    import base64
    import gzip
    import random
    import string
    from types import SimpleNamespace

    from gcl_sdk.cloud import openvpn

    ca = openvpn.certificate("maximum-mesh")
    ids = sorted(uuid.uuid4() for _ in range(17))
    nodes = {}
    for index, own in enumerate(ids):
        nodes[own] = {
            "cidr": f"10.250.{index}.0/24",
            "endpoint": f"192.0.2.{index + 1}",
            "tunnel_cidr": f"100.127.{index}.0/24",
            "server": openvpn.certificate(own.hex + "-server", ca, server=True),
            "client": openvpn.certificate(own.hex + "-client", ca),
            "tls_server_key": openvpn.tls_key(),
            "tls_clients": {},
        }
    for own in ids:
        for server in ids:
            if server < own:
                nodes[own]["tls_clients"][server.hex] = openvpn.tls_key(
                    nodes[server]["tls_server_key"]
                )
    rng = random.Random(1729)
    tails = [
        "".join(rng.choices(string.ascii_letters + string.digits, k=1900))
        for _ in range(3)
    ]
    payloads = []
    for own in ids:
        peers = [
            dict(node, id=str(remote))
            for remote, node in nodes.items()
            if remote != own
        ]
        bundle = openvpn.render(own, nodes[own], peers, ca["cert"])
        agent = bootstrap.agent_user_data(
            own,
            "agent-key",
            "https://core.example/" + tails[0],
            "https://core.example/" + tails[1],
            "https://repo.example/" + tails[2] + "/gcl_sdk.whl",
            caps_drivers="OpenVPNCapabilityDriver",
        )
        payload = openvpn.bootstrap_user_data(own, bundle, agent)
        payloads.append(payload)
    largest = max(
        payloads, key=lambda value: len(gzip.compress(value.encode(), mtime=0))
    )
    driver.server = mock.Mock(return_value=None)
    driver.flavor = mock.Mock(return_value=SimpleNamespace(id="flavor"))
    driver.ensure_server(
        uuid.uuid4(),
        name="gateway",
        pool=uuid.uuid4(),
        project=uuid.uuid4(),
        cores=1,
        ram=1024,
        image="image",
        ports=[],
        user_data=largest,
    )
    encoded = driver.api.compute.create_server.call_args.kwargs["user_data"]
    assert gzip.decompress(base64.b64decode(encoded)).decode() == largest


def test_owned_provider_session_is_closed_when_driver_is_released():
    import gc
    import weakref

    connection = mock.Mock()
    with mock.patch("openstack.connect", return_value=connection):
        driver = OpenStackDriver(
            CloudConfig(
                uuid.uuid4(),
                "https://identity.example/v3",
                {
                    "application_credential_id": "test",
                    "application_credential_secret": "secret",
                },
            )
        )
    reference = weakref.ref(driver)
    del driver
    gc.collect()
    assert reference() is None
    connection.close.assert_called_once_with()


def test_injected_provider_session_remains_owned_by_the_caller():
    import gc

    connection = mock.Mock()
    driver = OpenStackDriver(
        CloudConfig(
            uuid.uuid4(),
            "https://identity.example/v3",
            {
                "application_credential_id": "test",
                "application_credential_secret": "secret",
            },
        ),
        connection=connection,
    )
    del driver
    gc.collect()
    connection.close.assert_not_called()


def test_agent_start_is_queued_before_cloud_final_for_preinstalled_images():
    data = yaml.safe_load(
        bootstrap.agent_user_data(
            uuid.uuid4(),
            "private",
            "http://core/orch",
            "http://core/status",
            "gcl_sdk==3.7.2",
        )
    )
    command = data["bootcmd"][0]
    assert command[:3] == [
        "systemd-run",
        "--unit=exordos-cloud-bootstrap",
        "--no-block",
    ]
    assert "--property=After=cloud-config.service" in command
    # A plain cloud image needs cloud-final to install venv/build dependencies;
    # starting its installer early would hold multi-user.target in restart.
    assert (
        "--property=ExecCondition=/usr/bin/test -x /usr/bin/exordos-universal-agent"
        in command
    )
    assert "systemctl enable --now --no-block exordos-agent.service" in command[-1]
    assert "cloud-final" not in command[-1]


def test_provider_uses_api_credentials_without_yaml_or_environment(monkeypatch):
    monkeypatch.setenv("OS_CLOUD", "must-not-be-loaded")
    monkeypatch.setenv("OS_CLIENT_CONFIG_FILE", "/nonexistent/clouds.yaml")
    monkeypatch.setenv("OS_AUTH_URL", "https://wrong.example/v3")
    config = CloudConfig(
        uuid.uuid4(),
        "https://identity.example/v3",
        {
            "application_credential_id": "application",
            "application_credential_secret": "private-sentinel",
        },
    )
    with mock.patch(
        "openstack.service_description.ServiceDescription._make_proxy",
        return_value=mock.Mock(),
    ):
        driver = OpenStackDriver(config)
    assert driver.api.config.get_auth_args()["auth_url"] == config.auth_url
    assert (
        driver.api.config.get_auth_args()["application_credential_secret"]
        == "private-sentinel"
    )
    assert "private-sentinel" not in repr(config)


@pytest.mark.parametrize(
    "auth_type,auth",
    [
        ("v3applicationcredential", {}),
        (
            "v3applicationcredential",
            {"application_credential_id": "id", "application_credential_secret": ""},
        ),
        (
            "v3applicationcredential",
            {
                "application_credential_id": "id",
                "application_credential_secret": "secret",
                "endpoint_override": "http://other",
            },
        ),
        ("v3password", {"username": "user", "password": "secret"}),
    ],
)
def test_provider_rejects_incomplete_or_unknown_auth_fields(auth_type, auth):
    with pytest.raises(ValueError):
        CloudConfig(
            uuid.uuid4(), "https://identity.example/v3", auth, auth_type=auth_type
        )


def test_provider_accepts_project_scoped_password_authentication():
    config = CloudConfig(
        uuid.uuid4(),
        "https://identity.example/v3",
        {
            "username": "user",
            "password": "private-sentinel",
            "user_domain_name": "Default",
            "project_id": "project",
        },
        auth_type="v3password",
    )
    with mock.patch(
        "openstack.service_description.ServiceDescription._make_proxy",
        return_value=mock.Mock(),
    ):
        driver = OpenStackDriver(config)
    assert driver.api.config.get_auth_args()["project_id"] == "project"
    assert "private-sentinel" not in repr(config)
