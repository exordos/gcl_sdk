# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0

"""Flat reachability must preserve host, transport and private isolation."""

import json
import subprocess
import sys
from unittest import mock
import uuid

import pytest

from gcl_sdk.agents.universal.drivers.evpn import base
from gcl_sdk.agents.universal.drivers.evpn import hostns
from gcl_sdk.agents.universal.drivers.evpn import sh
from gcl_sdk.tests.integration.sdn import harness

VNI = 16117


@pytest.fixture
def flat(fabric):
    tag = uuid.uuid4().hex[:6]
    ns, link, peer = "flat" + tag, "fr" + tag, "fp" + tag
    run = harness._run
    run(["ip", "netns", "add", ns])
    try:
        run(["ip", "link", "add", link, "type", "veth", "peer", "name", peer])
        run(["ip", "link", "set", peer, "netns", ns])
        run(["ip", "addr", "add", "10.66.0.1/24", "dev", link])
        run(["ip", "link", "set", link, "up"])
        for address in (
            "10.66.0.10/24",
            "10.66.0.11/24",
            "10.67.0.10/32",
            "198.51.100.10/32",
        ):
            run(["ip", "-n", ns, "addr", "add", address, "dev", peer])
        run(["ip", "-n", ns, "link", "set", peer, "up"])
        run(["ip", "-n", ns, "link", "set", "lo", "up"])
        for cidr in ("10.67.0.0/24", "198.51.100.10/32"):
            run(["ip", "route", "add", cidr, "via", "10.66.0.10"])
        run(["ip", "-n", ns, "route", "add", "default", "via", "10.66.0.1"])
        for address in ("10.66.0.10", "10.66.0.11", "10.67.0.10", "198.51.100.10"):
            run(["ping", "-c", "1", "-W", "1", address])
        yield ns
    finally:
        run(["ip", "netns", "del", ns], check=False)
        run(["ip", "link", "del", link], check=False)


def _policy(flat_prefixes, protected_prefixes):
    sh.write_atomic(
        base.EGRESS_POLICY_FILE,
        json.dumps(
            {
                "flat_prefixes": flat_prefixes,
                "protected_prefixes": protected_prefixes,
            }
        ),
    )
    hostns.ensure_fabric_guard(VNI)


def test_flat_is_reachable_but_infrastructure_and_other_private_networks_are_not(
    fabric, flat
):
    guest = fabric.add_guest("fe", vlan=47, ip="10.42.17.7/24")
    fabric.apply_egress(guest, VNI, "10.42.17.0/24", "10.42.17.1")
    assert not guest.ping("10.66.0.10", count=1, wait=1)

    _policy(["10.66.0.0/24"], ["10.66.0.11/32", "198.51.100.10/32"])
    assert guest.ping("10.66.0.10"), "the ordinary flat endpoint must be reachable"
    with mock.patch.object(sh, "ns_run", wraps=sh.ns_run) as run:
        hostns.ensure_fabric_guard(VNI)
    assert not any(
        base.IPTABLES_RESTORE_BIN in call.args for call in run.call_args_list
    ), "an unchanged policy must settle without rewriting the chain"
    for address in (
        "10.66.0.1",
        "10.66.0.11",
        "10.67.0.10",
        "198.51.100.10",
        hostns.egress_transfer(VNI)[1],
    ):
        assert not guest.ping(address, count=1, wait=1), address

    _policy([], ["10.66.0.11/32", "198.51.100.10/32"])
    assert not guest.ping("10.66.0.10", count=1, wait=1), (
        "withdrawn flat access must close"
    )
    _policy(["10.66.0.0/24"], ["10.66.0.11/32", "198.51.100.10/32"])
    assert guest.ping("10.66.0.10"), "reconciliation must restore flat access"

    ns = hostns.vni_ns(VNI)
    subprocess.run(
        ["ip", "netns", "exec", ns, "iptables", "-F", "EVPN-FLAT"], check=True
    )
    hostns.ensure_fabric_guard(VNI)
    assert guest.ping("10.66.0.10"), "a flushed chain must be repaired"
    assert not guest.ping("10.66.0.11", count=1, wait=1)


def test_flat_tcp_service_is_reachable_while_fabric_ports_stay_closed(fabric, flat):
    guest = fabric.add_guest("ft", vlan=48, ip="10.42.17.8/24")
    fabric.apply_egress(guest, VNI, "10.42.17.0/24", "10.42.17.1")
    _policy(["10.66.0.0/24"], [])
    server = """import socket, threading
def serve(sock):
    while True:
        connection, peer = sock.accept()
        connection.sendall(peer[0].encode())
        connection.close()
for port in (11011, 179, 50051):
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    sock.listen(8)
    threading.Thread(target=serve, args=(sock,), daemon=True).start()
print("ready", flush=True)
threading.Event().wait()
"""
    process = subprocess.Popen(
        ["ip", "netns", "exec", flat, sys.executable, "-u", "-c", server],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "ready"
        client = """import socket, sys
try:
    with socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=1) as connection:
        print(connection.recv(100).decode())
except OSError:
    print("blocked")
"""
        for port in (11011, 179, 50051):
            assert (
                harness._run(
                    [sys.executable, "-c", client, "10.66.0.10", str(port)]
                ).strip()
                == "10.66.0.1"
            )
            actual = guest.exec(
                sys.executable, "-c", client, "10.66.0.10", str(port)
            ).strip()
            assert actual == ("10.66.0.1" if port == 11011 else "blocked")
        fabric.apply_security_group(guest, [{"direction": "egress", "proto": "none"}])
        assert not guest.tcp_connect("10.66.0.10", 11011, wait=1)
        fabric.apply_security_group(
            guest, [{"direction": "egress", "proto": "tcp", "port": 11011}]
        )
        assert guest.tcp_connect("10.66.0.10", 11011)
        fabric.apply_security_group(guest, [{"direction": "egress", "proto": "none"}])
        assert not guest.tcp_connect("10.66.0.10", 11011, wait=1)
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.mark.parametrize("protect", [False, True])
def test_policy_withdrawal_blocks_an_already_established_tcp_session(fabric, flat, protect):
    guest = fabric.add_guest("fc", vlan=49, ip="10.42.17.9/24")
    fabric.apply_egress(guest, VNI, "10.42.17.0/24", "10.42.17.1")
    _policy(["10.66.0.0/24"], [])
    server_code = """import socket
with socket.socket() as listener:
    listener.bind(('0.0.0.0', 11011))
    listener.listen(1)
    print('ready', flush=True)
    connection, _ = listener.accept()
    with connection:
        while data := connection.recv(1024):
            connection.sendall(data)
"""
    client_code = """import socket, sys
with socket.create_connection(('10.66.0.10', 11011), timeout=2) as connection:
    for line in sys.stdin:
        try:
            connection.sendall(line.encode())
            print(connection.recv(1024).decode().strip(), flush=True)
        except OSError:
            print('blocked', flush=True)
"""
    server = subprocess.Popen(["ip", "netns", "exec", flat, sys.executable, "-u", "-c", server_code], stdout=subprocess.PIPE, text=True)
    client = None
    try:
        assert server.stdout.readline().strip() == "ready"
        client = subprocess.Popen(["ip", "netns", "exec", guest.ns, sys.executable, "-u", "-c", client_code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        client.stdin.write("before\n")
        client.stdin.flush()
        assert client.stdout.readline().strip() == "before"
        _policy(["10.66.0.0/24"] if protect else [], ["10.66.0.10/32"] if protect else [])
        client.stdin.write("after\n")
        client.stdin.flush()
        assert client.stdout.readline().strip() == "blocked"
    finally:
        for process in (client, server):
            if process is not None:
                process.terminate()
                process.wait(timeout=5)
