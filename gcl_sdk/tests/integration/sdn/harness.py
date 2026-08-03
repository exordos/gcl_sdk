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

"""Fast, VM-less integration harness for the ovs_evpn SDN dataplane.

Real OVS + real network namespaces + the **real** agent code (the
``evpn`` driver's flow installers and the ``evpn_nf`` responder run as a
subprocess) — no VMs, no evpn_connector, so the NF layer (DHCP, DNS,
netboot proxy, security groups, per-guest patch/VLAN/metadata wiring, GC)
is exercised end-to-end in seconds. Forwarding through EVPN needs the
external ``evpn_connector`` and is out of scope here (that is the slow,
stand-based tier).

Each harness instance namespaces every OVS/netns object under a unique
prefix and tears them all down on ``cleanup()``, so tests are isolated and
leave nothing behind. Requires root, ``ovs-vsctl``/``ovs-ofctl``, ``ip``
and ``conntrack`` (the ``requires_sdn_env`` fixture skips otherwise).
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid as sys_uuid

from gcl_sdk.agents.universal import constants
from gcl_sdk.agents.universal.drivers import evpn
from gcl_sdk.agents.universal.drivers.evpn import base
from gcl_sdk.agents.universal.drivers.evpn import flows
from gcl_sdk.agents.universal.drivers.evpn import hostns

METADATA_IP = base.METADATA_IP

# The driver's paths as shipped, before any fabric repoints them.
_SHIPPED_PATHS = {
    name: value
    for name, value in vars(base).items()
    if name.isupper() and isinstance(value, str)
}


def host_paths(root):
    """Every path the driver writes on a host, moved under ``root``.

    Derived rather than listed: a path the driver later grows under the
    agent's work directory is isolated without anyone adding it here. These
    tiers run as root on a real host, which may run a live agent -- a path
    left behind edits its state (see `SdnHarness`).
    """
    paths = {"CLIENT_CONF_DIR": os.path.join(root, "vm_conf")}
    for name, value in _SHIPPED_PATHS.items():
        if value.startswith(constants.WORK_DIR):
            paths[name] = os.path.join(root, os.path.relpath(value, constants.WORK_DIR))
    return paths


def assert_isolated():
    """Fail loudly if any driver path still points at the real host."""
    live = [
        name
        for name in _SHIPPED_PATHS
        if str(getattr(base, name)).startswith(constants.WORK_DIR)
    ]
    assert not live, "driver paths left on the real host: %s" % live


def _run(cmd, check=True):
    """A command's stdout; a failure (with ``check``) fails the test."""
    out = subprocess.run(cmd, capture_output=True, text=True)
    if check and out.returncode:
        raise AssertionError("%s failed: %s" % (" ".join(cmd), out.stderr))
    return out.stdout


def wait_until(read, ready, timeout, fail=None, interval=2.0):
    """Poll ``read()`` until ``ready`` accepts what it returned; return that.

    Out of time: ``AssertionError(fail(last))``, or None without ``fail``.
    """
    deadline = time.time() + timeout
    value = None
    while time.time() < deadline:
        value = read()
        if ready(value):
            return value
        time.sleep(interval)
    if fail is None:
        return None
    raise AssertionError(fail(value))


def env_ready():
    """True if the host can run the SDN integration harness."""
    if os.geteuid() != 0:
        return False
    return all(shutil.which(b) for b in ("ovs-vsctl", "ovs-ofctl", "ip", "conntrack"))


class Guest:
    def __init__(self, harness, name, ns, host_veth, guest_dev, vlan, ipv4=None):
        self._h = harness
        self.name = name
        self.ns = ns
        self.host_veth = host_veth
        self.dev = guest_dev
        self.vlan = vlan
        self.ipv4 = ipv4  # address without the prefix, or None

    def exec(self, *args, check=True, timeout=15):
        return subprocess.run(
            ["ip", "netns", "exec", self.ns, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=check,
        ).stdout

    def popen(self, *args):
        """Start a background process inside the guest netns; caller kills it."""
        proc = subprocess.Popen(
            ["ip", "netns", "exec", self.ns, *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._h._procs.append(proc)
        return proc

    def tcp_connect(self, ip, port, wait=3):
        """True if a TCP connection to ip:port succeeds within ``wait`` s."""
        res = subprocess.run(
            [
                "ip",
                "netns",
                "exec",
                self.ns,
                "timeout",
                str(wait),
                "bash",
                "-c",
                "exec 3<>/dev/tcp/%s/%d" % (ip, port),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return res.returncode == 0

    def ping(self, ip, count=2, wait=2):
        res = subprocess.run(
            [
                "ip",
                "netns",
                "exec",
                self.ns,
                "ping",
                "-c",
                str(count),
                "-W",
                str(wait),
                ip,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return res.returncode == 0

    def http_get(self, url, wait=6):
        """Fetch a URL from inside the guest; returns (status, body)."""
        code = (
            "import urllib.request, urllib.error, sys\n"
            "try:\n"
            "    r = urllib.request.urlopen(sys.argv[1], timeout=%d)\n"
            "    print(r.status)\n"
            "    sys.stdout.write(r.read().decode(errors='replace'))\n"
            "except urllib.error.HTTPError as e:\n"
            "    print(e.code)\n"
            "except Exception as e:\n"
            "    print(0)\n"
            "    print(e)\n" % wait
        )
        out = self.exec("python3", "-c", code, url, check=False, timeout=wait + 6)
        lines = out.splitlines()
        status = int(lines[0]) if lines and lines[0].isdigit() else 0
        return status, "\n".join(lines[1:])

    def dhclient(self, wait=8):
        """Run a one-shot dhclient that gives up after ``wait`` seconds.

        ISC dhclient has no ``-timeout`` CLI flag — the timeout is a config
        directive, so we pass a per-guest ``-cf`` file. Per-guest lease/pid
        files keep runs and guests isolated. Returns the client's output;
        a lease shows as ``bound to <ip>``.
        """
        conf = "/tmp/dhclient-%s-%s.conf" % (self.ns, self.name)
        with open(conf, "w") as fl:
            fl.write("timeout %d;\n" % wait)
        try:
            # no -d: the client exits right after binding (or at the config
            # timeout on no offer); the backgrounded lease-holder dies with
            # the netns on cleanup.
            return self.exec(
                "dhclient",
                "-1",
                "-v",
                "-cf",
                conf,
                "-lf",
                "/tmp/dhclient-%s.lease" % self.ns,
                "-pf",
                "/tmp/dhclient-%s.pid" % self.ns,
                self.dev,
                check=False,
                timeout=wait + 8,
            )
        except subprocess.TimeoutExpired as e:
            out = e.output or b""
            return out.decode() if isinstance(out, bytes) else out
        finally:
            os.unlink(conf)

    @property
    def ofport(self):
        return _run(["ovs-vsctl", "get", "interface", self.host_veth, "ofport"]).strip()

    @property
    def mac(self):
        return self.exec("cat", "/sys/class/net/%s/address" % self.dev).strip()


class SdnHarness:
    """One isolated br-int test fabric with the real NF responder."""

    def __init__(self, workdir):
        tag = sys_uuid.uuid4().hex[:6]
        self.workdir = workdir
        self.br = "sb%s" % tag  # test br-int (<= 15 chars)
        paths = host_paths(workdir)
        self.records_dir = paths["NF_RECORDS_DIR"]
        self.metadata_port = 80  # the well-known one: the listener is namespaced
        os.makedirs(self.records_dir, exist_ok=True)
        self.evpn_br = "se%s" % tag  # stand-in for the evpn bridge (patches)
        self._guests = []
        self._procs = []
        self._vnis = set()
        self._saved = {}
        self.client_conf_dir = paths["CLIENT_CONF_DIR"]
        os.makedirs(self.client_conf_dir, exist_ok=True)
        self._egress_vnis = set()
        # Point the real driver's module constants at this test fabric, so
        # its flow installers act on our isolated bridge/ports/dirs.
        # PUBLISHED_FILE is here for a reason worth keeping written down: it
        # is not only isolation between tests. This tier runs as root on a
        # real host, and a hypervisor that also runs an agent has a live one
        # of these -- so a test that wrote to the real path would be editing
        # which addresses a production realm may reach, and would then lose
        # the race against that agent's next reconcile writing it back. That
        # is what it looked like from the outside: a published-address case
        # that failed about one run in three, on a host, for no reason
        # visible in its own code.
        overrides = dict(paths, BR_INT=self.br, EVPN_BRIDGE=self.evpn_br)
        for k, v in overrides.items():
            self._saved[k] = getattr(base, k)
            setattr(base, k, v)
        assert_isolated()
        self._reserve_host_vlans(base.VLAN_MAP_PATH)

    @staticmethod
    def _reserve_host_vlans(vlan_map_path):
        """Skip the local VLANs whose patch ports the host already has.

        The bridge is ours, but the driver names a guest's patch after its
        local VLAN (``pi<vlan>``/``pe<vlan>``) and port names are global to
        the OVS instance — so on a host that also runs an agent, the
        allocator's first choice is a name that already exists and the port
        cannot be created.
        """
        try:
            names = _run(["ovs-vsctl", "--bare", "--columns=name", "list", "Interface"])
        except Exception:
            return
        taken = set()
        for name in names.split():
            match = re.fullmatch(r"p[ie](\d+)", name)
            if match:
                taken.add(int(match.group(1)))
        if taken:
            with open(vlan_map_path, "w") as fl:
                json.dump({"host-%d" % v: v for v in sorted(taken)}, fl)

    # --- fabric -----------------------------------------------------------

    def setup(self):
        _run(["ovs-vsctl", "--may-exist", "add-br", self.br])
        _run(["ovs-vsctl", "--may-exist", "add-br", self.evpn_br])

    def ensure_vni(self, vni):
        """The VNI's namespace and its port, driver-style; returns the ofport."""
        self._vnis.add(vni)
        return hostns.ensure_vni_nf_port(vni)

    def apply_nf(self, guest, *kinds, vni=None, mac=None):
        """Install one guest's whole NF path, driver-style.

        The real ``_ensure_port_nf_intercepts``: what its functions steal
        and the return leg that brings the answers back. A guest whose port
        carries no ``dns`` function keeps its own resolver, which is exactly
        what this tier can prove.
        """
        vni = self.vni_of(guest) if vni is None else vni
        nf_ofport = self.ensure_vni(vni)
        if guest.ipv4:
            hostns.ensure_guest_neighbour(vni, guest.ipv4, mac or guest.mac)
        flows.ensure_port_nf_intercepts(
            {kind: {} for kind in kinds},
            guest.ofport,
            nf_ofport,
            mac or guest.mac,
            flows.port_cookie(guest.name, "nf"),
            zone=guest.vlan,
            sources=[guest.ipv4] if guest.ipv4 else [],
        )
        if self._procs:
            self.wait_for_worker(vni)

    def wait_for_worker(self, vni, timeout=10):
        """Block until the responder is serving this VNI inside its namespace.

        Production clients retry (DHCP by design, resolvers by timeout), so
        the daemon reaching a new VNI a second later is not a fault — but a
        test that asks once must not race it.
        """
        ns, iface = hostns.vni_ns(vni), hostns.nf_port(vni)
        found = wait_until(
            lambda: _run(
                ["ip", "netns", "exec", ns, "ss", "--packet", "-a"], check=False
            ),
            lambda out: iface in out,
            timeout,
            interval=0.3,
        )
        return found is not None

    def vni_of(self, guest):
        """This tier keys a VNI off the guest's VLAN — one guest, one of each."""
        return 16000 + guest.vlan

    def start_nf(self, zone_suffix="internal"):
        # The control plane seeds every network a `dns` function with the
        # installation's defaults; mirror that for the VNIs this fabric has
        # records for, unless the test already delivered its own.
        for vni in sorted(os.listdir(self.records_dir)):
            if not vni.isdigit():
                continue
            if os.path.exists(os.path.join(self.records_dir, vni, "nf-dns.json")):
                continue
            self.apply_nf_config(int(vni), "dns", zone_suffix=zone_suffix)
        # The production entry point, rescanning every second rather than
        # every five so a VNI a test adds later is served without a wait.
        cmd = [
            sys.executable,
            "-u",
            "-c",
            "import sys; from gcl_sdk.agents.universal.drivers import evpn_nf; "
            "evpn_nf.RESCAN_SECONDS = 1; sys.exit(evpn_nf.main())",
            "--records-dir",
            self.records_dir,
        ]
        # The worker's own output goes to a file rather than into the test
        # run's: a responder that dies of a traceback used to say so in the
        # middle of unrelated output, where it read as noise. Scanned at
        # teardown by `no_unexpected_logs`, and kept for the post-mortem.
        self.nf_log_path = os.path.join(self.workdir, "evpn-nf.log")
        log = open(self.nf_log_path, "w")
        self._procs.append(subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT))
        time.sleep(2.5)  # let the supervisor enter each namespace and bind

    # --- guests -----------------------------------------------------------

    def add_guest(self, name, vlan, ip=None, mac=None, gateway=None, nfs=(), vni=None):
        ns = "%s-%s" % (self.br, name)
        veth = "%s%s" % (self.br[:8], name)
        gdev = "%si" % veth
        _run(["ip", "netns", "add", ns])
        _run(["ip", "link", "add", veth, "type", "veth", "peer", "name", gdev])
        _run(["ip", "link", "set", gdev, "netns", ns])
        # vlan=0 leaves the port untagged (the fabric tier lets the real
        # EvpnPort driver assign the per-guest VLAN itself).
        cmd = ["ovs-vsctl", "add-port", self.br, veth]
        if vlan:
            cmd += ["--", "set", "port", veth, "tag=%d" % vlan]
        _run(cmd)
        _run(["ip", "link", "set", veth, "up"])
        _run(["ip", "-n", ns, "link", "set", "lo", "up"])
        if mac:
            _run(["ip", "-n", ns, "link", "set", gdev, "address", mac])
        _run(["ip", "-n", ns, "link", "set", gdev, "up"])
        if ip:
            _run(["ip", "-n", ns, "addr", "add", ip, "dev", gdev])
        if gateway:
            _run(["ip", "-n", ns, "route", "replace", "default", "via", gateway])
        g = Guest(self, name, ns, veth, gdev, vlan, ip.split("/")[0] if ip else None)
        self._guests.append(g)
        if nfs:
            self.apply_nf(g, *nfs, vni=vni)
        return g

    # --- records + real driver dataplane ----------------------------------

    def apply_nf_config(self, vni, kind, **config):
        """Deliver one function's settings to a VNI, driver-style.

        The real `EvpnNf`: what the function answers with lands beside the
        VNI's guest records, which is what separates it from who it answers
        for.
        """
        import uuid as _uuid

        evpn.EvpnNf(uuid=_uuid.uuid4(), kind=kind, vni=vni, config=config).dump_to_dp()

    def write_record(self, port_uuid, mac, ipv4, vni, **dhcp):
        import json

        vni_dir = os.path.join(self.records_dir, str(vni))
        os.makedirs(vni_dir, exist_ok=True)
        path = os.path.join(vni_dir, "%s.json" % port_uuid)
        rec = {"mac": mac, "ipv4": ipv4, "vni": vni, **dhcp}
        with open(path, "w") as fl:
            json.dump(rec, fl)
        os.utime(vni_dir)  # bump dir mtime so the cache reloads

    def apply_security_group(
        self,
        guest,
        rules,
        cookie="0x51c000000000aa",
        delivery_from=None,
        identity=0,
        delivery_ofport=None,
        sources=(),
        port_security=False,
        mac=None,
    ):
        """Install a guest's group, driver-style.

        ``delivery_from`` is the guest whose port stands in for the fabric
        patch traffic is delivered over — this tier has no patch pair, so a
        neighbour's port plays that role. ``delivery_ofport`` names that port
        directly, for a delivery leg that is not a guest at all (the VNI's
        routed gateway). Left unset, the ingress filter matches the
        destination MAC alone (the pre-scoping behaviour).

        ``sources`` + ``port_security`` are what the control plane says this
        guest holds, and pin every entry to it. ``mac`` overrides the address
        the flows are written against, which is how a port whose guest has
        since changed its own MAC is expressed: the control plane keeps
        saying what it issued.
        """
        if delivery_ofport is None and delivery_from is not None:
            delivery_ofport = delivery_from.ofport
        flows.ensure_security_group(
            guest.ofport,
            guest.vlan,
            rules,
            cookie,
            mac or guest.mac,
            delivery_ofport=delivery_ofport,
            identity=identity,
            sources=sources,
            port_security=port_security,
        )

    def ofport_of(self, iface):
        """ofport of an interface this fabric created (the gateway leg)."""
        return _run(["ovs-vsctl", "get", "interface", iface, "ofport"]).strip()

    def wait_for_listener(self, vni, port, timeout=15):
        """Block until the VNI's proxy holds a TCP port inside its namespace.

        The listeners are opened by threads that retry, and the worker itself
        restarts until the namespace exists — so "the daemon is running" is
        not "the port is answering". A test that drives traffic before the
        bind measures the bind, not what it came to measure.
        """
        ns = hostns.vni_ns(vni)
        found = wait_until(
            lambda: _run(["ip", "netns", "exec", ns, "ss", "-tlnH"], check=False),
            lambda out: any(":%d " % port in ln for ln in out.splitlines()),
            timeout,
            interval=0.3,
        )
        return found is not None

    def dump_flows(self):
        """This fabric's flow table, as ovs-ofctl prints it."""
        return _run(["ovs-ofctl", "-O", base.OPENFLOW_VERSION, "dump-flows", self.br])

    def clear_security_group(self, cookie="0x51c000000000aa"):
        _run(["ovs-ofctl", "del-flows", self.br, "cookie=%s/-1" % cookie])

    def flows_of(self, cookie):
        """The flows carrying one cookie, as the bridge prints them.

        Compared before and after a membership change: the promise of a set
        shipped as its own resource is that a member joining moves nothing
        of any port's.
        """
        out = _run(
            [
                "ovs-ofctl",
                "-O",
                "OpenFlow13",
                "dump-flows",
                self.br,
                "cookie=%s/-1" % cookie,
            ]
        )
        return sorted(
            re.sub(
                r"(duration|n_packets|n_bytes|idle_age|hard_age)=\S+", "", line
            ).strip()
            for line in out.splitlines()
            if "cookie=" in line
        )

    def apply_metadata(self, guest, cidr, vni=None, forwards=()):
        """Wire the metadata/netboot path for a guest, driver-style.

        The address lives on the VNI's port inside its namespace, so there
        is no policy routing to mirror: the punt and the return leg are the
        guest's own NF flows (`apply_nf`), and the guest gets an on-link
        route + static neighbour for the metadata address
        (evpn_connector's ARP proxy in production).
        """
        vni = self.vni_of(guest) if vni is None else vni
        # The driver writes a record for a port carrying any of the three
        # functions, and the record directory is what tells the responder
        # which VNIs to serve — so a proxy-only guest has one too.
        self.write_record(
            guest.name,
            guest.mac,
            guest.ipv4,
            vni,
            cidr=cidr,
            dhcp_enabled=False,
            dns_enabled=False,
        )
        self.apply_nf_config(vni, "proxy", forwards=list(forwards or []))
        self.apply_nf(guest, "proxy", vni=vni)
        guest.exec(
            "ip", "route", "replace", "%s/32" % base.METADATA_IP, "dev", guest.dev
        )
        guest.exec(
            "ip",
            "neigh",
            "replace",
            base.METADATA_IP,
            "lladdr",
            base.METADATA_MAC,
            "dev",
            guest.dev,
        )
        # the supervisor rescans for VNIs every second; wait for this one's
        # listener to bind inside its namespace before driving traffic.
        want = "%s:%d" % (base.METADATA_IP, self.metadata_port)
        ns = hostns.vni_ns(vni)
        for _ in range(20):
            out = _run(["ip", "netns", "exec", ns, "ss", "-tlnH"], check=False)
            if any(want in ln for ln in out.splitlines()):
                return
            time.sleep(0.5)

    def apply_egress(self, guest, vni, cidr, gw_ip):
        """Wire the per-VNI DVR egress for a guest, driver-style.

        Runs the real ``_ensure_vni_egress`` (per-VNI namespace, gateway leg,
        transfer /30, double masquerade, gw client config). The leg lands
        on the stand-in evpn bridge; in production evpn_connector's L3
        pipeline delivers guest traffic there — this VM-less tier emulates
        that delivery by re-plugging the leg into br-int on the guest's
        VLAN (plain L2, the gateway answers ARP itself).
        """
        # One call is one of the agent's iterations, and the agent forgets
        # the per-VNI memo at the start of each. Without this a second apply
        # skips everything the memo guards, so a test that removes one of
        # those rules and re-applies passes while measuring nothing.
        hostns._GUARDED_VNIS.discard(vni)
        hostns.ensure_vni_egress(
            vni,
            {
                "cidr": cidr,
                "routers": [{"to": "0.0.0.0/0", "via": gw_ip}],
                "mtu": 1450,
            },
            ["65001:%d" % vni],
        )
        self._egress_vnis.add(vni)
        gw_br = "gw%d-br" % vni
        current = _run(["ovs-vsctl", "port-to-br", gw_br], check=False).strip()
        if current != self.br:
            _run(["ovs-vsctl", "--if-exists", "del-port", self.evpn_br, gw_br])
            _run(
                [
                    "ovs-vsctl",
                    "add-port",
                    self.br,
                    gw_br,
                    "--",
                    "set",
                    "port",
                    gw_br,
                    "tag=%d" % guest.vlan,
                ]
            )
        guest.exec("ip", "route", "replace", "default", "via", gw_ip)

    # --- cleanup ----------------------------------------------------------

    def cleanup(self):
        for vni in self._egress_vnis | self._vnis:
            try:
                hostns.gc_vni(vni)
            except Exception:
                pass
        for p in self._procs:
            try:
                p.send_signal(signal.SIGKILL)
                p.wait(timeout=3)
            except Exception:
                pass
        for g in self._guests:
            _run(["ip", "netns", "del", g.ns], check=False)
            _run(["ip", "link", "del", g.host_veth], check=False)
        _run(["ovs-vsctl", "--if-exists", "del-br", self.br], check=False)
        _run(["ovs-vsctl", "--if-exists", "del-br", self.evpn_br], check=False)
        for k, v in self._saved.items():
            setattr(base, k, v)
