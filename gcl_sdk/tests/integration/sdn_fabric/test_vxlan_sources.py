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

"""Automatic VTEP source admission on the real EVPN fabric."""

import re
import struct
import subprocess
from pathlib import Path

import pytest

from gcl_sdk.agents.universal.drivers import evpn_nf
from gcl_sdk.tests.integration.sdn import harness as nf_harness
from gcl_sdk.tests.integration.sdn_fabric import two_host
from gcl_sdk.tests.integration.sdn_fabric.test_two_host import _dhcp
from gcl_sdk.tests.integration.sdn_fabric.test_two_host import _reachable

VNI = 17000
PAYLOAD = b"vxlan-source-check"


def _sources(fab, host):
    rules = [
        line
        for line in fab.flows(host).splitlines()
        if "table=0," in line and "in_port=65278" in line
    ]
    assert all("tun_src=" in line for line in rules), rules
    sources = {re.search(r"tun_src=([0-9.]+)", line).group(1) for line in rules}
    assert len(rules) == len(sources), rules
    return sources


def _wait_sources(fab, host, sources):
    return nf_harness.wait_until(
        lambda: _sources(fab, host),
        lambda current: current == sources,
        60,
        lambda current: "expected VTEPs %s, got %s" % (sources, current),
    )


def _probe(fab, target, sender_ns, vni=VNI):
    receiver = subprocess.Popen(
        [
            "ip",
            "netns",
            "exec",
            target["ns"],
            two_host._sys_python(),
            "-c",
            "import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); "
            "s.bind(('0.0.0.0',42424)); s.settimeout(2); print('ready',flush=True); "
            "\ntry: print(s.recv(4096).hex(),flush=True)"
            "\nexcept socket.timeout: print('timeout',flush=True)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert receiver.stdout.readline().strip() == "ready"
        packet = evpn_nf._ipv4_udp(
            "10.170.0.6",
            target["ip"],
            42425,
            42424,
            PAYLOAD,
        )
        mac = bytes.fromhex(target["spec"]["mac"].replace(":", ""))
        frame = evpn_nf._eth(mac, b"\x02\x00\x00\x00\x00\x01", 0x0800, packet)
        vxlan = struct.pack("!BBH3sB", 0x88, 0, 0, vni.to_bytes(3, "big"), 0)
        nf_harness._run(
            [
                "ip",
                "netns",
                "exec",
                sender_ns,
                two_host._sys_python(),
                "-c",
                "import socket,sys; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); "
                "s.sendto(bytes.fromhex(sys.argv[1]),(sys.argv[2],4789))",
                (vxlan + frame).hex(),
                target["host"].underlay_ip,
            ]
        )
        out, err = receiver.communicate(timeout=5)
        assert receiver.returncode == 0, err
        return out.strip() == PAYLOAD.hex()
    finally:
        if receiver.poll() is None:
            receiver.kill()
            receiver.wait(timeout=5)


@pytest.mark.parametrize("legacy_snapshot", [False, True])
def test_unknown_source_stays_blocked_across_rr_loss_and_restart(
    two_host_fabric, legacy_snapshot
):
    fab = two_host_fabric
    ha, hb = fab.hosts
    ga = fab.add_guest(ha, "sa", "10.170.0.5", VNI, _dhcp())
    gb = fab.add_guest(hb, "sb", "10.170.0.6", VNI, _dhcp())
    fab.wait_rr_peers(2)
    fab.wait_host_rib(ha, [gb["ip"]])
    fab.wait_host_rib(hb, [ga["ip"]])
    _wait_sources(fab, ha, {hb.underlay_ip})
    _wait_sources(fab, hb, {ha.underlay_ip})
    assert _reachable(fab, ga, gb["ip"])
    assert _reachable(fab, gb, ga["ip"])
    assert _probe(fab, ga, hb.ns)
    assert not _probe(fab, ga, fab._rr_ns)
    assert not _probe(fab, ga, hb.ns, vni=VNI + 1)

    fab.kill_rr()
    fab.wait_host_rib_without(ha, [gb["ip"]])
    fab.wait_host_rib_without(hb, [ga["ip"]])
    restarted = []
    for host in fab.hosts:
        name = "connector-%s" % host.name
        proc = next(proc for label, proc, _ in fab._daemons.procs if label == name)
        proc.kill()
        proc.wait(timeout=10)
        if legacy_snapshot:
            path = Path(host.run) / "evpn_tmp_flow_file"
            flows = path.read_text().splitlines()
            sources = [
                re.search(r"tun_src=([0-9.]+)", flow).group(1)
                for flow in flows
                if "table=0," in flow and "in_port=65278" in flow
            ]
            assert sources
            old_flows = []
            for flow in flows:
                if "table=0," in flow and "in_port=65278" in flow:
                    continue
                if "table=3," in flow:
                    if "priority=0" in flow:
                        continue
                    match, action = flow.split(" ", 1)
                    for source in sources:
                        old_flows.append(
                            match.replace("table=3,", "table=0,")
                            + ",tun_src="
                            + source
                            + " "
                            + action
                        )
                else:
                    old_flows.append(flow)
            path.write_text("\n".join(old_flows) + "\n")
        nf_harness._run(
            [
                "ip",
                "netns",
                "exec",
                host.ns,
                "env",
                "OVS_RUNDIR=%s" % host.run,
                "ovs-ofctl",
                "-O",
                "OpenFlow13",
                "del-flows",
                "evpn",
            ]
        )
        assert "tun_src=" not in fab.flows(host)
        restarted.append(fab._daemons.spawn(name + "-restarted", proc.args))
    assert _reachable(fab, ga, gb["ip"])
    assert _reachable(fab, gb, ga["ip"])
    _wait_sources(fab, ha, {hb.underlay_ip})
    _wait_sources(fab, hb, {ha.underlay_ip})
    assert all(proc.poll() is None for proc in restarted)
    assert _probe(fab, ga, hb.ns)
    assert not _probe(fab, ga, fab._rr_ns)


def test_connector_default_preserves_unrestricted_sources(two_host_fabric):
    fab = two_host_fabric
    ha, hb = fab.hosts
    ga = fab.add_guest(ha, "da", "10.170.0.5", VNI, _dhcp())
    gb = fab.add_guest(hb, "db", "10.170.0.6", VNI, _dhcp())
    fab.wait_rr_peers(2)
    fab.wait_host_rib(ha, [gb["ip"]])
    _wait_sources(fab, ha, {hb.underlay_ip})
    restarted = []
    for host in fab.hosts:
        path = Path(host.connector_conf)
        conf = path.read_text()
        option = "restrict_tunnel_sources = True\n"
        assert option in conf
        path.write_text(conf.replace(option, ""))
        name = "connector-%s" % host.name
        proc = next(proc for label, proc, _ in fab._daemons.procs if label == name)
        proc.kill()
        proc.wait(timeout=10)
        restarted.append(fab._daemons.spawn(name + "-default", proc.args))
    for host in fab.hosts:
        nf_harness.wait_until(
            lambda: fab.flows(host),
            lambda dump: any(
                "table=0," in line
                and "in_port=65278" in line
                and "tun_src=" not in line
                for line in dump.splitlines()
            ),
            60,
            lambda dump: "default connector still restricts sources: %s" % dump,
        )
    assert _reachable(fab, ga, gb["ip"])
    assert _reachable(fab, gb, ga["ip"])
    assert _probe(fab, ga, fab._rr_ns)
    assert all(proc.poll() is None for proc in restarted)


def test_vtep_sources_follow_host_addition_and_last_route_withdrawal(two_host_fabric):
    fab = two_host_fabric
    ha, hb = fab.hosts
    ga = fab.add_guest(ha, "la", "10.170.0.5", VNI, _dhcp())
    gb = fab.add_guest(hb, "lb", "10.170.0.6", VNI, _dhcp())
    fab.wait_rr_peers(2)
    fab.wait_host_rib(ha, [gb["ip"]])
    _wait_sources(fab, ha, {hb.underlay_ip})

    hc = fab.add_host()
    gc = fab.add_guest(hc, "lc", "10.170.0.7", VNI, _dhcp())
    fab.wait_rr_peers(3)
    fab.wait_host_rib(ha, [gc["ip"]])
    _wait_sources(fab, ha, {hb.underlay_ip, hc.underlay_ip})
    assert _reachable(fab, ga, gc["ip"])
    assert _reachable(fab, gc, ga["ip"])
    assert _probe(fab, ga, hc.ns)

    fab.delete_guest(gc)
    fab.wait_host_rib_without(ha, [gc["ip"]])
    _wait_sources(fab, ha, {hb.underlay_ip})
    assert not _probe(fab, ga, hc.ns)
    assert _reachable(fab, ga, gb["ip"])


def test_ecmp_keeps_both_vtep_sources_and_withdraws_one(two_host_fabric):
    fab = two_host_fabric
    ha, hb = fab.hosts
    ga = fab.add_guest(ha, "ea", "10.170.0.5", VNI, _dhcp())
    gb = fab.add_guest(hb, "eb", "10.170.0.6", VNI, _dhcp())
    hc = fab.add_host()
    gc = fab.add_guest(hc, "ec", gb["ip"], VNI + 1, _dhcp())
    rt_a, rt_b, rt_c = [
        ["%d:%d" % (two_host.AS_NUMBER, VNI + offset)] for offset in range(3)
    ]
    fab.reapply_guest(ga, imp_rt=rt_a + rt_b + rt_c)
    fab.reapply_guest(gb, imp_rt=rt_a + rt_b, exp_rt=rt_b)
    fab.reapply_guest(gc, imp_rt=rt_a + rt_c, exp_rt=rt_c)
    fab.wait_rr_peers(3)
    fab.wait_host_rib(ha, [gb["ip"]])
    _wait_sources(fab, ha, {hb.underlay_ip, hc.underlay_ip})
    nf_harness.wait_until(
        lambda: fab.flows(ha),
        lambda dump: "multipath(" in dump,
        60,
        lambda dump: "no ECMP flow: %s" % dump,
    )
    fab.wait_host_rib(hb, [ga["ip"]])
    fab.wait_host_rib(hc, [ga["ip"]])
    _wait_sources(fab, hb, {ha.underlay_ip})
    _wait_sources(fab, hc, {ha.underlay_ip})
    assert _probe(fab, ga, hb.ns)
    assert _probe(fab, ga, hc.ns)
    receivers = []
    try:
        for target in (gb, gc, ga):
            proc = subprocess.Popen(
                [
                    "ip",
                    "netns",
                    "exec",
                    target["ns"],
                    two_host._sys_python(),
                    "-c",
                    "import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); "
                    "s.bind(('0.0.0.0',42424)); s.settimeout(2); n=0; print('ready',flush=True);"
                    "\ntry:"
                    "\n while True: s.recv(4096); n+=1"
                    "\nexcept socket.timeout: print(n,flush=True)",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            receivers.append(proc)
            assert proc.stdout.readline().strip() == "ready"
        nf_harness._run(
            [
                "ip",
                "netns",
                "exec",
                ga["ns"],
                two_host._sys_python(),
                "-c",
                "import socket,sys;"
                "\nfor port in range(43000,43064):"
                "\n s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); "
                "s.bind(('0.0.0.0',port)); s.sendto(b'ecmp',(sys.argv[1],42424)); s.close()",
                gb["ip"],
            ]
        )
        for source in (gb, gc):
            nf_harness._run(
                [
                    "ip",
                    "netns",
                    "exec",
                    source["ns"],
                    two_host._sys_python(),
                    "-c",
                    "import socket,sys; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); "
                    "s.sendto(b'ecmp',(sys.argv[1],42424))",
                    ga["ip"],
                ]
            )
        counts = []
        for proc in receivers:
            out, err = proc.communicate(timeout=5)
            assert proc.returncode == 0, err
            counts.append(int(out.strip()))
        assert all(count > 0 for count in counts[:2]), counts
        assert sum(counts[:2]) == 64, counts
        assert counts[2] == 2, counts
    finally:
        for proc in receivers:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
    fab.delete_guest(gc)
    _wait_sources(fab, ha, {hb.underlay_ip})
    nf_harness.wait_until(
        lambda: fab.flows(ha),
        lambda dump: "multipath(" not in dump,
        60,
        lambda dump: "withdrawn ECMP branch remains: %s" % dump,
    )
    assert _reachable(fab, ga, gb["ip"])
