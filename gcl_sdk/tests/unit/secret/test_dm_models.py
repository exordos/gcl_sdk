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

import datetime
import uuid as sys_uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
import pytest

from gcl_sdk.agents.universal.drivers import ssh_key as ssh_key_driver
from gcl_sdk.infra.dm import models as infra_models
from gcl_sdk.secret import constants as sc
from gcl_sdk.secret.dm import models

PROJECT = sys_uuid.uuid4()
PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOSomeTestKey comment"


def _make_pem_pair(
    common_name: str = "example.com",
    issuer_key: ec.EllipticCurvePrivateKey | None = None,
    issuer_name: str | None = None,
) -> tuple[ec.EllipticCurvePrivateKey, str, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.timezone.utc)
    subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, common_name)])
    issuer = x509.Name(
        [x509.NameAttribute(x509.NameOID.COMMON_NAME, issuer_name or common_name)]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(issuer_key or key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    return key, key_pem, cert_pem


def _make_manual_certificate(key: str | None, cert: str | None) -> models.Certificate:
    return models.Certificate(
        name="cert",
        project_id=PROJECT,
        email="hostmaster@example.com",
        domains=["example.com"],
        method=models.ManualCertificateMethod(),
        key=key,
        cert=cert,
    )


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
    fields = set(cert.get_resource_target_fields())
    assert fields >= {"email", "domains", "method"}
    assert not fields & {"key", "cert"}


def test_manual_certificate_targets_key_material():
    _, key_pem, cert_pem = _make_pem_pair()

    cert = _make_manual_certificate(key_pem, cert_pem)

    assert cert.method.kind == "manual"
    assert set(cert.get_resource_target_fields()) >= {"key", "cert"}

    view = cert.dump_to_simple_view()
    assert view["method"] == {"kind": "manual"}
    restored = models.Certificate.restore_from_simple_view(**view)
    assert restored.method.kind == "manual"
    assert restored.cert == cert_pem


def test_manual_certificate_accepts_valid_chain():
    ca_key, _, ca_pem = _make_pem_pair(common_name="ca")
    _, key_pem, leaf_pem = _make_pem_pair(issuer_key=ca_key, issuer_name="ca")

    cert = _make_manual_certificate(key_pem, leaf_pem + ca_pem)

    assert cert.cert == leaf_pem + ca_pem


def test_manual_certificate_requires_key_material():
    _, key_pem, _ = _make_pem_pair()

    with pytest.raises(ValueError, match="require string key and cert"):
        _make_manual_certificate(key_pem, None)


def test_manual_certificate_rejects_mismatched_pair():
    _, key_pem, _ = _make_pem_pair()
    _, _, other_cert_pem = _make_pem_pair()

    with pytest.raises(ValueError, match="do not match"):
        _make_manual_certificate(key_pem, other_cert_pem)


def test_manual_certificate_rejects_broken_chain():
    _, key_pem, leaf_pem = _make_pem_pair()
    # Same subject as the leaf issuer, but signed by an unrelated key
    _, _, unrelated_pem = _make_pem_pair()

    with pytest.raises(ValueError, match="invalid signature"):
        _make_manual_certificate(key_pem, leaf_pem + unrelated_pem)


def test_manual_certificate_rejects_non_certificate_data():
    _, key_pem, cert_pem = _make_pem_pair()

    with pytest.raises(ValueError, match="non-certificate data"):
        _make_manual_certificate(key_pem, cert_pem + "garbage\n")


def test_secret_target_fields():
    secret = models.Secret(name="token", project_id=PROJECT, value="v")

    assert secret.status == sc.SecretStatus.NEW.value
    assert set(secret.get_resource_target_fields()) == {
        "uuid",
        "name",
        "description",
        "project_id",
        "constructor",
        "value",
    }


def test_secret_effective_value_falls_back_to_default():
    secret = models.Secret(name="token", project_id=PROJECT, default_value="def")

    assert secret.value is None
    assert secret.effective_value == "def"
    assert secret.dump_to_simple_view()["value"] == "def"

    secret.value = "explicit"
    assert secret.effective_value == "explicit"
    assert secret.dump_to_simple_view()["value"] == "explicit"


def test_plain_secret_constructor_passthrough():
    constructor = models.PlainSecretConstructor()

    assert constructor.build("plain-text") == "plain-text"
