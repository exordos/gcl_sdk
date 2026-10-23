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

"""What can carry a group identity across our two bridges.

A security-group rule that names *a group* rather than a prefix can be built
two ways. Either every host that references the group learns every member
(OpenStack's `remote_group_id`), or the sender's group travels with the
packet and the receiving host matches it (AWS, GCP, Azure). The second needs
somewhere to put the identity, and our packets cross a boundary that decides
it: `br-int`, where our security groups live, is joined to the `evpn` bridge,
which evpn_connector owns and where the VXLAN tunnel is, by a **patch pair**.

These tests establish, on real OVS, which fields survive that hop. The answer
is the whole design:

* `tun_gbp_id` — the 16 bits VXLAN-GBP puts in the tunnel header — is
  matchable on the bridge that owns the tunnel and is **cleared** crossing a
  patch, in both directions. A mark set on `br-int` never reaches the wire,
  and a mark off the wire never reaches `br-int`.
* `pkt_mark` — the kernel skb mark — **survives**, and so is the carrier
  between our bridge and the fabric's.

So a group identity is buildable, and the contract with evpn_connector is
exactly two field copies at the tunnel boundary (`pkt_mark` <-> `tun_gbp_id`),
which is where the connector's own flows already are. Guests on one host
never cross the tunnel at all: their mark rides `pkt_mark` the whole way.
"""

import socket
import select
import subprocess
import time

import pytest

from gcl_sdk.tests.integration.sdn import harness as nf_harness


def _vsctl(args, check=True):
    return subprocess.run(
        ["ovs-vsctl"] + args, capture_output=True, text=True, check=check
    ).stdout


def _ofctl(args):
    return subprocess.run(
        ["ovs-ofctl", "-O", "OpenFlow13"] + args,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _trace(bridge, flow):
    return subprocess.run(
        ["ovs-appctl", "ofproto/trace", bridge, flow],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture
def patched_bridges():
    """Two bridges joined by a patch, the far one holding a GBP tunnel.

    The same shape as a hypervisor's `br-int` <-> `evpn`, with names of its
    own so it can never disturb a real one. The tunnel's remote does not
    exist and does not need to: what is under test is what the *translation*
    keeps, which `ofproto/trace` answers exactly.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("0.0.0.0", 0))
        tunnel_port = probe.getsockname()[1]
    near, far = "gbpprobea", "gbpprobeb"
    for br in (near, far):
        _vsctl(["--if-exists", "del-br", br])
    _vsctl(["add-br", near, "--", "add-br", far])
    _vsctl(
        [
            "add-port",
            near,
            "ppa",
            "--",
            "set",
            "interface",
            "ppa",
            "type=patch",
            "options:peer=ppb",
        ]
    )
    _vsctl(
        [
            "add-port",
            far,
            "ppb",
            "--",
            "set",
            "interface",
            "ppb",
            "type=patch",
            "options:peer=ppa",
        ]
    )
    # A dst_port of our own: OVS refuses GBP and non-GBP VXLAN on one port,
    # and the host may already run a fabric on 4789.
    _vsctl(
        [
            "add-port",
            far,
            "ppvx",
            "--",
            "set",
            "interface",
            "ppvx",
            "type=vxlan",
            "options:remote_ip=10.178.9.9",
            "options:dst_port=%d" % tunnel_port,
            "options:exts=gbp",
            "options:key=1234",
        ]
    )
    try:
        yield near, far
    finally:
        for br in (near, far):
            _vsctl(["--if-exists", "del-br", br], check=False)


def test_a_tunnel_mark_does_not_cross_the_patch(patched_bridges):
    near, far = patched_bridges

    # Outbound: set on our bridge, gone by the time the fabric's bridge —
    # the one that would put it on the wire — sees the packet.
    _ofctl(
        [
            "add-flow",
            near,
            "priority=100,ip,actions=set_field:42->tun_gbp_id,output:ppa",
        ]
    )
    _ofctl(["add-flow", far, "priority=100,tun_gbp_id=42,actions=LOCAL"])
    _ofctl(["add-flow", far, "priority=50,actions=drop"])
    out = _trace(near, "in_port=LOCAL,ip,nw_src=10.0.0.1,nw_dst=10.0.0.2")
    assert "tun_gbp_id=42" not in out.split('bridge("%s")' % far)[1]

    # Inbound: matchable where the tunnel is, gone one patch later.
    _ofctl(["del-flows", far])
    _ofctl(["del-flows", near])
    _ofctl(["add-flow", far, "priority=100,tun_gbp_id=42,actions=output:ppb"])
    _ofctl(["add-flow", near, "priority=100,tun_gbp_id=42,actions=LOCAL"])
    _ofctl(["add-flow", near, "priority=50,actions=drop"])
    out = _trace(
        far, "in_port=ppvx,tun_gbp_id=42,tun_id=1234,ip,nw_src=10.0.0.1,nw_dst=10.0.0.2"
    )
    assert "tun_gbp_id=42, priority 100" in out  # it does arrive, on that bridge
    assert "priority 50" in out.split('bridge("%s")' % near)[1]  # ... and no further


def test_the_skb_mark_is_the_carrier_between_our_bridges(patched_bridges):
    near, far = patched_bridges

    _ofctl(
        ["add-flow", near, "priority=100,ip,actions=set_field:77->pkt_mark,output:ppa"]
    )
    _ofctl(["add-flow", far, "priority=100,pkt_mark=77,actions=LOCAL"])
    _ofctl(["add-flow", far, "priority=50,actions=drop"])

    out = _trace(near, "in_port=LOCAL,ip,nw_src=10.0.0.1,nw_dst=10.0.0.2")
    beyond = out.split('bridge("%s")' % far)[1]
    assert "pkt_mark=0x4d, priority 100" in beyond, (
        "pkt_mark must cross the patch, or nothing our bridge sets can reach "
        "the fabric's bridge and a group identity has no carrier at all"
    )


VNI = 17100
CIDR = "10.171.0.0/24"
MARK = 42


def _guest_dhcp():
    # As in the sibling two-host test: no gateway, so cross-host traffic is
    # pure connector L3 and the per-VNI egress names cannot collide between
    # two "hosts" that share this one kernel.
    return {"cidr": CIDR, "routers": [], "dns_servers": [], "mtu": 1400}


def _host_ofctl(host, args):
    return subprocess.run(
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
        ]
        + args,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _packets(dump, needle):
    for line in dump.splitlines():
        if needle in line:
            return int(line.split("n_packets=")[1].split(",")[0])
    return None


def test_an_identity_crosses_the_fabric_end_to_end(two_host_fabric):
    """The mark a host puts on a guest's traffic reaches the other host.

    This is the contract the fabric owes a policy built on identity: the
    sender's group is set where the guest is, copied onto the wire by the
    connector, taken back off it on the far side, and is matchable on the
    bridge where security groups live. Two real hosts, real gobgpd, the real
    connector, real VXLAN.
    """
    fab = two_host_fabric
    ha, hb = fab.hosts

    ga = fab.add_guest(ha, "ga", "10.171.0.5", VNI, _guest_dhcp())
    fab.add_guest(hb, "gb", "10.171.0.6", VNI, _guest_dhcp())
    fab.wait_rr_peers(2)
    fab.wait_host_rib(ha, ["10.171.0.6"])
    fab.wait_host_rib(hb, ["10.171.0.5"])
    fab.wait_forwarding(ga, "10.171.0.6")

    # Sender: what a port's identity would do — above the ct pipeline, since
    # what is under test is carriage rather than filtering.
    _host_ofctl(
        ha,
        [
            "add-flow",
            "br-int",
            "table=0,priority=300,ip,nw_src=10.171.0.5,"
            "actions=set_field:%d->pkt_mark,NORMAL" % MARK,
        ],
    )
    # Receiver: what a rule would match.
    _host_ofctl(
        hb,
        [
            "add-flow",
            "br-int",
            "table=0,priority=300,pkt_mark=%d,actions=NORMAL" % MARK,
        ],
    )

    # ovs-ofctl prints the mark in hex, so ask for it the way it answers.
    needle = "pkt_mark=0x%x" % MARK
    before = _packets(_host_ofctl(hb, ["dump-flows", "br-int"]), needle)
    assert before is not None, "the match did not install"

    assert fab.ping(ga, "10.171.0.6"), "marked traffic must still be forwarded"

    after = _packets(_host_ofctl(hb, ["dump-flows", "br-int"]), needle)
    assert after > before, (
        "the identity did not survive the crossing: set on the sending "
        "host's br-int, it must ride pkt_mark to the tunnel, VXLAN-GBP over "
        "it, and pkt_mark again into the far host's br-int"
    )


SG_VNI = 17200
SG_CIDR = "10.172.0.0/24"
# Bits, not names: what a port carries is the set of groups it is in.
SUBNET_BIT, WEB, DB, LISTEN = 0x1, 0x2, 0x4, 8080


def _sg_dhcp():
    return {"cidr": SG_CIDR, "routers": [], "dns_servers": [], "mtu": 1400}


def _listener(guest, port=LISTEN, echo=False):
    handler = (
        "def handle(c):\n"
        "    with c:\n"
        "        while data := c.recv(4096):\n"
        "            c.sendall(data)\n"
        if echo
        else "def handle(c): c.close()\n"
    )
    return subprocess.Popen(
        [
            "ip",
            "netns",
            "exec",
            guest["ns"],
            "python3",
            "-c",
            "import socket, threading\n" + handler + "s = socket.socket()\n"
            "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
            's.bind(("0.0.0.0", %d))\n'
            "s.listen(16)\n"
            "while True:\n"
            "    c, _ = s.accept()\n"
            "    threading.Thread(target=handle, args=(c,), daemon=True).start()\n"
            % port,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _connects(guest, ip, port=LISTEN, wait=3):
    return (
        subprocess.run(
            [
                "ip",
                "netns",
                "exec",
                guest["ns"],
                "timeout",
                str(wait),
                "bash",
                "-c",
                "exec 3<>/dev/tcp/%s/%d" % (ip, port),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def test_a_rule_that_names_a_group_holds_across_the_fabric(two_host_fabric):
    """The point of the whole mechanism, on two hosts and a real tunnel.

    The receiver admits a group. No address appears in its rule, and no host
    is told who is in that group: the sender stamps what it is, the fabric
    carries it, and the receiver matches it. Then the sender changes group —
    one port recompiled, the receiver untouched — and new connections
    stop being allowed while the established session survives.
    """
    fab = two_host_fabric
    ha, hb = fab.hosts

    sender = fab.add_guest(ha, "sgs", "10.172.0.5", SG_VNI, _sg_dhcp(), identity=WEB)
    target = fab.add_guest(
        hb,
        "sgt",
        "10.172.0.6",
        SG_VNI,
        _sg_dhcp(),
        security_rules=[
            {
                "direction": "ingress",
                "proto": "tcp",
                "port": LISTEN,
                "remote_identity": WEB,
            },
            {"direction": "egress", "proto": "any"},
        ],
    )
    fab.wait_rr_peers(2)
    fab.wait_host_rib(ha, ["10.172.0.6"])
    fab.wait_host_rib(hb, ["10.172.0.5"])

    proc = _listener(target, echo=True)
    session = None
    try:
        time.sleep(1)
        assert nf_harness.wait_until(
            lambda: _connects(sender, "10.172.0.6"), bool, timeout=40
        ), "a member of the named group gets in"
        session = subprocess.Popen(
            [
                "ip",
                "netns",
                "exec",
                sender["ns"],
                "python3",
                "-u",
                "-c",
                "import socket, sys\n"
                "s = socket.create_connection(('10.172.0.6', %d), timeout=5)\n"
                "r = s.makefile('rb')\n"
                "for line in sys.stdin.buffer:\n"
                "    s.sendall(line)\n"
                "    print(r.readline().decode().strip(), flush=True)\n" % LISTEN,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )

        def exchange(message):
            session.stdin.write(message + b"\n")
            session.stdin.flush()
            assert select.select([session.stdout], [], [], 6)[0], (
                "session reply timed out"
            )
            assert session.stdout.readline() == message + b"\n"

        exchange(b"before-membership-change")

        # Membership is what grants it: move the sender to another group and
        # the receiver's rule — unchanged — no longer admits it.
        fab.reapply_guest(sender, identity=SUBNET_BIT | DB)
        assert not _connects(sender, "10.172.0.6", wait=3), "a non-member is refused"
        exchange(b"after-membership-change")

        # ... and back again, because this is membership and not a lease.
        fab.reapply_guest(sender, identity=SUBNET_BIT | WEB)
        assert _connects(sender, "10.172.0.6")
    finally:
        if session is not None:
            session.kill()
            session.wait()
        proc.kill()
        proc.wait()


def test_a_fabric_halfway_through_the_gbp_change_keeps_working(two_host_fabric):
    """What a mixed fabric actually does, rather than what was assumed.

    Turning the tunnel into a GBP one is a property of the whole fabric —
    OVS refuses GBP and non-GBP VXLAN on one UDP port — and the docs said
    compatibility was broken deliberately, implying frames stop. They do
    not: GBP lives in bits a plain VXLAN receiver ignores, so a
    not-yet-upgraded host keeps forwarding and only loses the identity.

    Nor is the identity lost, which was the second guess: the option
    governs what a port encodes, and a receiver reads the bits whichever way
    its own port is configured. So an operator upgrading one hypervisor at a
    time sees neither dropped traffic nor group rules quietly denying — the
    thing to schedule deliberately is the change itself, not a window of
    breakage.
    """
    fab = two_host_fabric
    ha, hb = fab.hosts
    dhcp = {
        "cidr": "10.173.0.0/24",
        "routers": [],
        "dns_servers": [],
        "mtu": 1400,
    }

    ga = fab.add_guest(ha, "mxa", "10.173.0.5", 17300, dhcp, identity=MARK)
    fab.add_guest(hb, "mxb", "10.173.0.6", 17300, dhcp)
    fab.wait_rr_peers(2)
    fab.wait_host_rib(ha, ["10.173.0.6"])
    fab.wait_host_rib(hb, ["10.173.0.5"])

    needle = "pkt_mark=0x%x" % MARK
    _host_ofctl(
        hb,
        [
            "add-flow",
            "br-int",
            "table=0,priority=300,pkt_mark=%d,actions=NORMAL" % MARK,
        ],
    )
    fab.wait_forwarding(ga, "10.173.0.6")
    carried = _packets(_host_ofctl(hb, ["dump-flows", "br-int"]), needle)
    assert carried is not None

    # One host is rolled back to a plain tunnel, as it would be mid-upgrade.
    fab._ovs_vsctl(hb, ["remove", "interface", "vxlan_out", "options", "exts"])
    options = fab._ovs_vsctl(hb, ["get", "interface", "vxlan_out", "options"])
    assert "gbp" not in options, "the rollback has to have taken effect"
    time.sleep(2)
    before = _packets(_host_ofctl(hb, ["dump-flows", "br-int"]), needle)
    assert fab.ping(ga, "10.173.0.6"), "the fabric keeps forwarding"
    after = _packets(_host_ofctl(hb, ["dump-flows", "br-int"]), needle)
    # Measured, and not what was assumed: the identity arrives anyway. The
    # option governs what a port *encodes*; the bits ride in reserved space
    # and the receiver reads them regardless, so a fabric halfway through an
    # upgrade neither drops traffic nor mis-evaluates a group rule.
    assert after > before, "and the identity arrives even so"

    fab._ovs_vsctl(hb, ["set", "interface", "vxlan_out", "options:exts=gbp"])
    time.sleep(2)
    assert fab.ping(ga, "10.173.0.6")
    assert _packets(_host_ofctl(hb, ["dump-flows", "br-int"]), needle) > after, (
        "and it arrives again once the host is upgraded"
    )
