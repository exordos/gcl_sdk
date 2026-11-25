#    Copyright 2026 Genesis Corporation.
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License.

"""Runtime credentials and isolated TLS clients for RawStor.

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


def _trust_domain(ca_pem):
    from cryptography import x509

    ca = x509.load_pem_x509_certificate(ca_pem.encode())
    try:
        identities = ca.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value.get_values_for_type(x509.UniformResourceIdentifier)
    except x509.ExtensionNotFound as error:
        raise ValueError("RawStor CA requires a SPIFFE trust domain") from error
    if len(identities) != 1 or not identities[0].startswith("spiffe://"):
        raise ValueError("RawStor CA requires one SPIFFE trust domain")
    domain = identities[0][len("spiffe://") :]
    if not domain or any(
        c not in "abcdefghijklmnopqrstuvwxyz0123456789.-_" for c in domain
    ):
        raise ValueError("Invalid RawStor CA trust domain")
    return domain


def install(name, credentials):
    """Publish a complete immutable PEM generation through one atomic symlink.

    Only the rawstor group can read private keys. CA private keys are never
    accepted. Missing credentials fail closed, including while issuance is pending.
    """
    if not credentials or credentials.get("pending"):
        raise RuntimeError("RawStor TLS credentials are not ready")
    if not name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in name):
        raise ValueError("Invalid RawStor credential name")
    values = {k: credentials[k] for k in ("cert", "key", "ca")}
    if any(not isinstance(v, str) or not v for v in values.values()):
        raise ValueError("Incomplete RawStor TLS credentials")
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    domain = _trust_domain(values["ca"])
    leaf = x509.load_pem_x509_certificate(values["cert"].encode())
    ca = x509.load_pem_x509_certificate(values["ca"].encode())
    leaf.verify_directly_issued_by(ca)
    if (
        leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        or not ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    ):
        raise ValueError("RawStor requires a workload leaf and an issuing CA")
    now = datetime.datetime.now(datetime.timezone.utc)
    if not leaf.not_valid_before_utc <= now < leaf.not_valid_after_utc:
        raise ValueError("RawStor certificate is not currently valid")
    identities = leaf.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.UniformResourceIdentifier)
    if len(identities) != 1 or not identities[0].startswith(f"spiffe://{domain}/"):
        raise ValueError("RawStor certificate has an unexpected SPIFFE identity")
    key = serialization.load_pem_private_key(values["key"].encode(), password=None)
    form = serialization.PublicFormat.SubjectPublicKeyInfo
    if key.public_key().public_bytes(
        serialization.Encoding.DER, form
    ) != leaf.public_key().public_bytes(serialization.Encoding.DER, form):
        raise ValueError("RawStor certificate and key do not match")
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
                ("cert", "cert.crt"),
                ("key", "cert.key"),
                ("ca", "ca.crt"),
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
        "RAWSTOR_TLS_TRUST_DOMAIN": _trust_domain((current / "ca.crt").read_text()),
        "RAWSTOR_TLS_CERT": str(current / "cert.crt"),
        "RAWSTOR_TLS_KEY": str(current / "cert.key"),
        "RAWSTOR_TLS_CA": str(current / "ca.crt"),
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
        raise RuntimeError("RawStor TLS credentials are not ready")
    with tempfile.TemporaryDirectory(prefix="rawstor-client-") as staged:
        current = Path(staged) / "current"
        current.mkdir(mode=0o700)
        for name, field in (
            ("cert.crt", "cert"),
            ("cert.key", "key"),
            ("ca.crt", "ca"),
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
            raise RuntimeError("RawStor TLS worker failed: " + result.stderr[-2000:])
        response = json.loads(result.stdout)
        if "errno" in response:
            raise OSError(response["errno"], response["error"])
        value = response["value"]
        if method in ("info", "spec"):
            return SimpleNamespace(**value)
        if method == "objects":
            return [
                _Remote(self, "target", item["uri"], item["spec"]) for item in value
            ]
        return value


class _Remote:
    def __init__(self, bindings, kind, uri, spec=None):
        self.bindings, self.kind, self.uri = bindings, kind, uri
        self._spec = spec

    def spec(self):
        if self._spec is not None:
            return SimpleNamespace(**self._spec)
        return self.bindings.call(self.kind, self.uri, "spec")

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
        raise ValueError("Unsupported RawStor operation")
    rawstor.initialize()
    obj = rawstor.Target(uri) if kind == "target" else rawstor.Location(uri)
    try:
        value = getattr(obj, method)(*args, **kwargs)
        if method == "info":
            value = {k: getattr(value, k) for k in ("total", "used")}
        elif method == "spec":
            value = {k: getattr(value, k) for k in ("size", "chunk_size")}
        elif method == "objects":
            objects = []
            for target in value:
                spec = target.spec()
                objects.append(
                    {
                        "uri": target.uri,
                        "spec": {"size": spec.size, "chunk_size": spec.chunk_size},
                    }
                )
            value = objects
        else:
            value = None
        print(json.dumps({"value": value}))
    except OSError as exc:
        print(json.dumps({"errno": exc.errno, "error": str(exc)}))


if __name__ == "__main__":
    _main()
