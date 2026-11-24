#    Copyright 2026 Genesis Corporation.
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License.

"""Runtime credentials and isolated TLS clients for Rawstor.

Bindings initialize one process-wide TLS context. Run control operations in a
fresh child process so multiple identities and renewed credentials never reuse
an old context or disturb live vhost connections.
"""

from contextlib import contextmanager
import datetime
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import uuid

TLS_DIR = Path("/etc/rawstor/tls")


def install(name, credentials):
    """Publish a complete immutable PEM generation through one atomic symlink.

    Only the rawstor group can read private keys. CA private keys are never
    accepted. Missing credentials fail closed, including while issuance is pending.
    """
    if not credentials or credentials.get("pending"):
        raise RuntimeError("Rawstor TLS credentials are not ready")
    if not name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in name):
        raise ValueError("Invalid Rawstor credential name")
    values = {k: credentials[k] for k in ("cert", "key", "ca", "trust_domain")}
    domain = values["trust_domain"]
    if not domain or any(
        c not in "abcdefghijklmnopqrstuvwxyz0123456789.-_" for c in domain
    ):
        raise ValueError("Invalid Rawstor trust domain")
    if any(not isinstance(v, str) or not v for v in values.values()):
        raise ValueError("Incomplete Rawstor TLS credentials")
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    leaf = x509.load_pem_x509_certificate(values["cert"].encode())
    ca = x509.load_pem_x509_certificate(values["ca"].encode())
    leaf.verify_directly_issued_by(ca)
    if (
        leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        or not ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    ):
        raise ValueError("Rawstor requires a workload leaf and an issuing CA")
    now = datetime.datetime.now(datetime.timezone.utc)
    if not leaf.not_valid_before_utc <= now < leaf.not_valid_after_utc:
        raise ValueError("Rawstor certificate is not currently valid")
    identities = leaf.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.UniformResourceIdentifier)
    if len(identities) != 1 or not identities[0].startswith(f"spiffe://{domain}/"):
        raise ValueError("Rawstor certificate has an unexpected SPIFFE identity")
    key = serialization.load_pem_private_key(values["key"].encode(), password=None)
    form = serialization.PublicFormat.SubjectPublicKeyInfo
    if key.public_key().public_bytes(
        serialization.Encoding.DER, form
    ) != leaf.public_key().public_bytes(serialization.Encoding.DER, form):
        raise ValueError("Rawstor certificate and key do not match")
    digest = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
    directory = TLS_DIR / name
    directory_mode = 0o700 if name.startswith("control-") else 0o750
    file_mode = 0o600 if name.startswith("control-") else 0o640
    group = pwd.getpwnam("rawstor").pw_gid
    directory.mkdir(parents=True, exist_ok=True, mode=directory_mode)
    os.chown(TLS_DIR, 0, group)
    os.chmod(TLS_DIR, 0o750)
    os.chown(directory, 0, group)
    os.chmod(directory, directory_mode)
    current = directory / "current"
    if current.is_symlink() and os.readlink(current) == digest:
        return directory, False
    generation = directory / digest
    if not generation.exists():
        with tempfile.TemporaryDirectory(dir=directory) as staged:
            stage = Path(staged)
            os.chown(stage, 0, group)
            os.chmod(stage, directory_mode)
            for key, filename in (
                ("cert", "cert.pem"),
                ("key", "key.pem"),
                ("ca", "ca.pem"),
            ):
                path = stage / filename
                # Create private material with restrictive permissions from the start.
                with open(
                    os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, file_mode), "w"
                ) as f:
                    f.write(values[key])
                    f.flush()
                    os.fsync(f.fileno())
                    os.fchown(f.fileno(), 0, group)
            (stage / "domain").write_text(domain)
            stage.rename(generation)
    link = directory / (".current-" + uuid.uuid4().hex)
    try:
        link.symlink_to(digest)
        link.replace(current)
    finally:
        link.unlink(missing_ok=True)
    return directory, True


def remove(name):
    """Remove a stopped workload's local credentials; keep the issuer CA elsewhere."""
    path = TLS_DIR / name
    if path.exists():
        shutil.rmtree(path)


def environment(directory):
    current = Path(directory) / "current"
    return {
        "RAWSTOR_TLS_TRUST_DOMAIN": (current / "domain").read_text(),
        "RAWSTOR_TLS_CERT": str(current / "cert.pem"),
        "RAWSTOR_TLS_KEY": str(current / "key.pem"),
        "RAWSTOR_TLS_CA": str(current / "ca.pem"),
    }


def service_environment(directory):
    return "".join(
        f'Environment="{key}={value}"\n'
        for key, value in environment(directory).items()
    )


def reload_service(unit):
    """SIGUSR1 reloads TLS; SIGHUP is reserved for topology/resize."""
    subprocess.run(
        ["systemctl", "kill", "--kill-whom=main", "--signal=SIGUSR1", unit], check=True
    )


def reload_if_changed(directory, unit):
    directory = Path(directory)
    generation = os.readlink(directory / "current")
    acknowledged = directory / "loaded"
    if acknowledged.exists() and acknowledged.read_text() == generation:
        return
    reload_service(unit)
    acknowledged.write_text(generation)


@contextmanager
def temporary_bindings(credentials):
    """Use control credentials in an unprivileged API process without persisting them."""
    if not credentials or credentials.get("pending"):
        raise RuntimeError("Rawstor TLS credentials are not ready")
    with tempfile.TemporaryDirectory(prefix="rawstor-client-") as staged:
        current = Path(staged) / "current"
        current.mkdir(mode=0o700)
        for name, field in (
            ("cert.pem", "cert"),
            ("key.pem", "key"),
            ("ca.pem", "ca"),
            ("domain", "trust_domain"),
        ):
            with open(
                os.open(current / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600),
                "w",
            ) as stream:
                stream.write(credentials[field])
        yield Bindings(staged)


class Bindings:
    def __init__(self, directory):
        self.directory = directory

    def Target(self, uri):
        return _Remote(self, "target", uri)

    def Location(self, uri):
        return _Remote(self, "location", uri)

    def call(self, kind, uri, method, *args, **kwargs):
        env = {k: v for k, v in os.environ.items() if not k.startswith("RAWSTOR_TLS_")}
        env.update(environment(self.directory))
        result = subprocess.run(
            [sys.executable, "-m", __name__],
            input=json.dumps([kind, uri, method, args, kwargs]),
            text=True,
            capture_output=True,
            env=env,
            timeout=120,
        )
        if result.returncode:
            # Native diagnostics contain endpoints, not credential material.
            raise RuntimeError("Rawstor TLS worker failed: " + result.stderr[-2000:])
        response = json.loads(result.stdout)
        if "errno" in response:
            raise OSError(response["errno"], response["error"])
        value = response["value"]
        if method in ("info", "spec"):
            return SimpleNamespace(**value)
        if method == "objects":
            return [self.Target(item) for item in value]
        return value


class _Remote:
    def __init__(self, bindings, kind, uri):
        self.bindings, self.kind, self.uri = bindings, kind, uri

    def __getattr__(self, method):
        return lambda *args, **kwargs: self.bindings.call(
            self.kind, self.uri, method, *args, **kwargs
        )


def _main():
    import rawstor

    kind, uri, method, args, kwargs = json.load(sys.stdin)
    allowed = {
        "target": {"spec", "create", "remove", "resize"},
        "location": {"info", "objects", "create_account"},
    }
    if kind not in allowed or method not in allowed[kind]:
        raise ValueError("Unsupported Rawstor operation")
    rawstor.initialize()
    obj = rawstor.Target(uri) if kind == "target" else rawstor.Location(uri)
    try:
        value = getattr(obj, method)(*args, **kwargs)
        if method == "info":
            value = {k: getattr(value, k) for k in ("total", "used")}
        elif method == "spec":
            value = {k: getattr(value, k) for k in ("size", "chunk_size")}
        elif method == "objects":
            value = [target.uri for target in value]
        else:
            value = None
        print(json.dumps({"value": value}))
    except OSError as exc:
        print(json.dumps({"errno": exc.errno, "error": str(exc)}))


if __name__ == "__main__":
    _main()
