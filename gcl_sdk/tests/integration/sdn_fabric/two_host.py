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

"""Two-host EVPN fabric on one machine: real cross-host VXLAN forwarding.

Where the single-host tier (``fabric.py``) proves the full CP+DP stack on
one host, this one adds the piece that only bites across hosts: a guest on
host A reaches a guest on host B over VXLAN (routes reflected by the RR),
and the data plane survives losing the route reflector (fail-static) —
which is only meaningful once there are *remote*, reflected routes to lose.

To keep both "hosts" fully real yet share one kernel, each host runs in its
own network namespace with its **own** OVS instance (ovsdb-server +
ovs-vswitchd on a private OVS_RUNDIR, hence a private per-netns datapath),
its own gobgpd and the real ``evpn_connector``. Because the namespaces are
isolated, the two hosts reuse identical names and ports (``evpn``,
``br-int``, gRPC :50051, the connector's ``vxlan_out``) — only their
filesystem paths differ. One RR in a third namespace reflects between them;
the underlay is a plain Linux bridge in the root namespace.

The guests are wired by the **real** ``EvpnPort`` driver, run inside the
owning host's namespace via ``_port_apply`` so every OVS/ip command it
issues lands in that host's namespace and OVS — no mocking anywhere.

Deviation from production: the two hosts share one kernel (separate netns +
OVS instances); the EVPN control and data path are otherwise real.
"""

import json
import os
import subprocess
import time
import uuid as sys_uuid

from gcl_sdk.agents.universal.drivers import evpn
from gcl_sdk.agents.universal.drivers.evpn import base
from gcl_sdk.tests.integration.sdn import harness as nf_harness
from gcl_sdk.tests.integration.sdn_fabric import fabric as single

AS_NUMBER = single.AS_NUMBER
RR_IP = "10.178.1.10"
UNDERLAY_PREFIX = "10.178.1.0/24"
GRPC_PORT = 50051

_OVS_SCHEMAS = (
    "/usr/share/openvswitch/vswitch.ovsschema",
    "/usr/local/share/openvswitch/vswitch.ovsschema",
)


def _ovs_schema():
    for path in _OVS_SCHEMAS:
        if os.path.exists(path):
            return path
    return None


def daemons_ready():
    """The two-host tier additionally needs to run private OVS instances."""
    from shutil import which

    return _ovs_schema() is not None and all(
        which(b) for b in ("ovsdb-server", "ovsdb-tool", "ovs-vswitchd")
    )


class _Host:
    def __init__(self, workdir, tag, index):
        self.index = index
        self.name = "h%d" % index
        self.underlay_ip = "10.178.1.%d" % (index + 1)
        self.ns = "%sh%d" % (tag, index)
        self.veth = "%sv%d" % (tag, index)  # root-side underlay leg
        self.veth_ns = "%svn%d" % (tag, index)  # host-ns underlay leg
        self.run = os.path.join(workdir, "ovs%d" % index)  # private OVS_RUNDIR
        # Every driver path of this host, under one private root.
        self.paths = nf_harness.host_paths(os.path.join(workdir, self.name))
        self.conf_dir = self.paths["CLIENT_CONF_DIR"]
        self.records_dir = self.paths["NF_RECORDS_DIR"]
        self.gobgp_conf = os.path.join(workdir, "%s_gobgp.conf" % self.name)
        self.connector_conf = os.path.join(workdir, "%s_connector.cfg" % self.name)
        for d in (self.run, self.conf_dir, self.records_dir):
            os.makedirs(d, exist_ok=True)
        self.guests = []

    def env(self):
        """Driver globals for this host (bridges shared, paths private)."""
        return {
            "OVS_RUNDIR": self.run,
            "EVPN_HOST_GLOBALS": json.dumps(
                dict(self.paths, BR_INT="br-int", EVPN_BRIDGE="evpn")
            ),
        }


_run = nf_harness._run


class TwoHostFabric:
    def __init__(self, workdir, cache):
        self._workdir = workdir
        self._gobgp = os.path.join(cache, "bin", "gobgp")
        self._gobgpd = os.path.join(cache, "bin", "gobgpd")
        self._connector = os.path.join(cache, "venv", "bin", "evpn-connector")
        self._tag = "t%s" % sys_uuid.uuid4().hex[:4]
        self._ubr = "u%s" % self._tag  # root-ns underlay bridge
        self._rr_ns = "%srr" % self._tag
        self._rr_veth = "%svr" % self._tag
        self._rr_veth_ns = "%svrn" % self._tag
        self.hosts = [_Host(workdir, self._tag, i) for i in range(2)]
        self._daemons = single.ProcGroup(workdir)
        self._ovs = []  # (rundir, ns) of started OVS instances
        # Guests whose port was deleted through the driver. They leave
        # `host.guests`, which is what "this host serves it" means, but their
        # namespace and veth are still on the machine and teardown has to
        # take them: a test that deletes a port used to leak one of each per
        # run, which is invisible until somebody lists the namespaces.
        self._gone = []

    # --- underlay + namespaces -------------------------------------------

    def setup(self):
        # Root-ns Linux bridge is the shared underlay fabric.
        _run(["ip", "link", "add", self._ubr, "type", "bridge"])
        _run(["ip", "link", "set", self._ubr, "up"])
        # RR namespace + underlay leg.
        self._attach_ns(self._rr_ns, self._rr_veth, self._rr_veth_ns, RR_IP)
        # Each host: namespace, underlay leg, private OVS instance.
        for h in self.hosts:
            self._attach_ns(h.ns, h.veth, h.veth_ns, h.underlay_ip)
            self._start_ovs(h)

    def _attach_ns(self, ns, veth, veth_ns, ip):
        _run(["ip", "netns", "add", ns])
        _run(["ip", "netns", "exec", ns, "ip", "link", "set", "lo", "up"])
        _run(["ip", "link", "add", veth, "type", "veth", "peer", "name", veth_ns])
        _run(["ip", "link", "set", veth, "master", self._ubr])
        _run(["ip", "link", "set", veth, "up"])
        _run(["ip", "link", "set", veth_ns, "netns", ns])
        _run(["ip", "netns", "exec", ns, "ip", "link", "set", veth_ns, "up"])
        _run(
            [
                "ip",
                "netns",
                "exec",
                ns,
                "ip",
                "addr",
                "add",
                "%s/24" % ip,
                "dev",
                veth_ns,
            ]
        )

    def _start_ovs(self, host):
        schema = _ovs_schema()
        db = os.path.join(host.run, "conf.db")
        _run(["ovsdb-tool", "create", db, schema])
        self._daemons.spawn(
            "ovsdb-%s" % host.name,
            [
                "ip",
                "netns",
                "exec",
                host.ns,
                "ovsdb-server",
                db,
                "--remote=punix:%s/db.sock" % host.run,
                "--unixctl=%s/ovsdb.ctl" % host.run,
            ],
        )
        self._daemons.spawn(
            "vswitchd-%s" % host.name,
            [
                "ip",
                "netns",
                "exec",
                host.ns,
                "env",
                "OVS_RUNDIR=%s" % host.run,
                "ovs-vswitchd",
                "unix:%s/db.sock" % host.run,
                "--unixctl=%s/vswitchd.ctl" % host.run,
                "--no-chdir",
            ],
        )
        self._ovs.append((host.run, host.ns))
        self._wait_ovs(host)
        # Integration bridge the driver plugs guests into, and the EVPN
        # bridge the connector owns. Both are --may-exist in production;
        # pre-creating them here just removes the guest-wiring-vs-connector
        # startup race (the connector still adds its vxlan port + flows).
        # br-int keeps OVS's permissive default protocol list exactly as the
        # driver's bare ``add-br`` would create it — the driver asks for
        # OpenFlow 1.3 per call, and a pinned bridge would break debugging
        # with a plain ``ovs-ofctl``.
        self._ovs_vsctl(host, ["--may-exist", "add-br", "br-int"])
        self._ovs_vsctl(
            host,
            [
                "--may-exist",
                "add-br",
                "evpn",
                "--",
                "set",
                "bridge",
                "evpn",
                "protocols=OpenFlow13",
                "fail-mode=secure",
            ],
        )

    def _wait_ovs(self, host, timeout=30):
        cmd = ["ip", "netns", "exec", host.ns, "env", "OVS_RUNDIR=%s" % host.run]
        nf_harness.wait_until(
            lambda: (
                subprocess.run(
                    cmd + ["ovs-vsctl", "show"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ).returncode
                == 0
            ),
            bool,
            timeout,
            lambda _: "OVS for %s never came up" % host.name,
            interval=0.5,
        )

    # --- process supervision ---------------------------------------------

    def start_fabric(self):
        rr = evpn.BgpRr(
            uuid=sys_uuid.uuid4(),
            as_number=AS_NUMBER,
            peer_prefixes=[UNDERLAY_PREFIX],
        )
        with open(os.path.join(self._workdir, "gobgp-rr.conf"), "w") as fl:
            fl.write(single.role_conf(rr, RR_IP))
        self._daemons.spawn(
            "gobgpd-rr",
            [
                "ip",
                "netns",
                "exec",
                self._rr_ns,
                self._gobgpd,
                "-f",
                os.path.join(self._workdir, "gobgp-rr.conf"),
                "--api-hosts",
                "127.0.0.1:%d" % GRPC_PORT,
            ],
        )
        for h in self.hosts:
            host = evpn.EvpnHost(
                uuid=sys_uuid.uuid4(), as_number=AS_NUMBER, rr_addresses=[RR_IP]
            )
            with open(h.gobgp_conf, "w") as fl:
                fl.write(single.role_conf(host, h.underlay_ip))
            # The connector config bakes in ``configs_dir`` (from
            # CLIENT_CONF_DIR) and ``switch_name`` (from EVPN_BRIDGE); point
            # them at this host's private conf dir before rendering so the
            # connector reads the client configs the driver writes there.
            # Its run directory too, so the two connectors keep their own
            # flow-sync scratch and anycast files.
            saved = (base.CLIENT_CONF_DIR, base.EVPN_BRIDGE, base.CONNECTOR_RUN_DIR)
            base.CLIENT_CONF_DIR, base.EVPN_BRIDGE = h.conf_dir, "evpn"
            base.CONNECTOR_RUN_DIR = h.run
            try:
                conf = host._render_connector_conf(h.underlay_ip)
            finally:
                base.CLIENT_CONF_DIR, base.EVPN_BRIDGE, base.CONNECTOR_RUN_DIR = saved
            # The connector does not propagate OVS_RUNDIR to its ovs-vsctl /
            # ovs-ofctl children, so it would program the *system* OVS. Point
            # it at per-host wrapper binaries that pin OVS_RUNDIR, so every
            # command it runs lands in this host's private OVS instance.
            vsctl_bin = self._ovs_wrapper(h, "ovs-vsctl")
            ofctl_bin = self._ovs_wrapper(h, "ovs-ofctl")
            conf = conf.replace(
                'ovs_vsctl_bin_path = "/usr/bin/ovs-vsctl"',
                'ovs_vsctl_bin_path = "%s"' % vsctl_bin,
            )
            conf = conf.replace(
                'ovs_ofctl_bin_path = "/usr/bin/ovs-ofctl"',
                'ovs_ofctl_bin_path = "%s"' % ofctl_bin,
            )
            with open(h.connector_conf, "w") as fl:
                fl.write(conf)
            self._daemons.spawn(
                "gobgpd-%s" % h.name,
                [
                    "ip",
                    "netns",
                    "exec",
                    h.ns,
                    self._gobgpd,
                    "-f",
                    h.gobgp_conf,
                    "--api-hosts",
                    "127.0.0.1:%d" % GRPC_PORT,
                ],
            )
            self._daemons.spawn(
                "connector-%s" % h.name,
                [
                    "ip",
                    "netns",
                    "exec",
                    h.ns,
                    "env",
                    "OVS_RUNDIR=%s" % h.run,
                    self._connector,
                    "--config-file",
                    h.connector_conf,
                ],
            )

    # --- guests -----------------------------------------------------------

    def add_guest(
        self,
        host,
        name,
        ipv4,
        vni,
        dhcp,
        retries=8,
        identity=0,
        security_rules=(),
    ):
        gns = "%s-%s" % (host.ns, name)
        host_leg = "%s%s" % (host.veth[:9], name)  # OVS port in host ns
        guest_leg = "%sg" % host_leg  # inside the guest ns
        _run(["ip", "netns", "add", gns])
        _run(["ip", "netns", "exec", gns, "ip", "link", "set", "lo", "up"])
        _run(["ip", "link", "add", host_leg, "type", "veth", "peer", "name", guest_leg])
        _run(["ip", "link", "set", host_leg, "netns", host.ns])
        _run(["ip", "link", "set", guest_leg, "netns", gns])
        _run(["ip", "netns", "exec", host.ns, "ip", "link", "set", host_leg, "up"])
        _run(["ip", "netns", "exec", gns, "ip", "link", "set", guest_leg, "up"])
        _run(
            [
                "ip",
                "netns",
                "exec",
                gns,
                "ip",
                "addr",
                "add",
                "%s/24" % ipv4,
                "dev",
                guest_leg,
            ]
        )
        _run(
            [
                "ip",
                "netns",
                "exec",
                gns,
                "ip",
                "link",
                "set",
                guest_leg,
                "mtu",
                str(dhcp["mtu"]),
            ]
        )
        mac = (
            _run(["ip", "netns", "exec", gns, "ip", "link", "show", guest_leg])
            .split("link/ether")[1]
            .split()[0]
        )
        port_uuid = sys_uuid.uuid4()
        # Plug the guest onto br-int in the host's OVS with the iface-id the
        # driver resolves by (what libvirt's openvswitch virtualport sets).
        self._ovs_vsctl(host, ["add-port", "br-int", host_leg])
        self._ovs_vsctl(
            host,
            ["set", "interface", host_leg, 'external_ids:iface-id="%s"' % port_uuid],
        )
        # Optional default route only if the CP subnet defines a gateway.
        gw = next((r["via"] for r in dhcp.get("routers", []) if r.get("via")), None)
        if gw:
            _run(
                [
                    "ip",
                    "netns",
                    "exec",
                    gns,
                    "ip",
                    "route",
                    "replace",
                    "default",
                    "via",
                    gw,
                ]
            )
        spec = {
            "uuid": str(port_uuid),
            "mac": mac,
            "ipv4": ipv4,
            "vni": vni,
            "imp_rt": ["%d:%d" % (AS_NUMBER, vni)],
            "exp_rt": ["%d:%d" % (AS_NUMBER, vni)],
            "dhcp": dhcp,
            "identity": identity,
            "security_rules": list(security_rules),
        }
        self._apply_port(host, spec, retries)
        g = {
            "ns": gns,
            "host_leg": host_leg,
            "ip": ipv4,
            "host": host,
            "spec": spec,
        }
        host.guests.append(g)
        return g

    def reapply_guest(self, guest, **changes):
        """Recompile a guest's port with something changed.

        What a reconcile does when the control plane moves a port between
        groups: the same port, applied again, with a different identity.
        """
        spec = dict(guest["spec"])
        spec.update(changes)
        guest["spec"] = spec
        self._apply_port(guest["host"], spec, 8)
        return guest

    def delete_guest(self, guest):
        """Take a port away through the driver, as a delete really arrives.

        The netns and the veth stay: what is under test is what the *fabric*
        does when a port is gone, and leaving the guest's kernel in place
        keeps "the route was withdrawn" apart from "the machine went away
        and nothing would answer anyway".
        """
        self._apply_port(guest["host"], guest["spec"], 3, verb="delete")
        guest["host"].guests.remove(guest)
        self._gone.append(guest)
        return guest

    def wait_host_rib_without(self, host, prefixes, timeout=60):
        """Block until the host's RIB no longer carries these prefixes."""
        return nf_harness.wait_until(
            lambda: self.host_rib(host),
            lambda rib: not any(p in rib for p in prefixes),
            timeout,
            lambda rib: (
                "host %s still carries %s in:\n%s"
                % (host.name, [p for p in prefixes if p in rib], rib)
            ),
        )

    def _apply_port(self, host, spec, retries, verb=None):
        cmd = (
            [
                "ip",
                "netns",
                "exec",
                host.ns,
                "env",
            ]
            + ["%s=%s" % (k, v) for k, v in host.env().items()]
            + [
                _sys_python(),
                "-m",
                "gcl_sdk.tests.integration.sdn_fabric._port_apply",
                json.dumps(spec),
            ]
            + ([verb] if verb else [])
        )
        last = None
        for _ in range(retries):
            r = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
            if r.returncode == 0:
                return
            last = r.stdout
            time.sleep(2)
        raise AssertionError(
            "driver failed to wire guest on %s:\n%s" % (host.name, last)
        )

    def _ovs_vsctl(self, host, args):
        return _run(
            [
                "ip",
                "netns",
                "exec",
                host.ns,
                "env",
                "OVS_RUNDIR=%s" % host.run,
                "ovs-vsctl",
            ]
            + args
        )

    def _ovs_wrapper(self, host, tool):
        """A per-host ``ovs-*`` shim that pins OVS_RUNDIR at this host's OVS."""
        path = os.path.join(host.run, tool)
        with open(path, "w") as fl:
            fl.write(
                "#!/bin/sh\n"
                'exec env OVS_RUNDIR=%s /usr/bin/%s "$@"\n' % (host.run, tool)
            )
        os.chmod(path, 0o755)
        return path

    def ping(self, guest, target, count=3, wait=2):
        r = subprocess.run(
            [
                "ip",
                "netns",
                "exec",
                guest["ns"],
                "ping",
                "-c",
                str(count),
                "-W",
                str(wait),
                target,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return r.returncode == 0

    # --- control-plane waits ----------------------------------------------

    def _gobgp_in(self, ns, args):
        return single.gobgp(self._gobgp, "127.0.0.1:%d" % GRPC_PORT, args, ns=ns)

    def wait_rr_peers(self, n, timeout=90):
        return nf_harness.wait_until(
            lambda: self._gobgp_in(self._rr_ns, ["neighbor"]),
            lambda out: out.count("Establ") >= n,
            timeout,
            lambda out: "RR never got %d peers:\n%s" % (n, out),
        )

    def host_rib(self, host):
        return self._gobgp_in(host.ns, ["global", "rib", "-a", "evpn"])

    def wait_host_rib(self, host, prefixes, timeout=120):
        return nf_harness.wait_until(
            lambda: self.host_rib(host),
            lambda rib: all(p in rib for p in prefixes),
            timeout,
            lambda rib: (
                "host %s missing %s in:\n%s"
                % (host.name, [p for p in prefixes if p not in rib], rib)
            ),
            interval=3,
        )

    def kill_rr(self):
        for name, proc, _ in self._daemons.procs:
            if name == "gobgpd-rr":
                proc.terminate()
                proc.wait(timeout=10)

    # --- cleanup ----------------------------------------------------------

    def cleanup(self):
        self._daemons.cleanup()
        for g in self._gone:
            _run(["ip", "netns", "del", g["ns"]], check=False)
        for h in self.hosts:
            for g in h.guests:
                _run(["ip", "netns", "del", g["ns"]], check=False)
            _run(["ip", "netns", "del", h.ns], check=False)
        _run(["ip", "netns", "del", self._rr_ns], check=False)
        _run(["ip", "link", "del", self._ubr], check=False)


def _sys_python():
    import sys

    return sys.executable
