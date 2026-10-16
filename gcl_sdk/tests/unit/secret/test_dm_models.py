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

import uuid as sys_uuid

import pytest

from gcl_sdk.agents.universal.drivers import ssh_key as ssh_key_driver
from gcl_sdk.infra.dm import models as infra_models
from gcl_sdk.secret import constants as sc
from gcl_sdk.secret.dm import models

PROJECT = sys_uuid.uuid4()
PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOSomeTestKey comment"


def _make_ssh_key(**overrides) -> models.SSHKey:
    kwargs = dict(
        name="my-ssh-key",
        project_id=PROJECT,
        target=infra_models.NodeTarget.from_node(sys_uuid.uuid4()),
        user="root",
        target_public_key=PUBLIC_KEY,
    )
    kwargs.update(overrides)
    return models.SSHKey(**kwargs)


def test_ssh_key_on_node_target():
    node = sys_uuid.uuid4()
    key = _make_ssh_key(target=infra_models.NodeTarget.from_node(node))

    assert key.target_nodes() == [node]
    assert key.status == sc.SecretStatus.NEW.value
    assert key.authorized_keys == sc.AUTHORIZED_KEYS_PATH
    assert set(key.get_resource_target_fields()) == {
        "uuid",
        "name",
        "description",
        "project_id",
        "constructor",
        "user",
        "authorized_keys",
        "target",
        "target_public_key",
    }


def test_ssh_key_on_node_set_target():
    node_set = sys_uuid.uuid4()
    key = _make_ssh_key(target=infra_models.NodeSetTarget.from_node_set(node_set))

    assert key.target.kind == "node_set"
    assert key.target.owners() == [node_set]
    # The SDK does not own the node inventory: resolving the members
    # of the set is up to the consumer.
    with pytest.raises(NotImplementedError):
        key.target_nodes()


def test_ssh_key_dump_restore_round_trip():
    key = _make_ssh_key()

    view = key.dump_to_simple_view()
    assert view["target"] == {
        "kind": "node",
        "node": str(key.target.node),
    }
    assert view["constructor"] == {"kind": "plain"}

    restored = models.SSHKey.restore_from_simple_view(**view)
    assert restored.uuid == key.uuid
    assert restored.target.node == key.target.node
    assert restored.target_public_key == PUBLIC_KEY


def test_ssh_key_to_host_resource():
    node = sys_uuid.uuid4()
    key = _make_ssh_key(target=infra_models.NodeTarget.from_node(node))

    resource = key.to_host_resource(
        master=key.uuid, node=node, status=sc.SecretStatus.NEW
    )

    assert resource.kind == sc.SSH_KEY_TARGET_KIND
    assert resource.kind == ssh_key_driver.SSH_KEY_TARGET_KIND
    assert resource.uuid == sys_uuid.uuid5(key.uuid, str(node))
    assert resource.master == key.uuid
    assert resource.agent == node
    assert resource.status == sc.SecretStatus.NEW.value
    assert resource.value["user"] == "root"
    assert resource.value["target_public_key"] == PUBLIC_KEY
    assert "target" not in resource.value


def test_ssh_host_key_defaults():
    host_key = models.SSHHostKey(uuid=sys_uuid.uuid4(), user="root")

    assert host_key.authorized_keys == sc.AUTHORIZED_KEYS_PATH
    assert host_key.target_public_key == ""


def test_password_target_fields_depend_on_method():
    auto = models.Password(name="pwd", project_id=PROJECT)
    assert auto.method == sc.SecretMethod.AUTO_HEX.value
    assert "value" not in auto.get_resource_target_fields()

    manual = models.Password(
        name="pwd",
        project_id=PROJECT,
        method=sc.SecretMethod.MANUAL.value,
        value="s3cret",
    )
    assert "value" in manual.get_resource_target_fields()


def test_rsa_key_generates_keys_from_bitness():
    key = models.RSAKey(name="rsa", project_id=PROJECT, bitness=2048)

    assert key.private_key.startswith("-----BEGIN")
    assert key.public_key.startswith("-----BEGIN")
    assert key.bitness == 2048


def test_certificate_builds_and_dumps():
    cert = models.Certificate(
        name="cert",
        project_id=PROJECT,
        email="hostmaster@example.com",
        domains=["example.com", "*.example.com"],
    )

    assert cert.method.kind == "dns_core"
    assert cert.expiration_threshold == 14
    assert set(cert.get_resource_target_fields()) >= {"email", "domains", "method"}


def test_plain_secret_constructor_passthrough():
    constructor = models.PlainSecretConstructor()

    assert constructor.build("plain-text") == "plain-text"
