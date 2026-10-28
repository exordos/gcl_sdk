# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0

import ipaddress
from unittest import mock
import uuid

import pytest
import yaml

from gcl_sdk.cloud import bootstrap
from gcl_sdk.cloud import openvpn


@pytest.fixture
def material():
    ca = openvpn.certificate("test-link")
    ids = sorted([uuid.uuid4() for _ in range(3)])
    nodes = {}
    for index, own in enumerate(ids):
        nodes[own] = {
            "cidr": f"10.250.{index}.0/24",
            "gateway_ip": f"10.250.{index}.254",
            "tunnel_cidr": f"100.127.{index}.0/24",
            "server": openvpn.certificate(own.hex + "-server", ca, server=True),
            "client": openvpn.certificate(own.hex + "-client", ca),
            "tls_server_key": openvpn.tls_key(),
            "tls_clients": {},
            "endpoint": f"192.0.2.{index + 1}",
        }
    for own in ids:
        for remote in ids:
            if remote < own:
                nodes[own]["tls_clients"][remote.hex] = openvpn.tls_key(
                    nodes[remote]["tls_server_key"]
                )
    return ca, nodes


def bundles(material):
    ca, nodes = material
    return {
        own: openvpn.render(
            own,
            node,
            [
                dict(other, id=str(remote))
                for remote, other in nodes.items()
                if remote != own
            ],
            ca["cert"],
        )
        for own, node in nodes.items()
    }


@pytest.mark.parametrize(
    ("own_role", "peer_role", "serves"),
    [
        ("auto", "auto", True),
        ("server", "client", True),
        ("client", "server", False),
        ("server", "auto", True),
        ("auto", "server", False),
        ("client", "auto", False),
        ("auto", "client", True),
    ],
)
def test_explicit_roles_override_uuid_order(own_role, peer_role, serves):
    first, second = uuid.UUID(int=1), uuid.UUID(int=2)
    assert openvpn.serves(first, second, own_role, peer_role) is serves
    assert openvpn.serves(second, first, peer_role, own_role) is not serves


@pytest.mark.parametrize(
    ("own_role", "peer_role"),
    [("client", "client"), ("server", "server"), ("invalid", "auto"), ([], "auto")],
)
def test_incompatible_roles_are_rejected(own_role, peer_role):
    with pytest.raises(ValueError):
        openvpn.serves(uuid.uuid4(), uuid.uuid4(), own_role, peer_role)


def test_outbound_only_endpoint_has_no_listener_or_advertised_address(material):
    _, nodes = material
    first, middle, last = sorted(nodes)
    for own, role in zip((first, middle, last), ("client", "auto", "server")):
        nodes[own]["role"] = role
    nodes[first]["endpoint"] = None
    for client, server in ((first, middle), (first, last), (middle, last)):
        nodes[client]["tls_clients"][server.hex] = openvpn.tls_key(
            nodes[server]["tls_server_key"]
        )
    configs = bundles(material)
    assert "server" not in configs[first]["services"]
    assert configs[last]["services"] == ["server"]
    for own, bundle in configs.items():
        assert len(bundle["peers"]) == 2
        for peer, state in bundle["peers"].items():
            other = configs[uuid.UUID(peer)]["peers"][own.hex]
            assert ("client" in state) != ("client" in other)
    first_client = configs[first]["files"]["client-" + last.hex + ".conf"]
    assert f"remote {nodes[last]['endpoint']} 443" in first_client
    assert (
        "iroute 10.250.0.0 255.255.255.0"
        in configs[last]["files"]["ccd/" + first.hex + "-client"]
    )


def test_three_sites_have_one_direct_connection_for_each_pair(material):
    configs = bundles(material)
    assert (
        sum(
            name.startswith("client-")
            for bundle in configs.values()
            for name in bundle["services"]
        )
        == 3
    )
    for own, bundle in configs.items():
        assert len(bundle["peers"]) == 2
        for peer, state in bundle["peers"].items():
            other = configs[uuid.UUID(peer)]["peers"][own.hex]
            assert ("client" in state) != ("client" in other)
        all_files = str(bundle["files"])
        assert "MASQUERADE" not in all_files and "redirect-gateway" not in all_files
        assert material[0]["key"] not in all_files
        assert all(
            "tls-crypt-v2" in bundle["files"][name + ".conf"]
            for name in bundle["services"]
        )


def test_server_has_kernel_and_per_client_routes_and_exact_certificate_identity(
    material,
):
    configs = bundles(material)
    first, last = min(configs), max(configs)
    remote = ipaddress.IPv4Network(material[1][last]["cidr"])
    server = configs[first]["files"]["server.conf"]
    ccd = configs[first]["files"]["ccd/" + last.hex + "-client"]
    assert f"route {remote.network_address} {remote.netmask}" in server
    assert f"iroute {remote.network_address} {remote.netmask}" in ccd
    assert "ccd-exclusive" in server
    client = configs[last]["files"]["client-" + first.hex + ".conf"]
    assert f"verify-x509-name {first.hex}-server name" in client
    assert "remote-cert-tls server" in client


def test_bootstrap_starts_transport_before_agent_and_has_no_core_dependency(material):
    own, bundle = next(iter(bundles(material).items()))
    data = yaml.safe_load(
        openvpn.bootstrap_user_data(
            own,
            bundle,
            bootstrap.agent_user_data(
                own,
                "agent-key",
                "http://unreachable.invalid:11011/",
                "http://unreachable.invalid:11012/",
                "gcl_sdk",
                caps_drivers="OpenVPNCapabilityDriver",
            ),
        )
    )
    assert "openvpn" in data["packages"] and "wireguard-tools" not in data["packages"]
    startup = data["runcmd"]
    vpn_index = next(
        i
        for i, command in enumerate(startup)
        if any("exordos-openvpn" in item for item in command)
    )
    agent_index = next(
        i for i, command in enumerate(startup) if "exordos-agent.service" in command
    )
    assert vpn_index < agent_index
    early = data["bootcmd"][0][-1]
    assert early.index("exordos-openvpn-") < early.index("exordos-agent.service")
    vpn_files = [
        f
        for f in data["write_files"]
        if "exordos-openvpn" in f["path"] or "network-links" in f["path"]
    ]
    assert all("unreachable.invalid" not in f["content"] for f in vpn_files)
    assert all(
        f["permissions"] == "0600"
        for f in vpn_files
        if f["path"].endswith((".key", ".json"))
    )


def test_unchanged_bootstrap_configuration_does_not_restart_tunnels(material):
    own, bundle = next(iter(bundles(material).items()))
    with (
        mock.patch.object(openvpn, "read_manifest", return_value=bundle),
        mock.patch.object(openvpn, "read_bundle", return_value=bundle),
        mock.patch.object(
            openvpn.subprocess, "run", return_value=mock.Mock(returncode=0)
        ) as run,
    ):
        openvpn.install(own, bundle)
    assert all(call.args[0][1] == "is-active" for call in run.call_args_list)


def test_invalid_managed_filename_cannot_write_outside_resource_directory():
    with pytest.raises(ValueError, match="filename"):
        openvpn.service_files(
            uuid.uuid4(),
            {
                "files": {"../../escape.key": "secret"},
                "local_cidrs": [],
                "remote_cidrs": [],
            },
        )


def test_agent_can_restore_metadata_before_reading_configuration_from_disk(material):
    from gcl_sdk.agents.universal.drivers.openvpn import OpenVPNLink

    own, bundle = next(iter(bundles(material).items()))
    model = OpenVPNLink.restore_from_simple_view(uuid=str(own))
    with (
        mock.patch.object(openvpn, "read_bundle", return_value=bundle),
        mock.patch.object(openvpn, "connected", return_value=True),
    ):
        model.restore_from_dp()
    assert model.configuration == bundle
    assert model.status == "ACTIVE"


def test_renewed_ca_preserves_trust_during_staggered_leaf_updates(tmp_path):
    import subprocess

    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    ca = openvpn.certificate("renewal-ca")
    previous = openvpn.certificate("gateway-server", ca, server=True)
    renewed = openvpn.certificate("renewal-ca", private_key=ca["key"])
    current = openvpn.certificate("gateway-server", renewed, server=True)

    def public(pem):
        return (
            x509.load_pem_x509_certificate(pem.encode())
            .public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )

    assert public(ca["cert"]) == public(renewed["cert"])
    assert previous["key"] != current["key"]
    for trust in [ca, renewed]:
        (tmp_path / "ca.pem").write_text(trust["cert"])
        for leaf in [previous, current]:
            (tmp_path / "leaf.pem").write_text(leaf["cert"])
            result = subprocess.run(
                [
                    "openssl",
                    "verify",
                    "-CAfile",
                    str(tmp_path / "ca.pem"),
                    str(tmp_path / "leaf.pem"),
                ],
                capture_output=True,
            )
            assert result.returncode == 0
    assert not openvpn.expiring(current["cert"])
    assert openvpn.expiring(current["cert"], within_days=366)


def test_missing_core_openvpn_binary_reports_runtime_prerequisite(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("openvpn")

    monkeypatch.setattr(openvpn.subprocess, "run", missing)
    with pytest.raises(RuntimeError, match="Install OpenVPN 2.6+"):
        openvpn.tls_key()


def test_removed_peer_units_are_disabled_before_their_files_are_deleted(
    tmp_path, monkeypatch
):
    old = {"services": ["old-peer", "retained-peer"]}
    current = {"services": ["retained-peer"]}
    stale = tmp_path / "old-peer.service"
    stale.write_text("old unit")
    monkeypatch.setattr(openvpn, "read_manifest", lambda resource: old)
    monkeypatch.setattr(openvpn, "read_bundle", lambda resource: old)
    monkeypatch.setattr(openvpn, "units", lambda resource, bundle: bundle["services"])
    monkeypatch.setattr(
        openvpn,
        "service_files",
        lambda resource, bundle: [
            (str(tmp_path / (name + ".service")), "unit", "0600")
            for name in bundle["services"]
        ],
    )

    def run(command, **kwargs):
        if command[:2] == ["systemctl", "disable"]:
            assert command == ["systemctl", "disable", "old-peer"]
            assert stale.exists()
        return mock.Mock(returncode=0)

    with mock.patch.object(openvpn.subprocess, "run", side_effect=run) as execute:
        openvpn.install(uuid.uuid4(), current)
    assert not stale.exists()
    assert ["systemctl", "disable", "old-peer"] in [
        c.args[0] for c in execute.call_args_list
    ]


def test_uninstall_restores_forwarding_only_after_the_last_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(openvpn, "ROOT", tmp_path)
    first, second = uuid.uuid4(), uuid.uuid4()
    for own in [first, second]:
        (tmp_path / ("openvpn-owner-" + own.hex)).touch()
    baseline = tmp_path / "openvpn-forwarding.before"
    baseline.write_text("0\n")
    monkeypatch.setattr(openvpn, "read_manifest", lambda resource: {"services": []})
    monkeypatch.setattr(openvpn, "service_files", lambda *args: [])
    with mock.patch.object(
        openvpn.subprocess, "run", return_value=mock.Mock(returncode=0)
    ) as execute:
        openvpn.uninstall(first)
        assert baseline.exists()
        assert not any(call.args[0][0] == "sysctl" for call in execute.call_args_list)
        openvpn.uninstall(second)
        assert ["sysctl", "-w", "net.ipv4.ip_forward=0"] in [
            c.args[0] for c in execute.call_args_list
        ]
        assert [
            "iptables",
            "-w",
            "-D",
            "INPUT",
            "-i",
            "cl" + second.hex[:8] + "+",
            "-j",
            "DROP",
        ] in [c.args[0] for c in execute.call_args_list]
    assert not baseline.exists()
    assert not list(tmp_path.glob("openvpn-owner-*"))


def test_gateway_agent_routes_use_private_source_and_allow_only_reply_input(material):
    for own, bundle in bundles(material).items():
        for service in bundle["services"]:
            assert "route-up" in bundle["files"][service + ".conf"]
            hook = bundle["files"]["route-" + service + ".sh"]
            assert "src " + material[1][own]["gateway_ip"] in hook
        files = openvpn.service_files(own, bundle)
        script = next(
            body for path, body, mode in files if path.startswith("/usr/local/sbin")
        )
        assert "--ctstate ESTABLISHED,RELATED -j ACCEPT" in script
        assert "-j DROP" in script
