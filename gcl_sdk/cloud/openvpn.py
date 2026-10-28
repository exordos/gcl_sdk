# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0

"""OpenVPN bootstrap and agent configuration for routed cloud networks."""

import datetime
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import tempfile
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID
from cryptography.x509.oid import NameOID
import yaml

ROOT = Path("/var/lib/exordos/network-links")


def identity(value):
    return uuid.UUID(str(value)).hex


def _key_pem(key):
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def certificate(name, ca=None, server=False, private_key=None):
    key = (
        serialization.load_pem_private_key(private_key.encode(), None)
        if private_key
        else ec.generate_private_key(ec.SECP256R1())
    )
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    signer = serialization.load_pem_private_key(ca["key"].encode(), None) if ca else key
    issuer = (
        x509.load_pem_x509_certificate(ca["cert"].encode()).subject if ca else subject
    )
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=3650 if ca is None else 365))
        .add_extension(
            x509.BasicConstraints(ca=ca is None, path_length=0 if ca is None else None),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=ca is not None,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=ca is None,
                crl_sign=ca is None,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    )
    if ca:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage(
                [
                    ExtendedKeyUsageOID.SERVER_AUTH
                    if server
                    else ExtendedKeyUsageOID.CLIENT_AUTH
                ]
            ),
            critical=False,
        )
    cert = builder.sign(signer, hashes.SHA256())
    return {
        "cert": cert.public_bytes(serialization.Encoding.PEM).decode(),
        "key": _key_pem(key),
    }


def expiring(pem, within_days=30):
    deadline = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        days=within_days
    )
    return x509.load_pem_x509_certificate(pem.encode()).not_valid_after_utc <= deadline


def tls_key(server_key=None):
    """Use OpenVPN's supported tls-crypt-v2 format, without logging key material."""
    with tempfile.TemporaryDirectory(prefix="exordos-openvpn-") as directory:
        target = Path(directory) / "generated.key"
        command = ["openvpn"]
        if server_key:
            source = Path(directory) / "server.key"
            source.write_text(server_key)
            source.chmod(0o600)
            command += ["--tls-crypt-v2", str(source)]
        command += [
            "--genkey",
            "tls-crypt-v2-client" if server_key else "tls-crypt-v2-server",
            str(target),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True)
        except FileNotFoundError as error:
            raise RuntimeError(
                "Install OpenVPN 2.6+ on the Core general-service host "
                "to generate tls-crypt-v2 keys"
            ) from error
        return target.read_text()


def paths(resource):
    own = identity(resource)
    return ROOT / own, Path("/run/exordos-openvpn") / own


def _route(cidr, directive="route"):
    network = ipaddress.IPv4Network(cidr)
    if not network.prefixlen:
        raise ValueError("Default routes are not allowed in cloud network links")
    return f"{directive} {network.network_address} {network.netmask}"


def render(resource, own, peers, ca_cert, protocol="tcp", port=443, mtu=1380):
    """Render one listener and one client per lower-UUID peer."""
    own_id = identity(resource)
    if protocol not in {"tcp", "udp"} or not 1 <= int(port) <= 65535:
        raise ValueError("Invalid OpenVPN transport")
    if not 1280 <= int(mtu) <= 1500:
        raise ValueError("Invalid tunnel MTU")
    root, runtime = paths(resource)
    prefix = "cl" + own_id[:8]
    files = {"ca.crt": ca_cert}
    services, expected = [], {}
    gateway_ip = own.get("gateway_ip")
    if gateway_ip is not None:
        gateway_ip = str(ipaddress.IPv4Address(gateway_ip))
        if ipaddress.IPv4Address(gateway_ip) not in ipaddress.IPv4Network(own["cidr"]):
            raise ValueError("Gateway address must belong to its local subnet")

    def route_source(name, remote_peers):
        if gateway_ip is None:
            return []
        filename = "route-" + name + ".sh"
        files[filename] = "#!/bin/sh\nset -eu\n" + "".join(
            f"ip route replace {ipaddress.IPv4Network(peer['cidr'])} "
            f'via "$route_vpn_gateway" dev "$dev" src {gateway_ip}\n'
            for peer in remote_peers
        )
        return ["script-security 2", f'route-up "/bin/sh {root / filename}"']

    common = [
        "dev-type tun",
        "persist-key",
        "persist-tun",
        "nobind",
        "disable-dco",
        "ca ca.crt",
        "tls-version-min 1.2",
        "data-ciphers AES-256-GCM:AES-128-GCM",
        f"tun-mtu {int(mtu)}",
        "auth-nocache",
        "verb 3",
    ]
    higher = [p for p in peers if identity(p["id"]) > own_id]
    lower = [p for p in peers if identity(p["id"]) < own_id]
    if higher:
        name = "server"
        subnet = ipaddress.IPv4Network(own["tunnel_cidr"])
        lines = [line for line in common if line != "nobind"] + [
            f"dev {prefix}s",
            f"proto {'tcp-server' if protocol == 'tcp' else 'udp'}",
            f"port {int(port)}",
            "topology subnet",
            f"server {subnet.network_address} {subnet.netmask} nopool",
            "cert server.crt",
            "key server.key",
            "dh none",
            "remote-cert-tls client",
            "client-config-dir ccd",
            "ccd-exclusive",
            "tls-crypt-v2 tls-server.key",
            "keepalive 10 60",
            f"management {runtime / (name + '.sock')} unix",
        ]
        files.update(
            {
                "server.crt": own["server"]["cert"],
                "server.key": own["server"]["key"],
                "tls-server.key": own["tls_server_key"],
            }
        )
        for index, peer in enumerate(sorted(higher, key=lambda p: identity(p["id"]))):
            peer_id = identity(peer["id"])
            lines.append(_route(peer["cidr"]))
            files[f"ccd/{peer_id}-client"] = (
                f"ifconfig-push {subnet.network_address + index + 2} {subnet.netmask}\n"
                + _route(peer["cidr"], "iroute")
                + "\n"
            )
            expected[peer_id] = {"service": name, "client": peer_id + "-client"}
        lines += route_source(name, higher)
        files[name + ".conf"] = "\n".join(lines) + "\n"
        services.append(name)
    for index, peer in enumerate(sorted(lower, key=lambda p: identity(p["id"]))):
        peer_id = identity(peer["id"])
        host = str(ipaddress.IPv4Address(peer["endpoint"]))
        name = "client-" + peer_id
        lines = common + [
            "client",
            "route-nopull",
            f"dev {prefix}c{index}",
            f"proto {'tcp-client' if protocol == 'tcp' else 'udp'}",
            f"remote {host} {int(port)}",
            "cert client.crt",
            "key client.key",
            "remote-cert-tls server",
            f"verify-x509-name {peer_id}-server name",
            f"tls-crypt-v2 tls-{peer_id}.key",
            _route(peer["cidr"]),
            "ping 10",
            "ping-restart 60",
            "connect-retry 2 30",
            f"management {runtime / (name + '.sock')} unix",
        ]
        lines += route_source(name, [peer])
        files.update(
            {
                "client.crt": own["client"]["cert"],
                "client.key": own["client"]["key"],
                f"tls-{peer_id}.key": own["tls_clients"][peer_id],
            }
        )
        files[name + ".conf"] = "\n".join(lines) + "\n"
        services.append(name)
        expected[peer_id] = {"service": name}
    return {
        "files": files,
        "services": services,
        "peers": expected,
        "local_cidrs": [str(ipaddress.IPv4Network(own["cidr"]))],
        "remote_cidrs": [str(ipaddress.IPv4Network(p["cidr"])) for p in peers],
    }


def service_files(resource, bundle):
    own = identity(resource)
    root, runtime = paths(resource)
    prefix = "cl" + own[:8] + "+"
    chain = "EXO" + own[:12]
    rules = []
    for local in bundle["local_cidrs"]:
        for remote in bundle["remote_cidrs"]:
            local, remote = (
                str(ipaddress.IPv4Network(local)),
                str(ipaddress.IPv4Network(remote)),
            )
            rules += [
                f"-A {chain} -i {prefix} -s {remote} -d {local} -j ACCEPT",
                f"-A {chain} -o {prefix} -s {local} -d {remote} -j ACCEPT",
            ]
    script = [
        "#!/bin/sh",
        "set -eu",
        f"mkdir -p {ROOT}",
        "(",
        "flock 9",
        f"if [ ! -f {ROOT / 'openvpn-forwarding.before'} ]; then cat /proc/sys/net/ipv4/ip_forward > {ROOT / 'openvpn-forwarding.before'}; fi",
        f"touch {ROOT / ('openvpn-owner-' + own)}",
        f") 9>{ROOT / 'openvpn-forwarding.lock'}",
        "sysctl -w net.ipv4.ip_forward=1 >/dev/null",
        "iptables-restore --noflush <<'EXORDOS_RULES'",
        "*filter",
        f":{chain} - [0:0]",
        f"-F {chain}",
        *rules,
        f"-A {chain} -j DROP",
        "COMMIT",
        "EXORDOS_RULES",
    ]
    for args in [
        ["FORWARD", "-i", prefix, "-j", chain],
        ["FORWARD", "-o", prefix, "-j", chain],
        ["INPUT", "-i", prefix, "-j", "DROP"],
        [
            "INPUT",
            "-i",
            prefix,
            "-m",
            "conntrack",
            "--ctstate",
            "ESTABLISHED,RELATED",
            "-j",
            "ACCEPT",
        ],
    ]:
        script.append(
            shlex.join(["iptables", "-w", "-C", *args])
            + " 2>/dev/null || "
            + shlex.join(["iptables", "-w", "-I", *args])
        )
    result = []
    for name, content in bundle["files"].items():
        if not re.fullmatch(
            r"(?:ccd/)?[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+|ccd/[a-zA-Z0-9_-]+", name
        ):
            raise ValueError("Invalid OpenVPN managed filename")
        result.append((str(root / name), content, "0600"))
    script_path = f"/usr/local/sbin/exordos-openvpn-{own}"
    result.append((script_path, "\n".join(script) + "\n", "0700"))
    forward = f"exordos-openvpn-{own}-forward.service"
    result.append(
        (
            f"/etc/systemd/system/{forward}",
            f"[Unit]\nDescription=Exordos cloud link forwarding\nBefore=network-online.target\n[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart={script_path}\n[Install]\nWantedBy=multi-user.target\n",
            "0644",
        )
    )
    for name in bundle["services"]:
        if not re.fullmatch(r"server|client-[a-f0-9]{32}", name):
            raise ValueError("Invalid OpenVPN service name")
        unit = f"exordos-openvpn-{own}-{name}.service"
        content = (
            f"[Unit]\nDescription=Exordos cloud link OpenVPN\nAfter=network-online.target {forward}\nRequires={forward}\n"
            f"[Service]\nRuntimeDirectory=exordos-openvpn/{own}\nRuntimeDirectoryMode=0700\nRuntimeDirectoryPreserve=yes\n"
            f"ExecStart=/usr/sbin/openvpn --cd {root} --config {name}.conf\nRestart=always\nRestartSec=3\n"
            "[Install]\nWantedBy=multi-user.target\n"
        )
        result.append((f"/etc/systemd/system/{unit}", content, "0644"))
    result.append(
        (str(root / "bundle.json"), json.dumps(bundle, sort_keys=True), "0600")
    )
    return result


def units(resource, bundle):
    own = identity(resource)
    return [
        f"exordos-openvpn-{own}-forward.service",
        *[f"exordos-openvpn-{own}-{s}.service" for s in bundle["services"]],
    ]


def bootstrap_user_data(resource, bundle, agent_data):
    data = yaml.safe_load(agent_data)
    data["packages"] = list(
        dict.fromkeys([*data.get("packages", []), "openvpn", "iptables"])
    )
    data["write_files"] += [
        {"path": p, "content": c, "permissions": m, "owner": "root:root"}
        for p, c, m in service_files(resource, bundle)
    ]
    # The early cloud-config startup path must preserve transport-first order
    # too. If packages are not yet present, cloud-final retries this setup.
    for command in data.get("bootcmd", []):
        if command[:2] == ["systemd-run", "--unit=exordos-cloud-bootstrap"]:
            command[-1] = (
                "systemctl daemon-reload; "
                + shlex.join(["systemctl", "enable", "--now", *units(resource, bundle)])
                + "; "
                + command[-1]
            )
    data["runcmd"] = [
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "--now", *units(resource, bundle)],
        *data["runcmd"],
    ]
    return "#cloud-config\n" + yaml.safe_dump(data)


def read_manifest(resource):
    root, _ = paths(resource)
    return json.loads((root / "bundle.json").read_text())


def read_bundle(resource):
    root, _ = paths(resource)
    bundle = read_manifest(resource)
    for name in bundle["files"]:
        bundle["files"][name] = (root / name).read_text()
    return bundle


def install(resource, bundle):
    files = service_files(resource, bundle)
    try:
        old = read_manifest(resource)
        try:
            observed = read_bundle(resource)
        except FileNotFoundError:
            observed = None
    except FileNotFoundError:
        old = observed = None
    # Bootstrap already installs the exact target before the agent can run.
    if observed == bundle and all(
        subprocess.run(["systemctl", "is-active", "--quiet", u]).returncode == 0
        for u in units(resource, bundle)
    ):
        return
    if old:
        subprocess.run(
            ["systemctl", "stop", *reversed(units(resource, old))],
            check=True,
            capture_output=True,
        )
        removed = sorted(set(units(resource, old)) - set(units(resource, bundle)))
        if removed:
            subprocess.run(
                ["systemctl", "disable", *removed], check=True, capture_output=True
            )
        keep = {path for path, _, _ in files}
        for path, _, _ in service_files(resource, old):
            if path not in keep:
                Path(path).unlink(missing_ok=True)
    for path, content, mode in files:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=target.parent, delete=False
        ) as handle:
            os.fchmod(handle.fileno(), int(mode, 8))
            handle.write(content)
        Path(handle.name).replace(target)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(
        ["systemctl", "enable", "--now", *units(resource, bundle)],
        check=True,
        capture_output=True,
    )


def _management(path):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(2)
        connection.connect(str(path))
        connection.recv(4096)
        chunks = []
        for command in [b"state\n", b"status 3\n"]:
            connection.sendall(command)
            reply = b""
            while b"END" not in reply.splitlines():
                chunk = connection.recv(65536)
                if not chunk:
                    raise OSError("OpenVPN management closed before its reply")
                reply += chunk
            chunks.append(reply)
        return b"".join(chunks).decode()


def connected(resource, bundle):
    _, runtime = paths(resource)
    observed = {}
    try:
        for service in bundle["services"]:
            observed[service] = _management(runtime / (service + ".sock"))
    except (OSError, TimeoutError):
        return False
    return bool(bundle["peers"]) and all(
        (
            any(
                line.startswith("CLIENT_LIST\t" + p["client"] + "\t")
                for line in observed[p["service"]].splitlines()
            )
            if "client" in p
            else "CONNECTED,SUCCESS" in observed[p["service"]]
        )
        for p in bundle["peers"].values()
    )


def uninstall(resource):
    try:
        bundle = read_manifest(resource)
    except FileNotFoundError:
        return
    subprocess.run(
        ["systemctl", "disable", "--now", *reversed(units(resource, bundle))],
        check=True,
        capture_output=True,
    )
    own = identity(resource)
    prefix, chain = "cl" + own[:8] + "+", "EXO" + own[:12]
    subprocess.run(
        ["iptables", "-w", "-D", "INPUT", "-i", prefix, "-j", "DROP"],
        capture_output=True,
    )
    subprocess.run(
        [
            "iptables",
            "-w",
            "-D",
            "INPUT",
            "-i",
            prefix,
            "-m",
            "conntrack",
            "--ctstate",
            "ESTABLISHED,RELATED",
            "-j",
            "ACCEPT",
        ],
        capture_output=True,
    )
    for direction in ["-i", "-o"]:
        subprocess.run(
            ["iptables", "-w", "-D", "FORWARD", direction, prefix, "-j", chain],
            capture_output=True,
        )
    for operation in ["-F", "-X"]:
        subprocess.run(["iptables", "-w", operation, chain], capture_output=True)
    for path, _, _ in service_files(resource, bundle):
        Path(path).unlink(missing_ok=True)
    with (ROOT / "openvpn-forwarding.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        (ROOT / ("openvpn-owner-" + own)).unlink(missing_ok=True)
        baseline = ROOT / "openvpn-forwarding.before"
        if not list(ROOT.glob("openvpn-owner-*")) and baseline.exists():
            previous = baseline.read_text().strip()
            if previous not in {"0", "1"}:
                raise ValueError("Invalid saved forwarding state")
            subprocess.run(
                ["sysctl", "-w", "net.ipv4.ip_forward=" + previous],
                check=True,
                capture_output=True,
            )
            baseline.unlink()
    subprocess.run(["systemctl", "daemon-reload"], check=True)
