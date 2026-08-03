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

"""Integration: one node holding both fabric roles, with real gobgpd.

The single-host stand's shape — the core node is its own route reflector
*and* a hypervisor. gobgpd reads one file, so the agent assembles it from
one fragment per role. What only a real daemon can prove: that the
assembled file is a configuration gobgpd accepts, that the reflector half
still takes dynamic neighbours, and that the hypervisor half is not given
a session to itself.
"""

import json
import os
import time
import uuid as sys_uuid

import pytest

from gcl_sdk.agents.universal.drivers import evpn
from gcl_sdk.agents.universal.drivers.evpn import base
from gcl_sdk.agents.universal.drivers.evpn import fabric
from gcl_sdk.agents.universal.drivers.evpn import sh
from gcl_sdk.tests.integration.sdn import harness as nf_h
from gcl_sdk.tests.integration.sdn_fabric import fabric as f

AS_NUMBER = 65001
DEV = "sdual0"
PEER_DEV = "sdual1"
CLIENT_NS = "sdualc"
NODE_IP = "10.178.1.1"  # the node that is RR *and* hypervisor
CLIENT_IP = "10.178.1.2"  # a plain hypervisor peering with it
UNDERLAY = "10.178.1.0/24"
NODE_API = "127.0.0.1:50071"


class _Daemons(f.ProcGroup):
    def __init__(self, workdir, cache):
        super().__init__(workdir)
        self.cache = cache

    def gobgpd(self, name, conf, api, ns=None):
        prefix = ["ip", "netns", "exec", ns] if ns else []
        return self.spawn(
            name,
            prefix
            + [
                os.path.join(self.cache, "bin", "gobgpd"),
                "-f",
                conf,
                "--api-hosts",
                api,
            ],
        )

    def wait_established(self, api, timeout=60):
        return nf_h.wait_until(
            lambda: f.gobgp(
                os.path.join(self.cache, "bin", "gobgp"), api, ["neighbor"]
            ),
            lambda out: "Establ" in out,
            timeout,
            lambda out: "BGP never established on %s:\n%s" % (api, out),
        )


@pytest.fixture
def dual_role(tmp_path):
    """The dual-role node in the root namespace, a hypervisor in its own.

    Two namespaces, not two addresses on one device: peering a daemon with
    a local address of its own host makes the kernel source the session
    from the destination, and the reflector would see its client under the
    wrong address.
    """
    parts_dir = os.path.join(str(tmp_path), "gobgp.d")
    saved = base.GOBGP_PARTS_DIR
    base.GOBGP_PARTS_DIR = parts_dir
    nf_h._run(["ip", "netns", "add", CLIENT_NS])
    nf_h._run(["ip", "link", "add", DEV, "type", "veth", "peer", "name", PEER_DEV])
    nf_h._run(["ip", "link", "set", PEER_DEV, "netns", CLIENT_NS])
    nf_h._run(["ip", "addr", "add", "%s/24" % NODE_IP, "dev", DEV])
    nf_h._run(["ip", "link", "set", DEV, "up"])
    for cmd in (
        ["addr", "add", "%s/24" % CLIENT_IP, "dev", PEER_DEV],
        ["link", "set", PEER_DEV, "up"],
        ["link", "set", "lo", "up"],
    ):
        nf_h._run(["ip", "-n", CLIENT_NS] + cmd)
    daemons = _Daemons(str(tmp_path), f.default_cache_dir())
    try:
        yield daemons, parts_dir
    finally:
        daemons.cleanup()
        base.GOBGP_PARTS_DIR = saved
        nf_h._run(["ip", "link", "del", DEV], check=False)
        nf_h._run(["ip", "netns", "del", CLIENT_NS], check=False)


def test_a_node_that_is_its_own_reflector_serves_both_roles(dual_role, tmp_path):
    daemons, parts_dir = dual_role

    # The node holds both roles, and its hypervisor half is pointed at
    # itself — exactly how the single-host stand is configured.
    rr = evpn.BgpRr(
        uuid=sys_uuid.uuid4(), as_number=AS_NUMBER, peer_prefixes=[UNDERLAY]
    )
    host = evpn.EvpnHost(
        uuid=sys_uuid.uuid4(), as_number=AS_NUMBER, rr_addresses=[NODE_IP]
    )
    host._write_part(NODE_IP)
    rr._write_part(NODE_IP)
    assert sorted(os.listdir(parts_dir)) == ["bgp_rr.json", "evpn_host.json"]

    conf = fabric.assemble_gobgp_conf()
    # One daemon, one identity, and no session to itself.
    assert conf.count("[global.config]") == 1
    assert "route-reflector-client = true" in conf
    assert 'neighbor-address = "%s"' % NODE_IP not in conf

    node_conf = os.path.join(str(tmp_path), "gobgp-node.conf")
    with open(node_conf, "w") as fl:
        fl.write(conf)
    daemons.gobgpd("gobgpd-node", node_conf, NODE_API)

    client = evpn.EvpnHost(
        uuid=sys_uuid.uuid4(), as_number=AS_NUMBER, rr_addresses=[NODE_IP]
    )
    client_conf = os.path.join(str(tmp_path), "gobgp-client.conf")
    with open(client_conf, "w") as fl:
        fl.write(f.role_conf(client, CLIENT_IP))
    daemons.gobgpd("gobgpd-client", client_conf, "127.0.0.1:50072", ns=CLIENT_NS)

    # The reflector half works: the hypervisor comes up as a dynamic
    # neighbour of the very daemon that also carries the hypervisor role —
    # and it is the node's only neighbour, because the hypervisor role's
    # own RR address is this node and a session to oneself never comes up.
    established = daemons.wait_established(NODE_API)
    assert CLIENT_IP in established
    assert NODE_IP not in established


def test_the_roles_do_not_overwrite_each_other(dual_role, tmp_path):
    """The bug this replaced: each role rendered the whole file, so the two
    took turns owning it and restarted the daemon on every iteration. Now
    each apply is a no-op once the fragments are in place."""
    daemons, parts_dir = dual_role
    conf_file = os.path.join(str(tmp_path), "gobgp.conf")
    saved = base.GOBGP_CONF_FILE
    base.GOBGP_CONF_FILE = conf_file
    restarts = []

    def run(cmd):
        if cmd[0] == base.SYSTEMCTL_BIN:
            if "restart" in cmd or "enable" in cmd:
                restarts.append(cmd)
            return ""
        if cmd[0] == base.IP_BIN and "route" in cmd:
            return json.dumps([{"prefsrc": NODE_IP}])
        return ""

    try:
        rr = evpn.BgpRr(
            uuid=sys_uuid.uuid4(), as_number=AS_NUMBER, peer_prefixes=[UNDERLAY]
        )
        host = evpn.EvpnHost(
            uuid=sys_uuid.uuid4(), as_number=AS_NUMBER, rr_addresses=[NODE_IP]
        )
        real_run = sh.run
        sh.run = run
        try:
            rr.dump_to_dp()
            host.dump_to_dp()
            settled = len(restarts)
            for _ in range(3):
                rr.update_on_dp()
                host.update_on_dp()
            assert len(restarts) == settled
        finally:
            sh.run = real_run

        # both roles' sections survived every pass
        with open(conf_file) as fl:
            conf = fl.read()
        assert "[[peer-groups]]" in conf and "[[dynamic-neighbors]]" in conf
        # gobgpd accepts what the reconcile converged on
        daemons.gobgpd("gobgpd-settled", conf_file, NODE_API)
        time.sleep(3)
        assert daemons.procs[-1][1].poll() is None, "gobgpd rejected the config"
    finally:
        base.GOBGP_CONF_FILE = saved
