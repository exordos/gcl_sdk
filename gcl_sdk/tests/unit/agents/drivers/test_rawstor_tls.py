#    Copyright 2026 Genesis Corporation.
#    Licensed under the Apache License, Version 2.0 (the "License").

import datetime
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
import pytest

from gcl_sdk.agents.universal.drivers import rawstor_tls as tls


@pytest.fixture
def credentials():
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.timezone.utc)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.UniformResourceIdentifier("spiffe://rawstor.local/admin/test")]
            ),
            False,
        )
        .sign(key, hashes.SHA256())
    )
    ca_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.UniformResourceIdentifier("spiffe://rawstor.local/admin/test")]
            ),
            False,
        )
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    return dict(
        cert=pem,
        ca=ca_pem,
        key=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        trust_domain="rawstor.local",
    )


@pytest.fixture
def destination(tmp_path, monkeypatch):
    monkeypatch.setattr(tls, "TLS_DIR", tmp_path / "tls")
    monkeypatch.setattr(tls.os, "chown", Mock())
    monkeypatch.setattr(tls.os, "fchown", Mock())
    monkeypatch.setattr(
        tls.pwd, "getpwnam", lambda _: SimpleNamespace(pw_gid=os.getgid())
    )
    return tmp_path / "tls"


def test_publish_pair_once_with_private_permissions(destination, credentials):
    directory, changed = tls.install("mds-test", credentials)
    assert changed
    assert (directory / "current").is_symlink()
    assert (directory / "current/cert.key").stat().st_mode & 0o777 == 0o640
    assert directory.stat().st_mode & 0o777 == 0o750
    assert (directory / "current/cert.crt").read_text() == credentials["cert"]
    assert tls.install("mds-test", credentials) == (directory, False)


def test_old_filenames_are_republished_without_certificate_renewal(
    destination, credentials
):
    values = {k: credentials[k] for k in ("cert", "key", "ca", "trust_domain")}
    digest = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
    directory = destination / "mds-test"
    old = directory / digest
    old.mkdir(parents=True)
    for field, filename in (("cert", "cert.pem"), ("key", "key.pem"), ("ca", "ca.pem")):
        (old / filename).write_text(values[field])
    (directory / "current").symlink_to(digest)

    _, changed = tls.install("mds-test", credentials)

    assert changed
    env = tls.environment(directory)
    for variable, filename, field in (
        ("RAWSTOR_TLS_CERT", "cert.crt", "cert"),
        ("RAWSTOR_TLS_KEY", "cert.key", "key"),
        ("RAWSTOR_TLS_CA", "ca.crt", "ca"),
    ):
        assert env[variable] == str(directory / "current" / filename)
        assert Path(env[variable]).read_text() == values[field]
    assert (old / "key.pem").read_text() == values["key"]
    assert tls.install("mds-test", credentials) == (directory, False)


def test_rotation_keeps_previous_pair_until_publish(destination, credentials):
    directory, _ = tls.install("mds-test", credentials)
    previous = (directory / "current").resolve()
    replacement = dict(credentials, cert=credentials["cert"] + "\n")
    assert tls.install("mds-test", replacement)[1]
    assert (directory / "current").resolve() != previous
    assert (previous / "cert.key").read_text() == credentials["key"]


@pytest.mark.parametrize("value", [{}, {"pending": True}])
def test_pending_credentials_fail_closed(destination, value):
    with pytest.raises(RuntimeError, match="not ready"):
        tls.install("mds-test", value)
    assert not destination.exists()


def test_mismatched_pair_does_not_replace_current(destination, credentials):
    directory, _ = tls.install("mds-test", credentials)
    old = os.readlink(directory / "current")
    other = ec.generate_private_key(ec.SECP256R1())
    bad = dict(
        credentials,
        key=other.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
    )
    with pytest.raises(ValueError, match="do not match"):
        tls.install("mds-test", bad)
    assert os.readlink(directory / "current") == old


def test_failed_reload_is_retried(destination, credentials, monkeypatch):
    directory, _ = tls.install("mds-test", credentials)
    reload = Mock(side_effect=[RuntimeError("failed"), None])
    monkeypatch.setattr(tls, "reload_service", reload)
    with pytest.raises(RuntimeError):
        tls.reload_if_changed(directory, "rawstor-mds@test")
    tls.reload_if_changed(directory, "rawstor-mds@test")
    tls.reload_if_changed(directory, "rawstor-mds@test")
    assert reload.call_count == 2


def test_reload_uses_usr1(monkeypatch):
    run = Mock()
    monkeypatch.setattr(tls.subprocess, "run", run)
    tls.reload_service("rawstor-vhost@test")
    assert "--signal=SIGUSR1" in run.call_args.args[0]


def test_worker_uses_current_credentials_and_returns_errno(credentials, monkeypatch):
    run = Mock(
        return_value=SimpleNamespace(
            returncode=0, stdout=json.dumps({"errno": 2, "error": "missing"})
        )
    )
    monkeypatch.setattr(tls.subprocess, "run", run)
    monkeypatch.setenv("RAWSTOR_TLS_MDS_ID", "stale-process-pin")
    with tls.temporary_bindings(credentials) as client:
        with pytest.raises(FileNotFoundError):
            client.Target("mds://127.0.0.1:7776/test").spec()
        env = run.call_args.kwargs["env"]
        assert "RAWSTOR_TLS_MDS_ID" not in env
        key = Path(env["RAWSTOR_TLS_KEY"])
        assert key.stat().st_mode & 0o777 == 0o600
    assert not key.exists()


def test_vhost_rotates_tls_without_restarting_or_resizing(
    destination, credentials, monkeypatch, tmp_path
):
    import uuid

    pytest.importorskip("libvirt")
    from gcl_sdk.agents.universal.drivers import exordos_hyper

    driver = object.__new__(exordos_hyper.ExordosLocalHyperDriver)
    monkeypatch.setattr(
        driver, "_vhost_drop_in_dir", lambda _: str(tmp_path / "vhost-dropin")
    )
    run = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(tls.subprocess, "run", run)
    monkeypatch.setattr(tls.subprocess, "check_call", Mock())
    volume = SimpleNamespace(
        uuid=uuid.uuid4(),
        storage_location="mds://host:7776/",
        rawstor_tls={"control": credentials, "service": credentials},
    )
    driver.configure_rawstor_tls(volume)
    driver.configure_rawstor_tls(volume)
    signals = [call for call in run.call_args_list if "kill" in call.args[0]]
    assert len(signals) == 1
    renewed = dict(credentials, cert=credentials["cert"] + "\n")
    volume.rawstor_tls["service"] = renewed
    driver.configure_rawstor_tls(volume)
    signals = [call for call in run.call_args_list if "kill" in call.args[0]]
    assert len(signals) == 2
    assert all("--signal=SIGUSR1" in call.args[0] for call in signals)
    assert all("restart" not in call.args[0] for call in run.call_args_list)
    env = (tmp_path / "vhost-dropin/tls.conf").read_text()
    assert "RAWSTOR_TLS_CERT=" in env and "--insecure" not in env
    volume.rawstor_tls = {}
    driver.configure_rawstor_tls(volume)


def test_control_plane_key_is_not_readable_by_rawstor_services(
    destination, credentials
):
    directory, _ = tls.install("control-test", credentials)
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "current/cert.key").stat().st_mode & 0o777 == 0o600


def test_inventory_collects_specs_in_one_worker(credentials, monkeypatch):
    run = Mock(
        return_value=SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "value": [
                        {
                            "uri": "mds://host/object",
                            "spec": {"size": 1 << 30, "chunk_size": 1 << 30},
                        }
                    ]
                }
            ),
        )
    )
    monkeypatch.setattr(tls.subprocess, "run", run)
    with tls.temporary_bindings(credentials) as client:
        objects = client.Location("mds://host/").objects()
        assert objects[0].uri == "mds://host/object"
        assert objects[0].spec().size == 1 << 30
        assert run.call_count == 1
