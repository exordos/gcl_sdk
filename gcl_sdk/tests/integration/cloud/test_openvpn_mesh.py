# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0

"""Root-only, isolated three-site dataplane check; no Core API is running."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

import pytest

from gcl_sdk.cloud import openvpn

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0 or not shutil.which("openvpn"),
    reason="requires root, iproute2, iptables and OpenVPN",
)


def run(*command, check=True):
    return subprocess.run(list(command), check=check, capture_output=True, text=True)


@pytest.mark.parametrize(
    "roles", [("auto", "auto", "auto"), ("client", "auto", "server")]
)
def test_full_mesh_bootstraps_without_core_and_survives_one_site_loss(
    tmp_path, monkeypatch, roles
):
    monkeypatch.setattr(openvpn, "ROOT", tmp_path / "network-links")
    tag = uuid.uuid4().hex[:6]
    bridge = "cb" + tag
    nat_gateway = "cn" + tag if roles[0] == "client" else None
    nat_host = "cnw" + tag
    gateways = ["cg" + tag + str(i) for i in range(3)]
    guests = ["cv" + tag + str(i) for i in range(3)]
    ids = sorted([uuid.uuid4() for _ in range(3)])
    ca, nodes = openvpn.certificate("integration-link"), {}
    processes, logs, bundles = [], [], {}
    try:
        run("ip", "link", "add", bridge, "type", "bridge")
        run("ip", "link", "set", bridge, "up")
        if nat_gateway:
            run("ip", "netns", "add", nat_gateway)
            run(
                "ip",
                "link",
                "add",
                nat_host,
                "type",
                "veth",
                "peer",
                "name",
                "wan0",
                "netns",
                nat_gateway,
            )
            run("ip", "link", "set", nat_host, "master", bridge)
            run("ip", "link", "set", nat_host, "up")
            run(
                "ip",
                "-n",
                nat_gateway,
                "addr",
                "add",
                "198.18.230.254/24",
                "dev",
                "wan0",
            )
            run("ip", "-n", nat_gateway, "link", "set", "wan0", "up")
            run("ip", "-n", nat_gateway, "link", "set", "lo", "up")
            run(
                "ip",
                "netns",
                "exec",
                nat_gateway,
                "sysctl",
                "-qw",
                "net.ipv4.ip_forward=1",
            )
            run("ip", "netns", "exec", nat_gateway, "iptables", "-P", "FORWARD", "DROP")
            run(
                "ip",
                "netns",
                "exec",
                nat_gateway,
                "iptables",
                "-A",
                "FORWARD",
                "-m",
                "conntrack",
                "--ctstate",
                "RELATED,ESTABLISHED",
                "-j",
                "ACCEPT",
            )
            run(
                "ip",
                "netns",
                "exec",
                nat_gateway,
                "iptables",
                "-A",
                "FORWARD",
                "-s",
                "172.29.0.0/24",
                "-o",
                "wan0",
                "-j",
                "ACCEPT",
            )
            run(
                "ip",
                "netns",
                "exec",
                nat_gateway,
                "iptables",
                "-t",
                "nat",
                "-A",
                "POSTROUTING",
                "-s",
                "172.29.0.0/24",
                "-o",
                "wan0",
                "-j",
                "MASQUERADE",
            )
        for index, (gateway, guest, own) in enumerate(zip(gateways, guests, ids)):
            run("ip", "netns", "add", gateway)
            run("ip", "netns", "add", guest)
            wan_host, wan = "cw" + tag + str(index), "eth0"
            run(
                "ip",
                "link",
                "add",
                wan_host,
                "type",
                "veth",
                "peer",
                "name",
                wan,
                "netns",
                gateway,
            )
            if nat_gateway and index == 0:
                run("ip", "link", "set", wan_host, "netns", nat_gateway)
                run(
                    "ip",
                    "-n",
                    nat_gateway,
                    "addr",
                    "add",
                    "172.29.0.1/24",
                    "dev",
                    wan_host,
                )
                run("ip", "-n", nat_gateway, "link", "set", wan_host, "up")
            else:
                run("ip", "link", "set", wan_host, "master", bridge)
                run("ip", "link", "set", wan_host, "up")
            run(
                "ip",
                "-n",
                gateway,
                "addr",
                "add",
                "172.29.0.2/24"
                if nat_gateway and index == 0
                else f"198.18.230.{index + 11}/24",
                "dev",
                wan,
            )
            run("ip", "-n", gateway, "link", "set", wan, "up")
            if nat_gateway and index == 0:
                run("ip", "-n", gateway, "route", "add", "default", "via", "172.29.0.1")
            run(
                "ip",
                "link",
                "add",
                "lan0",
                "netns",
                gateway,
                "type",
                "veth",
                "peer",
                "name",
                "eth0",
                "netns",
                guest,
            )
            for ns, address, interface in [
                (gateway, f"10.250.{index}.1/24", "lan0"),
                (guest, f"10.250.{index}.10/24", "eth0"),
            ]:
                run("ip", "-n", ns, "addr", "add", address, "dev", interface)
                run("ip", "-n", ns, "link", "set", interface, "up")
                run("ip", "-n", ns, "link", "set", "lo", "up")
            run(
                "ip", "-n", guest, "route", "add", "default", "via", f"10.250.{index}.1"
            )
            run(
                "ip",
                "netns",
                "exec",
                gateway,
                "iptables",
                "-A",
                "OUTPUT",
                "-p",
                "udp",
                "-j",
                "REJECT",
            )
            if roles[index] == "client":
                run(
                    "ip",
                    "netns",
                    "exec",
                    gateway,
                    "iptables",
                    "-A",
                    "INPUT",
                    "-p",
                    "tcp",
                    "--dport",
                    "443",
                    "-m",
                    "conntrack",
                    "--ctstate",
                    "NEW",
                    "-j",
                    "REJECT",
                )
            nodes[own] = {
                "role": roles[index],
                "cidr": f"10.250.{index}.0/24",
                "endpoint": None
                if roles[index] == "client"
                else f"198.18.230.{index + 11}",
                "tunnel_cidr": f"100.127.{240 + index}.0/24",
                "server": openvpn.certificate(own.hex + "-server", ca, server=True),
                "client": openvpn.certificate(own.hex + "-client", ca),
                "tls_server_key": openvpn.tls_key(),
                "tls_clients": {},
            }
        for own in ids:
            for peer in ids:
                if peer != own and openvpn.serves(
                    peer, own, nodes[peer]["role"], nodes[own]["role"]
                ):
                    nodes[own]["tls_clients"][peer.hex] = openvpn.tls_key(
                        nodes[peer]["tls_server_key"]
                    )
        for gateway, own in zip(gateways, ids):
            peers = [
                dict(node, id=str(peer)) for peer, node in nodes.items() if peer != own
            ]
            bundle = bundles[own] = openvpn.render(own, nodes[own], peers, ca["cert"])
            root, runtime = openvpn.paths(own)
            runtime.mkdir(parents=True, mode=0o700)
            for path, content, mode in openvpn.service_files(own, bundle):
                target = Path(path)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                target.write_text(content)
                target.chmod(int(mode, 8))
            script = "/usr/local/sbin/exordos-openvpn-" + own.hex
            run("ip", "netns", "exec", gateway, script)
            for service in bundle["services"]:
                handle = (tmp_path / (gateway + "-" + service + ".log")).open("w+")
                logs.append(handle)
                process = subprocess.Popen(
                    [
                        "ip",
                        "netns",
                        "exec",
                        gateway,
                        "openvpn",
                        "--cd",
                        str(root),
                        "--config",
                        service + ".conf",
                    ],
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                )
                processes.append((gateway, process))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if all(openvpn.connected(own, bundles[own]) for own in ids):
                break
            if any(p.poll() is not None for _, p in processes):
                for log in logs:
                    log.flush()
                    log.seek(0)
                    print(log.read()[-4000:])
                pytest.fail("OpenVPN exited during bootstrap")
            time.sleep(0.25)
        else:
            for log in logs:
                log.flush()
                log.seek(0)
                print(log.read()[-4000:])
            pytest.fail("Full mesh did not connect")
        for index, guest in enumerate(guests):
            for remote in range(3):
                if index != remote:
                    run(
                        "ip",
                        "netns",
                        "exec",
                        guest,
                        "ping",
                        "-c",
                        "2",
                        "-W",
                        "2",
                        f"10.250.{remote}.10",
                    )
        # A cloud-side HTTP proxy reaches a backend behind an outbound-only
        # gateway. The backend sees the proxy's private address, without SNAT.
        handler = """from http.server import BaseHTTPRequestHandler
from socketserver import TCPServer
import urllib.request
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if {proxy}:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({{}}))
            body = opener.open('http://10.250.0.10:18081/', timeout=1).read()
        else:
            body = self.client_address[0].encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)
TCPServer(('{address}', {port}), Handler).serve_forever()
"""
        for index, port, proxy in [(0, 18081, False), (2, 18080, True)]:
            handle = (tmp_path / (guests[index] + "-http.log")).open("w+")
            logs.append(handle)
            process = subprocess.Popen(
                [
                    "ip",
                    "netns",
                    "exec",
                    guests[index],
                    sys.executable,
                    "-c",
                    handler.format(
                        proxy=proxy, address=f"10.250.{index}.10", port=port
                    ),
                ],
                stdout=handle,
                stderr=subprocess.STDOUT,
            )
            processes.append((gateways[index], process))
        request = (
            "import urllib.request; "
            "opener=urllib.request.build_opener(urllib.request.ProxyHandler({})); "
            "print(opener.open('http://10.250.2.10:18080/', timeout=1).read().decode())"
        )
        for _ in range(20):
            response = run(
                "ip",
                "netns",
                "exec",
                guests[1],
                sys.executable,
                "-c",
                request,
                check=False,
            )
            if response.returncode == 0:
                assert response.stdout.strip() == "10.250.2.10"
                break
            time.sleep(0.1)
        else:
            for log in logs:
                log.flush()
                log.seek(0)
                print(log.read()[-4000:])
            pytest.fail(
                "Cloud proxy could not reach the on-premises backend: "
                + response.stderr
            )
        # Routed workloads can communicate, but a tunnel must not expose the
        # gateway host's own management listeners to another site.
        listener = subprocess.Popen(
            [
                "ip",
                "netns",
                "exec",
                gateways[1],
                sys.executable,
                "-m",
                "http.server",
                "18443",
                "--bind",
                "10.250.1.1",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        processes.append((gateways[1], listener))
        probe = (
            "import socket; socket.create_connection(('10.250.1.1',18443),1).close()"
        )
        for _ in range(30):
            if (
                run(
                    "ip",
                    "netns",
                    "exec",
                    guests[1],
                    sys.executable,
                    "-c",
                    probe,
                    check=False,
                ).returncode
                == 0
            ):
                break
            time.sleep(0.1)
        else:
            pytest.fail("Gateway test listener did not start")
        assert (
            run(
                "ip",
                "netns",
                "exec",
                guests[2],
                sys.executable,
                "-c",
                probe,
                check=False,
            ).returncode
            != 0
        )
        # A missing direct B-C connection would route via A or lose connectivity.
        for gateway, process in processes:
            if gateway == gateways[0]:
                process.terminate()
                process.wait(timeout=5)
        run("ip", "-n", gateways[0], "link", "set", "eth0", "down")
        run(
            "ip",
            "netns",
            "exec",
            guests[1],
            "ping",
            "-c",
            "3",
            "-W",
            "2",
            "10.250.2.10",
        )
        run(
            "ip",
            "netns",
            "exec",
            guests[2],
            "ping",
            "-c",
            "3",
            "-W",
            "2",
            "10.250.1.10",
        )
        for gateway in gateways[1:]:
            nat = run(
                "ip", "netns", "exec", gateway, "iptables", "-t", "nat", "-S"
            ).stdout
            assert "SNAT" not in nat and "MASQUERADE" not in nat
    finally:
        for _, process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for log in logs:
            log.close()
        for ns in [*guests, *gateways]:
            run("ip", "netns", "del", ns, check=False)
        if nat_gateway:
            run("ip", "netns", "del", nat_gateway, check=False)
            run("ip", "link", "del", nat_host, check=False)
        run("ip", "link", "del", bridge, check=False)
        for own, bundle in bundles.items():
            for path, _, _ in openvpn.service_files(own, bundle):
                Path(path).unlink(missing_ok=True)
            root, runtime = openvpn.paths(own)
            shutil.rmtree(root, ignore_errors=True)
            shutil.rmtree(runtime, ignore_errors=True)
            Path("/usr/local/sbin/exordos-openvpn-" + own.hex).unlink(missing_ok=True)
