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

"""Full-fabric SDN harness: the real EVPN control+data plane on one host.

Extends the NF-tier :class:`~..sdn.harness.SdnHarness` with the pieces the
NF tier deliberately leaves out — the **real external forwarder stack**:

* an RR ``gobgpd`` (config rendered by the real :class:`BgpRr` renderer,
  passive dynamic-neighbors) on a dedicated underlay address;
* a host ``gobgpd`` (real :class:`EvpnHost` renderer, GR+LLGR);
* the **real ``evpn_connector`` daemon** (config rendered by the real
  :class:`EvpnHost` connector renderer) owning the test EVPN bridge and
  consuming the client configs the real :class:`EvpnPort` driver writes.

Deviation from production: process supervision — systemd starts/restarts
the daemons there; here the harness owns the processes directly.
"""

import os
import socket
import subprocess
import time
import uuid as sys_uuid

from gcl_sdk.agents.universal.drivers import evpn
from gcl_sdk.agents.universal.drivers.evpn import base

from gcl_sdk.tests.integration.sdn import harness as nf_harness

AS_NUMBER = 65001
# Dedicated underlay for the fabric tier; keep clear of anything real.
UNDERLAY_DEV_PREFIX = "sfab"
HOST_IP = "10.177.1.1"
RR_IP = "10.177.1.10"
UNDERLAY_PREFIX = "10.177.1.0/24"
# The channel the renderer writes, and therefore the one a deployed
# connector uses. The tier does not run on it: a machine that also runs an
# installation already has a gobgpd holding that port, and this tier would
# then be asking a live daemon about a fabric it did not build. So the pair
# is moved to ports nobody has claimed, and the renderer's own value is
# asserted before it is overridden -- what production uses stays tested,
# and what this tier talks to is unambiguously its own.
DEPLOYED_HOST_API = "unix:///run/gobgpd/api.sock"


def _free_port():
    """A port the kernel says is free right now.

    Not a fixed high number: two runs of this tier on one machine would
    then collide with each other, which is the same failure this moves away
    from, only harder to see.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def gobgp(binary, api, args, ns=None):
    """The ``gobgp`` CLI against one daemon's ``host:port`` API, in ``ns``."""
    host, port = api.split(":")
    prefix = ["ip", "netns", "exec", ns] if ns else []
    return subprocess.run(
        prefix + [binary, "-u", host, "-p", port] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ).stdout


class ProcGroup:
    """Daemons a tier runs itself, each logging to ``<workdir>/<name>.log``."""

    def __init__(self, workdir):
        self.workdir = workdir
        self.procs = []

    def spawn(self, name, cmd):
        log = open(os.path.join(self.workdir, name + ".log"), "w")
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
        self.procs.append((name, proc, log))
        return proc

    def cleanup(self):
        for _, proc, log in self.procs:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
            log.close()


def default_cache_dir():
    sudo_user = os.environ.get("SUDO_USER")
    if "SDN_FABRIC_CACHE" in os.environ:
        return os.environ["SDN_FABRIC_CACHE"]
    home = os.path.expanduser("~%s" % sudo_user) if sudo_user else "~"
    return os.path.join(os.path.expanduser(home), ".cache", "exordos-sdn-fabric")


def role_conf(model, source_ip):
    """One fabric role's complete gobgpd config.

    On a host the agent assembles `/etc/gobgp.conf` from a fragment per
    role, because a node may hold both roles at once; here each role runs
    as its own daemon, so each gets its own file.
    """
    return "\n".join(
        [
            base.GOBGP_CONF_MARKER + "\n",
            model._render_global(source_ip),
            model._render_body(source_ip),
        ]
    )


def cache_ready(cache):
    return os.access(os.path.join(cache, "bin", "gobgpd"), os.X_OK) and os.access(
        os.path.join(cache, "venv", "bin", "evpn-connector"), os.X_OK
    )


class FabricHarness(nf_harness.SdnHarness):
    def __init__(self, workdir, cache):
        super().__init__(workdir)
        self._cache = cache
        self._gobgp = os.path.join(cache, "bin", "gobgp")
        self._gobgpd = os.path.join(cache, "bin", "gobgpd")
        self._connector = os.path.join(cache, "venv", "bin", "evpn-connector")
        self._underlay_dev = UNDERLAY_DEV_PREFIX + self.br[2:8]
        self._workdir = workdir
        self._daemons = ProcGroup(workdir)
        self._host_port = _free_port()
        self._rr_port = _free_port()
        self._host_api = "127.0.0.1:%d" % self._host_port
        self._rr_api = "127.0.0.1:%d" % self._rr_port

    # --- control plane ----------------------------------------------------

    def start_fabric(self):
        """Underlay addresses + RR gobgpd + host gobgpd + evpn_connector."""
        nf_harness._run(["ip", "link", "add", self._underlay_dev, "type", "dummy"])
        nf_harness._run(["ip", "link", "set", self._underlay_dev, "up"])
        for addr in (HOST_IP, RR_IP):
            nf_harness._run(
                ["ip", "addr", "add", "%s/24" % addr, "dev", self._underlay_dev]
            )

        rr = evpn.BgpRr(
            uuid=sys_uuid.uuid4(),
            as_number=AS_NUMBER,
            peer_prefixes=[UNDERLAY_PREFIX],
        )
        rr_conf = os.path.join(self._workdir, "gobgp-rr.conf")
        with open(rr_conf, "w") as fl:
            fl.write(role_conf(rr, RR_IP))

        host = evpn.EvpnHost(
            uuid=sys_uuid.uuid4(),
            as_number=AS_NUMBER,
            rr_addresses=[RR_IP],
        )
        host_conf = os.path.join(self._workdir, "gobgp-host.conf")
        with open(host_conf, "w") as fl:
            fl.write(role_conf(host, HOST_IP))
        connector_conf = os.path.join(self._workdir, "evpn_connector.cfg")
        with open(connector_conf, "w") as fl:
            fl.write(self._connector_conf_on_our_own_port(host))

        self._daemons.spawn(
            "gobgpd-rr",
            [self._gobgpd, "-f", rr_conf, "--api-hosts", self._rr_api],
        )
        self._daemons.spawn(
            "gobgpd-host",
            [self._gobgpd, "-f", host_conf, "--api-hosts", self._host_api],
        )
        self._daemons.spawn(
            "evpn-connector",
            [self._connector, "--config-file", connector_conf],
        )
        self._require_the_fabric_we_started()

    def _connector_conf_on_our_own_port(self, host):
        """The rendered connector config, pointed at this run's daemon.

        The renderer writes the deployed channel, and that value is what a
        real installation uses -- so it is asserted here rather than quietly
        replaced: if the renderer ever stops writing it, this fails instead
        of silently testing something else. Only then is it redirected, and
        only the channel: everything else the renderer decided stands.
        """
        # Its files in this run's directory, not a live connector's.
        saved, base.CONNECTOR_RUN_DIR = base.CONNECTOR_RUN_DIR, self._workdir
        try:
            conf = host._render_connector_conf(HOST_IP)
        finally:
            base.CONNECTOR_RUN_DIR = saved
        assert DEPLOYED_HOST_API in conf, (
            "the connector renderer no longer writes %s, so this tier does "
            "not know which line to point at its own daemon:\n%s"
            % (DEPLOYED_HOST_API, conf)
        )
        return conf.replace(DEPLOYED_HOST_API, self._host_api)

    def _require_the_fabric_we_started(self):
        """Refuse to test somebody else's daemon.

        This tier used to run its gobgpd on the deployed channel's port, so
        on a machine that also runs a real installation it could not bind
        and died at once -- and every query then answered from the *live*
        daemon, which has peers, a RIB and prefixes of its own. Everything
        passed, about a fabric this tier did not build.

        That is not hypothetical: it is what this tier did, silently, until
        a version skew happened to make the same collision fail loudly
        instead. The ports are this run's own now, which removes that
        particular collision -- and the check stays, because what it really
        says is "assert nothing until the things we started are running",
        and a daemon can die of more than a busy port.
        """
        time.sleep(1)
        for name, proc, _ in self._daemons.procs:
            if proc.poll() is None:
                continue
            log = os.path.join(self._workdir, name + ".log")
            try:
                with open(log) as fl:
                    tail = "".join(fl.readlines()[-5:])
            except OSError:
                tail = "(no log)"
            raise AssertionError(
                "%s exited at once, so anything answering afterwards is not "
                "this fabric.\n%s" % (name, tail)
            )

    # --- waiting ----------------------------------------------------------

    def wait_bgp_established(self, timeout=60):
        return nf_harness.wait_until(
            lambda: gobgp(self._gobgp, self._host_api, ["neighbor"]),
            lambda out: "Establ" in out,
            timeout,
            lambda out: "BGP session never established:\n%s" % out,
        )

    def wait_rib(self, prefixes, timeout=90):
        """Wait until every prefix is announced (sync_period is 10s)."""
        return nf_harness.wait_until(
            self.rib_routes,
            lambda rib: all(p in rib for p in prefixes),
            timeout,
            lambda rib: (
                "missing %s in:\n%s" % ([p for p in prefixes if p not in rib], rib)
            ),
            interval=3,
        )

    def rib_routes(self):
        return gobgp(self._gobgp, self._host_api, ["global", "rib", "-a", "evpn"])

    def wait_flows(self, min_flows, timeout=180):
        """Wait until the connector has compiled at least ``min_flows``
        flows onto the EVPN bridge (the first sync takes ~1 min)."""

        def compiled():
            # The connector sets an OpenFlow13-only bridge.
            out = nf_harness._run(
                ["ovs-ofctl", "-O", "OpenFlow13", "dump-flows", self.evpn_br],
                check=False,
            )
            return len([ln for ln in out.splitlines() if "actions=" in ln])

        return nf_harness.wait_until(
            compiled,
            lambda n: n >= min_flows,
            timeout,
            lambda n: "connector never compiled >=%d flows (last %d)" % (min_flows, n),
            interval=3,
        )

    # --- guests through the real driver ------------------------------------

    def add_fabric_guest(self, name, ipv4, vni, dhcp):
        """A guest wired by the **real** EvpnPort driver.

        The veth lands on br-int untagged with ``external_ids:iface-id``
        set (what libvirt's openvswitch virtualport does); the driver then
        resolves it, assigns the per-guest VLAN, builds the patch pair,
        writes the client config/NF record and the egress namespace.
        """
        guest = self.add_guest(name, vlan=0, ip="%s/24" % ipv4)
        port_uuid = sys_uuid.uuid4()
        nf_harness._run(
            [
                "ovs-vsctl",
                "set",
                "interface",
                guest.host_veth,
                'external_ids:iface-id="%s"' % port_uuid,
            ]
        )
        port = evpn.EvpnPort(
            uuid=port_uuid,
            mac=guest.mac,
            ipv4=ipv4,
            vni=vni,
            imp_rt=["%d:%d" % (AS_NUMBER, vni)],
            exp_rt=["%d:%d" % (AS_NUMBER, vni)],
            dhcp=dhcp,
        )
        port.dump_to_dp()
        self._egress_vnis.add(vni)
        gw = next(r["via"] for r in dhcp["routers"] if r.get("via"))
        guest.exec("ip", "link", "set", guest.dev, "mtu", str(dhcp["mtu"]))
        guest.exec("ip", "route", "replace", "default", "via", gw)
        return guest, port

    # --- cleanup ----------------------------------------------------------

    def cleanup(self):
        self._daemons.cleanup()
        nf_harness._run(["ip", "link", "del", self._underlay_dev], check=False)
        super().cleanup()
