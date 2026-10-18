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

import json
import contextlib
import os
from pathlib import Path
import subprocess
import stat
from unittest import mock
import uuid as sys_uuid

import pytest

from gcl_sdk.agents.universal.drivers import evpn
from gcl_sdk.agents.universal.drivers import evpn_nf
from gcl_sdk.agents.universal.drivers import evpn_proxy
from gcl_sdk.agents.universal.drivers import evpn_records
from gcl_sdk.agents.universal.drivers import exceptions as driver_exc
from gcl_sdk.agents.universal.drivers.evpn import base
from gcl_sdk.agents.universal.drivers.evpn import driver as evpn_driver
from gcl_sdk.agents.universal.drivers.evpn import fabric
from gcl_sdk.agents.universal.drivers.evpn import flows as evpn_flows
from gcl_sdk.agents.universal.drivers.evpn import hostns
from gcl_sdk.agents.universal.drivers.evpn import nf
from gcl_sdk.agents.universal.drivers.evpn import sh
from gcl_sdk.agents.universal.drivers.evpn import state

STOCK_NFS = [
    {"kind": "dhcp", "config": {}},
    {"kind": "dns", "config": {"forwarders": [], "zone_suffix": "internal"}},
    {"kind": "proxy", "config": {"forwards": []}},
]


def _port(**kwargs):
    defaults = {
        "uuid": sys_uuid.uuid4(),
        "mac": "52:54:00:aa:bb:cc",
        "ipv4": "10.42.0.7",
        "vni": 10001,
        "imp_rt": ["65001:10001"],
        "exp_rt": ["65001:10001"],
        "dhcp": {"routers": [], "dns_servers": ["10.42.0.254"], "mtu": 1450},
        "nfs": list(STOCK_NFS),
    }
    defaults.update(kwargs)
    return evpn.EvpnPort(**defaults)


def _dp(**facts):
    """A snapshot of the host: these facts, every other one unknown."""
    if facts.get("cookies") is not None:
        facts["cookies"] = {
            int(cookie, 16) if isinstance(cookie, str) else cookie
            for cookie in facts["cookies"]
        }
    return {**state.dp_defaults(), **facts}


@contextlib.contextmanager
def _agent_cycle():
    agent = object.__new__(evpn.EvpnAgentCapabilityDriver)
    agent._storage = mock.Mock()
    with mock.patch.object(state, "snapshot_dataplane", side_effect=_dp):
        agent.start()
        try:
            yield
        finally:
            agent.finalize()


def test_revoked_rules_build_membership_set_once(dirs):
    previous = [{"proto": "tcp", "port": port} for port in range(80, 84)]
    Path(base.SG_RULES_PATH).write_text(
        json.dumps({"guest": evpn_flows._canonical_rules(previous)})
    )
    with (
        mock.patch.object(evpn_flows, "set", wraps=set, create=True) as sets,
        mock.patch.object(sh, "appctl"),
    ):
        evpn_flows.cut_revoked_sessions("guest", 42, previous[:1], "10.42.0.7")
        sets.assert_called_once()


def test_nf_shared_port_and_route_are_prepared_once_per_cycle(dirs):
    calls = []
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run(calls=calls)):
        for _ in range(2):
            with _agent_cycle():
                for guest in range(4):
                    assert hostns.ensure_vni_nf_port(7) == 1001
                    hostns.ensure_guest_neighbour(
                        7, "10.42.0.%d" % (guest + 1), "52:54:00:aa:bb:cc"
                    )
    assert sum("add-port" in cmd and hostns.nf_port(7) in cmd for cmd in calls) == 2
    assert sum(cmd[-2:] == ["rule", "show"] for cmd in calls) == 2
    assert sum("neigh" in cmd and "replace" in cmd for cmd in calls) == 8


def test_nf_port_cache_retries_failed_setup_and_discards_collected_vni(dirs):
    calls = []
    with (
        mock.patch.object(sh, "run", side_effect=_two_bridge_run(calls=calls)),
        mock.patch.object(hostns, "ensure_metadata_route_rule") as metadata,
    ):
        with _agent_cycle():
            metadata.side_effect = subprocess.CalledProcessError(1, "ip rule")
            with pytest.raises(subprocess.CalledProcessError):
                hostns.ensure_vni_nf_port(7)
            assert 7 not in hostns._NF_PORTS
            metadata.side_effect = None
            hostns.ensure_vni_nf_port(7)
            hostns.ensure_vni_nf_port(7)
            hostns.gc_vni(7)
            assert 7 not in hostns._NF_PORTS
            hostns.ensure_vni_nf_port(7)
        assert sum("add-port" in cmd and hostns.nf_port(7) in cmd for cmd in calls) == 3
        assert hostns._NF_PORTS is None


def test_failed_responder_apply_invalidates_healthy_restore_cache(dirs):
    Path(base.NF_UNIT_PATH).write_text("unit")
    with (
        mock.patch.object(nf, "render_nf_unit", return_value="unit"),
        mock.patch.object(sh, "write_if_changed", return_value=True),
        mock.patch.object(sh, "run") as run,
    ):
        with _agent_cycle():
            assert nf.missing_nf_responder() == []
            run.side_effect = [None, None, subprocess.CalledProcessError(1, "restart")]
            with pytest.raises(subprocess.CalledProcessError):
                nf.ensure_nf_responder()
            assert "nf responder" in nf.missing_nf_responder()
            run.side_effect = None
            nf.ensure_nf_responder()
            assert nf.missing_nf_responder() == []


def test_responder_restore_shares_unit_reads_and_refreshes_after_repair(dirs):
    Path(base.NF_UNIT_PATH).write_text("unit")
    with (
        mock.patch.object(nf, "render_nf_unit", return_value="unit") as render,
        mock.patch.object(sh, "run"),
        mock.patch.object(sh, "try_run"),
    ):
        for _ in range(2):
            with _agent_cycle():
                for _ in range(4):
                    assert nf.missing_nf_responder() == []
        assert render.call_count == 2
        with _agent_cycle():
            nf.teardown_nf_responder()
            assert nf.missing_nf_responder()
            nf.ensure_nf_responder()
            assert nf.missing_nf_responder() == []


def test_vni_occupancy_reads_client_records_once_and_tracks_changes(dirs):
    ports = [_port(vni=10001), _port(vni=10002)]
    for port in ports:
        sh.write_atomic(port._client_conf_path, json.dumps({"vni": port.vni}))
    load = json.load
    reads = []

    def counted_load(stream):
        if str(stream.name).startswith(base.CLIENT_CONF_DIR):
            reads.append(stream.name)
        return load(stream)

    with (
        mock.patch.object(json, "load", side_effect=counted_load),
        mock.patch.object(sh, "run", side_effect=_two_bridge_run()),
    ):
        with _agent_cycle():
            for _ in range(4):
                assert hostns.vni_in_use(10001, "other")
                assert not hostns.vni_in_use(10001, ports[0].uuid)
            assert len(reads) == 2
            ports[0].delete_from_dp()
            assert not hostns.vni_in_use(10001, "other")
            ports[0].vni = 10002
            ports[0].dump_to_dp()
            assert hostns.vni_in_use(10002, ports[1].uuid)


def test_cookie_lookup_uses_numeric_set_without_scanning(monkeypatch):
    class Cookies(set):
        def __iter__(self):
            raise AssertionError("cookie lookup scanned all flows")

    monkeypatch.setattr(state, "_DP_SNAPSHOT", {**_dp(), "cookies": Cookies({42})})
    assert state.has_cookie("0x000000000000002a")
    assert not state.has_cookie("0x000000000000002b")
    monkeypatch.setattr(state, "_DP_SNAPSHOT", _dp(cookies=None))
    assert state.has_cookie("0x2a")


def test_nf_responder_is_ensured_once_per_cycle(dirs):
    agent = object.__new__(evpn.EvpnAgentCapabilityDriver)
    agent._storage = mock.Mock()
    with (
        mock.patch.object(state, "snapshot_dataplane", side_effect=_dp),
        mock.patch.object(nf, "render_nf_unit", return_value="unit") as render,
        mock.patch.object(sh, "run") as run,
    ):
        for _ in range(2):
            agent.start()
            try:
                for _ in range(4):
                    nf.ensure_nf_responder()
            finally:
                agent.finalize()
        assert render.call_count == 2
        assert (
            run.call_args_list.count(
                mock.call([base.SYSTEMCTL_BIN, "enable", "--now", base.NF_SERVICE])
            )
            == 2
        )
        nf.ensure_nf_responder()
        nf.ensure_nf_responder()
        assert render.call_count == 4


@pytest.mark.parametrize("failed_command", [0, 1, 2])
def test_failed_nf_unit_apply_is_retried_in_next_cycle(dirs, failed_command):
    agent = object.__new__(evpn.EvpnAgentCapabilityDriver)
    agent._storage = mock.Mock()
    with (
        mock.patch.object(state, "snapshot_dataplane", side_effect=_dp),
        mock.patch.object(nf, "render_nf_unit", return_value="unit"),
        mock.patch.object(sh, "run") as run,
    ):
        agent.start()
        try:
            run.side_effect = [None] * failed_command + [
                subprocess.CalledProcessError(1, "systemctl")
            ]
            with pytest.raises(subprocess.CalledProcessError):
                nf.ensure_nf_responder()
        finally:
            agent.finalize()
        run.reset_mock(side_effect=True)
        agent.start()
        try:
            nf.ensure_nf_responder()
            nf.ensure_nf_responder()
            assert run.call_args_list == [
                mock.call([base.SYSTEMCTL_BIN, "daemon-reload"]),
                mock.call([base.SYSTEMCTL_BIN, "enable", "--now", base.NF_SERVICE]),
                mock.call([base.SYSTEMCTL_BIN, "restart", base.NF_SERVICE]),
            ]
        finally:
            agent.finalize()


@pytest.mark.parametrize("action", ["teardown", "failed_restart"])
def test_nf_responder_can_be_repaired_again_in_same_cycle(dirs, action):
    agent = object.__new__(evpn.EvpnAgentCapabilityDriver)
    agent._storage = mock.Mock()
    with (
        mock.patch.object(state, "snapshot_dataplane", side_effect=_dp),
        mock.patch.object(nf, "render_nf_unit", return_value="unit"),
        mock.patch.object(sh, "run") as run,
        mock.patch.object(sh, "try_run"),
    ):
        agent.start()
        try:
            if action == "teardown":
                nf.ensure_nf_responder()
                nf.teardown_nf_responder()
            else:
                run.side_effect = [
                    None,
                    None,
                    subprocess.CalledProcessError(1, "restart"),
                ]
                with pytest.raises(subprocess.CalledProcessError):
                    nf.ensure_nf_responder()
                run.side_effect = None
            nf.ensure_nf_responder()
            nf.ensure_nf_responder()
            assert (
                run.call_args_list.count(
                    mock.call([base.SYSTEMCTL_BIN, "enable", "--now", base.NF_SERVICE])
                )
                == 2
            )
            if action == "failed_restart":
                assert (
                    run.call_args_list.count(
                        mock.call([base.SYSTEMCTL_BIN, "restart", base.NF_SERVICE])
                    )
                    == 2
                )
        finally:
            agent.finalize()


def _host(**kwargs):
    defaults = {
        "uuid": sys_uuid.uuid4(),
        "as_number": 65001,
        "rr_addresses": ["10.77.0.10"],
    }
    defaults.update(kwargs)
    return evpn.EvpnHost(**defaults)


def _rr(**kwargs):
    defaults = {
        "uuid": sys_uuid.uuid4(),
        "as_number": 65001,
        "peer_prefixes": ["10.77.0.0/24"],
    }
    defaults.update(kwargs)
    return evpn.BgpRr(**defaults)


@pytest.fixture
def dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(base, "CLIENT_CONF_DIR", str(tmp_path / "vm_conf"))
    monkeypatch.setattr(base, "NF_RECORDS_DIR", str(tmp_path / "nf"))
    monkeypatch.setattr(base, "EGRESS_RECORDS_DIR", str(tmp_path / "egress"))
    monkeypatch.setattr(base, "GOBGP_CONF_FILE", str(tmp_path / "gobgp.conf"))
    monkeypatch.setattr(base, "GOBGP_PARTS_DIR", str(tmp_path / "gobgp.d"))
    monkeypatch.setattr(base, "GOBGP_UNIT_PATH", str(tmp_path / "gobgpd.service"))
    monkeypatch.setattr(fabric, "_GOBGP_UNIT_PENDING", False)
    monkeypatch.setattr(fabric, "gobgp_api_ready", lambda: True)
    Path(base.GOBGP_UNIT_PATH).write_text(fabric.render_gobgp_unit())
    monkeypatch.setattr(base, "VLAN_MAP_PATH", str(tmp_path / "vlan_map.json"))
    monkeypatch.setattr(base, "FLOW_BUNDLE_FILE", str(tmp_path / "flow_bundle.tmp"))
    monkeypatch.setattr(base, "PUBLISHED_FILE", str(tmp_path / "published.json"))
    monkeypatch.setattr(base, "SG_RULES_PATH", str(tmp_path / "sg_rules.json"))
    monkeypatch.setattr(base, "NF_UNIT_PATH", str(tmp_path / "nf.service"))
    monkeypatch.setattr(
        base, "CONNECTOR_CONF_FILE", str(tmp_path / "evpn_connector.cfg")
    )
    monkeypatch.setattr(base, "CONNECTOR_RUN_DIR", str(tmp_path / "run_evpn"))
    # An empty snapshot, so a check that finds no fact of its own never
    # reaches out to the machine running the tests — and never reports
    # missing what it could not read there.
    monkeypatch.setattr(state, "_DP_SNAPSHOT", state.dp_defaults())
    monkeypatch.setattr(nf, "_RESPONDER_READY", None)
    monkeypatch.setattr(nf, "_RESPONDER_UNIT_PENDING", False)
    # The egress guard is asserted once per VNI per iteration; a test is an
    # iteration of its own.
    hostns._GUARDED_VNIS.clear()
    return tmp_path


def test_render_client_conf_shape():
    port = _port()
    conf = json.loads(port._render_client_conf(1001))
    assert conf == {
        "cfg_type": "l3",
        "mac": "52:54:00:aa:bb:cc",
        "routes": ["10.42.0.7/32"],
        "vni": 10001,
        "ofport": 1001,
        "type": "flat",
        "tag": 0,
        "imp_rt": ["65001:10001"],
        "exp_rt": ["65001:10001"],
    }


def _two_bridge_run(guest_port="vethg", patch_ofport="1001", calls=None, bundles=None):
    """A _run mock aware of the two-bridge calls: guest name lookup on
    br-int vs the VNI patch ofport on the evpn bridge.

    ``bundles`` collects the lines of each flow bundle as it is submitted:
    they all pass through one scratch file, so a caller reading that file
    afterwards would only ever see the last one.
    """

    def run(cmd):
        if calls is not None:
            calls.append(cmd)
        if cmd[:3] in (
            [base.IP_BIN, "-json", "route"],
            [base.IP_BIN, "-json", "addr"],
        ):
            return "[]"
        if "netns" in cmd and "exec" in cmd and "rule" in cmd and "show" in cmd:
            return ""  # a fresh namespace has only the three default rules
        if "-json" in cmd and "link" in cmd and cmd[-1].startswith("gw"):
            return json.dumps([{"ifname": cmd[-1], "flags": ["UP"]}])
        if "netns" in cmd and "exec" in cmd and "show" in cmd:
            # A freshly created port is in the root namespace, so asking the
            # VNI's namespace about it fails — which is what makes the
            # driver move it there.
            raise subprocess.CalledProcessError(1, cmd)
        if bundles is not None and "bundle" in cmd:
            with open(cmd[-1]) as fl:
                bundles.append(fl.read().splitlines())
        if "find" in cmd and "Interface" in cmd:
            return guest_port + "\n"  # guest plugged into br-int
        if "get" in cmd and "interface" in cmd and cmd[-1] == "ofport":
            return patch_ofport + "\n"  # pe<vni> ofport on the evpn bridge
        return ""  # add-br / add-port / set port tag

    return run


def test_port_dump_points_config_at_the_vni_patch(dirs):
    port = _port()
    calls = []
    with mock.patch.object(
        sh,
        "run",
        side_effect=_two_bridge_run(
            guest_port="vethg", patch_ofport="1001", calls=calls
        ),
    ):
        port.dump_to_dp()

    client = json.loads((dirs / "vm_conf" / ("%s.json" % port.uuid)).read_text())
    # evpn_connector must see the patch ofport, not the guest's own.
    assert client["ofport"] == 1001
    record = json.loads(
        (dirs / "nf" / str(port.vni) / ("%s.json" % port.uuid)).read_text()
    )
    assert record["ipv4"] == "10.42.0.7"
    assert record["mtu"] == 1450
    # The guest port and its own per-guest patch are tagged into the same
    # local VLAN, and the NF record carries that VLAN for DNS zone scoping.
    guest_tag = next(
        c for c in calls if c[:3] == ["ovs-vsctl", "set", "port"] and c[3] == "vethg"
    )
    vlan = guest_tag[4]  # tag=<n>
    assert vlan.startswith("tag=")
    vlan_num = int(vlan.split("=")[1])
    # per-guest patch is keyed by the guest's VLAN (pi<vlan>), not the VNI
    assert any("pi%d" % vlan_num in c and vlan in c for c in calls)
    # the record names its VNI: that is the namespace it will be served in
    assert record["vni"] == port.vni
    assert port.status == "ACTIVE"


def test_the_metadata_path_lives_in_the_vni_namespace(dirs):
    """No policy routing and no per-guest table: the address is held by the
    VNI's port inside its namespace, so overlapping guest CIDRs are kept
    apart by the namespace itself."""
    port = _port(dhcp={"cidr": "10.42.0.0/24", "mtu": 1450})
    calls, bundles = [], []
    with mock.patch.object(
        sh, "run", side_effect=_two_bridge_run(calls=calls, bundles=bundles)
    ):
        port.dump_to_dp()
    ns = hostns.vni_ns(port.vni)
    nf_iface = hostns.nf_port(port.vni)
    # the metadata address is configured inside the namespace, on that port
    assert any(
        c[:4] == [base.IP_BIN, "netns", "exec", ns]
        and "addr" in c
        and "%s/32" % base.METADATA_IP in c
        and nf_iface in c
        for c in calls
    )
    # the guest's own neighbour is programmed there, not discovered by ARP
    assert any(
        c[:4] == [base.IP_BIN, "netns", "exec", ns] and "neigh" in c and port.mac in c
        for c in calls
    )
    # ...and the host route that goes with it is the responder's alone: in
    # its own table, reached by a rule matching the responder's address. In
    # the main table it would also catch the replies to whatever the guest
    # opens outward, which then bypass the ingress conntrack pipeline and
    # get the guest's next packet dropped as invalid.
    assert any(
        c[:4] == [base.IP_BIN, "netns", "exec", ns]
        and "route" in c
        and "%s/32" % port.ipv4 in c
        and c[-2:] == ["table", hostns.GUEST_ROUTE_TABLE]
        for c in calls
    )
    assert any(
        c[:4] == [base.IP_BIN, "netns", "exec", ns]
        and "rule" in c
        and base.METADATA_IP in c
        and hostns.GUEST_ROUTE_TABLE in c
        for c in calls
    )
    # per-guest punt to the metadata address, in the guest's NF bundle
    lines = [ln for b in bundles for ln in b]
    punt = [ln for ln in lines if base.METADATA_IP in ln and "tp_dst=80" in ln]
    assert punt and "mod_dl_dst:%s" % base.METADATA_MAC in punt[0]
    assert evpn_flows.port_cookie(port.uuid, "nf") in punt[0]
    # ... and the answers come back on this guest's own return leg
    assert any("dl_dst=%s" % port.mac in ln and "actions=output:" in ln for ln in lines)

    # delete removes the whole NF path by that one cookie
    dels = []
    with mock.patch.object(sh, "run", side_effect=lambda c: dels.append(c) or ""):
        port.delete_from_dp()
    assert any(
        c[0] == base.OVSOFCTL_BIN
        and "del-flows" in c
        and evpn_flows.port_cookie(port.uuid, "nf") in c[-1]
        for c in dels
    )


def test_guest_gc_is_per_port(dirs):
    # Each guest owns its patch/VLAN/md, so two ports in the same VNI get
    # DISTINCT VLANs (evpn_connector rejects a shared patch ofport), and
    # deleting one tears down only its own dataplane.
    p1 = _port(uuid=sys_uuid.uuid4(), vni=10005)
    p2 = _port(uuid=sys_uuid.uuid4(), vni=10005)
    for p in (p1, p2):
        with mock.patch.object(sh, "run", side_effect=_two_bridge_run()):
            p.dump_to_dp()
    v1 = hostns.lookup_local_vlan(p1.uuid)
    v2 = hostns.lookup_local_vlan(p2.uuid)
    assert v1 is not None and v2 is not None and v1 != v2

    # deleting p1 removes p1's own patch pair + md + frees p1's VLAN only
    calls = []
    with mock.patch.object(sh, "run", side_effect=lambda c: calls.append(c) or ""):
        p1.delete_from_dp()
    assert any("pi%d" % v1 in c for c in calls)
    assert any("pe%d" % v1 in c for c in calls)
    assert not any("pi%d" % v2 in c or "pe%d" % v2 in c for c in calls)
    assert hostns.lookup_local_vlan(p1.uuid) is None  # freed
    assert hostns.lookup_local_vlan(p2.uuid) == v2  # untouched


def test_a_lost_vlan_map_hands_out_no_vlan_ovs_holds(dirs):
    """The map is the agent's memory, OVS is the fact. A map lost or zeroed
    by a power cut read as empty, and numbering from 1 again gave a new
    guest the VLAN -- and the ct zone -- of a guest already wired."""

    def run(cmd):
        if cmd[-2:] == ["list", "Port"]:
            return "1\n\n2\n\n\n4\n"  # untagged ports print nothing
        return ""

    with open(base.VLAN_MAP_PATH, "w") as fl:
        fl.write("\0" * 32)  # what a rename without a sync leaves behind
    with mock.patch.object(sh, "run", side_effect=run):
        first = hostns.local_vlan_for_port("a")
        second = hostns.local_vlan_for_port("b")
    assert (first, second) == (3, 5)

    # An OVS that cannot be asked is not an empty one: nothing is handed out.
    def broken(cmd):
        raise subprocess.CalledProcessError(1, cmd)

    with mock.patch.object(sh, "run", side_effect=broken):
        with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
            hostns.local_vlan_for_port("c")
    assert hostns.lookup_local_vlan("c") is None


def test_a_released_vlan_is_the_last_one_handed_out_again(dirs):
    """A guest's patch is named after its VLAN, and OVS gives a name back the
    ofport it had. While the fabric is in fail-static, the connector keeps
    the gone guest's flows, still outputting to that ofport: the next guest
    to get the number received the old one's traffic."""
    with mock.patch.object(sh, "run", side_effect=lambda cmd: ""):
        first = hostns.local_vlan_for_port("a")
        second = hostns.local_vlan_for_port("b")
        hostns.release_local_vlan("a")
        third = hostns.local_vlan_for_port("c")
        assert third not in (first, second)
        # ... and only after every other number was taken does it come back
        with open(base.VLAN_MAP_PATH) as fl:
            vlan_map = json.load(fl)
        vlan_map.update({"x%d" % v: v for v in range(third + 1, 4095)})
        with open(base.VLAN_MAP_PATH, "w") as fl:
            json.dump(vlan_map, fl)
        assert hostns.local_vlan_for_port("d") == first
    assert hostns.lookup_local_vlan("b") == second


def test_state_files_are_synced_before_the_rename(dirs):
    path = str(dirs / "state.json")
    synced = []
    real_fsync = os.fsync

    def fsync(fd):
        synced.append(os.path.exists(path))
        real_fsync(fd)

    with mock.patch.object(os, "fsync", side_effect=fsync):
        sh.write_atomic(path, "{}")
    assert synced == [False]
    with open(path) as fl:
        assert fl.read() == "{}"


_ID = {"direction": "ingress", "proto": "any", "remote_identity": None}


@pytest.mark.parametrize(
    "rule,expected",
    [
        pytest.param({"proto": "tcp", "port": 22}, "tcp,tp_dst=22", id="tcp-port"),
        pytest.param({"proto": "icmp"}, "icmp", id="icmp"),
        pytest.param({"proto": "any"}, "ip", id="any"),
        pytest.param(
            {"proto": "any", "remote": "10.0.0.0/24"},
            "ip,nw_dst=10.0.0.0/24",
            id="remote",
        ),
        # `remote` is the far end: the destination of an egress rule, the
        # source of an ingress one.
        pytest.param(
            {"direction": "egress", "proto": "tcp", "remote": "10.0.0.0/24"},
            "tcp,nw_dst=10.0.0.0/24",
            id="remote-egress",
        ),
        pytest.param(
            {"direction": "ingress", "proto": "tcp", "remote": "10.0.0.0/24"},
            "tcp,nw_src=10.0.0.0/24",
            id="remote-ingress",
        ),
        # A port only applies to tcp/udp; unknown protos grant nothing.
        pytest.param({"proto": "icmp", "port": 22}, "icmp", id="icmp-ignores-port"),
        pytest.param({"proto": "sctp"}, None, id="unknown-proto"),
        # ovs-ofctl refuses `ip,nw_dst=<v6>` and with it the whole bundle.
        pytest.param(
            {"direction": "egress", "proto": "any", "remote": "2001:db8::/32"},
            None,
            id="v6-egress",
        ),
        pytest.param(
            {"direction": "ingress", "proto": "any", "remote": "2001:db8::/32"},
            None,
            id="v6-ingress",
        ),
        # The value lands in a flow spec, where a comma starts another field.
        pytest.param(
            {
                "direction": "egress",
                "proto": "tcp",
                "remote": "10.0.0.0/8,actions=NORMAL",
            },
            None,
            id="forged-remote",
        ),
        # The mark is the sender's: "from that group" is answerable, "to that
        # group" is not, and grants nothing rather than everything.
        pytest.param(
            {
                "direction": "ingress",
                "proto": "tcp",
                "port": 5432,
                "remote_identity": 42,
            },
            "tcp,pkt_mark=42/42,tp_dst=5432",
            id="identity-ingress",
        ),
        pytest.param(
            {"direction": "egress", "proto": "tcp", "remote_identity": 42},
            None,
            id="identity-egress",
        ),
        pytest.param(dict(_ID, remote_identity=0), None, id="identity-0"),
        pytest.param(dict(_ID, remote_identity=70000), None, id="identity-too-big"),
        pytest.param(dict(_ID, remote_identity="web"), None, id="identity-name"),
        pytest.param(dict(_ID, remote_identity="x"), None, id="identity-letter"),
    ],
)
def test_compile_sg_match(rule, expected):
    assert evpn_flows.compile_sg_match(rule) == expected


def test_a_guest_joins_its_vlan_only_once_its_group_is_in(dirs):
    """The tap waits parked until the apply is through: a group ovs-ofctl
    refused used to leave it on its VLAN with nothing filtering it, and
    every retry failed the same way."""
    port = _port(security_rules=[{"direction": "ingress", "proto": "icmp"}])
    calls = []
    inner = _two_bridge_run(calls=calls)

    def run(cmd):
        if "bundle" in cmd:
            with open(cmd[-1]) as fl:
                if evpn_flows.port_cookie(port.uuid, "sg") in fl.read():
                    raise subprocess.CalledProcessError(1, cmd)
        return inner(cmd)

    with mock.patch.object(sh, "run", side_effect=run):
        with pytest.raises(subprocess.CalledProcessError):
            port.dump_to_dp()
    assert not any(c[:4] == ["ovs-vsctl", "set", "port", "vethg"] for c in calls)

    calls.clear()
    with mock.patch.object(sh, "run", side_effect=inner):
        port.dump_to_dp()
    vlan = hostns.lookup_local_vlan(port.uuid)
    assert ["ovs-vsctl", "set", "port", "vethg", "tag=%d" % vlan] in calls


def test_port_with_security_rules_installs_ct_pipeline(dirs):
    port = _port(
        dhcp={"cidr": "10.42.0.0/24"},
        security_rules=[{"proto": "tcp", "port": 22}, {"proto": "icmp"}],
    )
    calls = []
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run(calls=calls)):
        port.dump_to_dp()
    # SG flows are submitted as one atomic bundle (see _run_flow_bundle),
    # not individual add-flow calls: assert on the bundle command + file.
    assert any(
        c[:5] == [base.OVSOFCTL_BIN, "-O", base.OPENFLOW_VERSION, "bundle", base.BR_INT]
        for c in calls
    )
    sg = evpn_flows.port_cookie(port.uuid, "sg")
    with open(base.FLOW_BUNDLE_FILE) as fl:
        bundle_lines = fl.read().splitlines()
    assert any(line.startswith("flow delete cookie=%s" % sg) for line in bundle_lines)
    flows = [line for line in bundle_lines if line.startswith("flow add ")]
    # ct entry, established allow, one flow per rule, default drop — all cookied
    assert any("ct(table=10,zone=1)" in f and sg in f for f in flows)
    assert any("ct_state=+est+trk" in f and "NORMAL" in f for f in flows)
    assert any(
        "tp_dst=22" in f and "ct(commit,zone=1)" in f and "NORMAL" in f for f in flows
    )
    assert any(
        "icmp" in f and "ct(commit,zone=1)" in f and "NORMAL" in f for f in flows
    )
    assert any("priority=10" in f and "actions=drop" in f and sg in f for f in flows)
    # inbound-to-guest (dl_dst = guest MAC) enters the ingress table, and
    # with no ingress rules a group denies everything inbound: a security
    # group is an allow-list in both directions.
    assert any(
        "priority=91" in f
        and "dl_dst=52:54:00:aa:bb:cc" in f
        and "ct(table=11,zone=1)" in f
        and sg in f
        for f in flows
    )
    assert any(
        "table=11" in f and "priority=10" in f and "actions=drop" in f for f in flows
    )
    # ... except replies to what the guest itself initiated
    assert any(
        "table=11" in f and "ct_state=+est+trk" in f and "NORMAL" in f for f in flows
    )
    # every SG flow is scoped to this guest: by in_port or by its MAC
    assert all(
        "in_port=" in f or "dl_dst=52:54:00:aa:bb:cc" in f for f in flows if sg in f
    )


def test_port_without_rules_has_no_ct_pipeline(dirs):
    """No rules and nothing pinned means nothing installed at all.

    A port that neither filters nor claims an identity nor is held to its
    own address has nothing for the bridge to do: the bundle is delete-only
    and br-int's default NORMAL carries the guest.
    """
    port = _port(dhcp={"cidr": "10.42.0.0/24"}, port_security=False)
    calls = []
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run(calls=calls)):
        port.dump_to_dp()
    assert any(
        c[:5] == [base.OVSOFCTL_BIN, "-O", base.OPENFLOW_VERSION, "bundle", base.BR_INT]
        for c in calls
    )
    with open(base.FLOW_BUNDLE_FILE) as fl:
        bundle_lines = fl.read().splitlines()
    # no rules, no port security => delete-only (allow-all, no ct pipeline)
    assert not any(line.startswith("flow add ") for line in bundle_lines)


def test_a_pinned_port_may_only_speak_as_itself(dirs):
    """Port security is a property of the port, not of its allow-list.

    A guest with no security group at all is still held to the MAC and the
    address the control plane gave it — otherwise an ingress rule elsewhere
    that names a peer by address is answerable by any neighbour that simply
    chooses that address. It cannot be done at the tap (libvirt refuses an
    nwfilter beside an openvswitch virtualport), so it is done here.
    """
    port = _port(dhcp={"cidr": "10.42.0.0/24"})
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run()):
        port.dump_to_dp()
    with open(base.FLOW_BUNDLE_FILE) as fl:
        lines = fl.read().splitlines()
    adds = [line for line in lines if line.startswith("flow add ")]
    # Its own address gets in, as itself...
    for source in ("10.42.0.7",):
        assert any(
            "priority=90" in line
            and "dl_src=52:54:00:aa:bb:cc" in line
            and "nw_src=%s" % source in line
            for line in adds
        ), source
        assert any("arp" in line and "arp_spa=%s" % source in line for line in adds), (
            source
        )
    # ... and everything else the port sends does not.
    assert any(
        "priority=%d" % evpn_flows._PS_DROP_PRIORITY in line and "actions=drop" in line
        for line in adds
    )


def test_port_security_without_an_address_does_not_blackhole_the_guest(dirs):
    """A control-plane omission must not cost the guest its network.

    With nothing to pin the port to, the enforcement has no allow-list to
    build and would leave only the catch-all drop — every frame the guest
    sends, refused. Not enforcing is the recoverable failure.
    """
    port = _port(ipv4=None, mac="52:54:00:aa:bb:cc")
    calls = []
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run(calls=calls)):
        with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
            port.dump_to_dp()
    assert not any("actions=drop" in " ".join(c) for c in calls)


def test_delete_gcs_the_flow_groups_the_port_installed(dirs):
    """Both of them, and only them: a port's functions and its security
    group are reconciled apart, so they are collected apart too."""
    port = _port(dhcp={"cidr": "10.42.0.0/24"}, security_rules=[{"proto": "icmp"}])
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run()):
        port.dump_to_dp()
    dels = []
    with mock.patch.object(sh, "run", side_effect=lambda c: dels.append(c) or ""):
        port.delete_from_dp()
    del_cookies = [
        c[-1] for c in dels if c[0] == base.OVSOFCTL_BIN and "del-flows" in c
    ]
    assert any(evpn_flows.port_cookie(port.uuid, "nf") in d for d in del_cookies)
    assert any(evpn_flows.port_cookie(port.uuid, "sg") in d for d in del_cookies)
    assert len(del_cookies) == 2


def test_port_not_plugged_yet_is_retriable(dirs):
    port = _port()
    with mock.patch.object(sh, "run", return_value="\n"):
        with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
            port.dump_to_dp()
    assert not (dirs / "vm_conf").exists()
    # Not an error: the VM interface simply is not there yet
    assert port.status == "NEW"


def test_a_port_never_takes_a_tap_by_its_mac(dirs):
    """The MAC is the tenant's to set. A port whose own tap is gone (its
    guest stopped) and that names a neighbour's MAC would find the
    neighbour's tap by `attached-mac` and retag it into its own VLAN."""
    port = _port()
    calls = []

    def run(cmd):
        joined = " ".join(cmd)
        calls.append(joined)
        if "find" in cmd and "Interface" in cmd:
            # the neighbour's tap answers to the MAC, never to this uuid
            return "\n" if "iface-id" in joined else "5\n"
        return ""

    with mock.patch.object(sh, "run", side_effect=run):
        with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
            port.dump_to_dp()

    assert not any("attached-mac" in c for c in calls)
    assert not any(c.startswith("ovs-vsctl set port") for c in calls)

    # ... nor is the neighbour's tap checked as this port's
    with _dp_with(port, ifaces={port.mac: "vethvictim"}, tags={"vethvictim": 1}):
        assert port._plugged_guest_port() is None


def test_port_without_ipv4_is_retriable(dirs):
    port = _port(ipv4=None)
    with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
        port.dump_to_dp()


def test_port_delete_removes_files(dirs):
    port = _port()
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run()):
        port.dump_to_dp()
    with mock.patch.object(sh, "run", return_value=""):
        port.delete_from_dp()
    assert not (dirs / "vm_conf" / ("%s.json" % port.uuid)).exists()
    assert not (dirs / "nf" / str(port.vni) / ("%s.json" % port.uuid)).exists()


def _rendered_conf(model, source_ip):
    """The config gobgpd reads once this role has written its fragment."""
    model._write_part(source_ip)
    return fabric.assemble_gobgp_conf()


def test_host_renders_gobgp_conf(dirs):
    host = _host(rr_addresses=["10.77.0.10", "10.77.0.20"])
    conf = _rendered_conf(host, "10.77.0.11")
    assert base.GOBGP_CONF_MARKER in conf
    assert "as = 65001" in conf
    assert 'router-id = "10.77.0.11"' in conf
    assert conf.count("[[neighbors]]") == 2
    assert 'neighbor-address = "10.77.0.20"' in conf
    assert "long-lived-graceful-restart" in conf


def _gobgp_restarts(runs):
    return [
        c
        for c in runs
        if c[0] == base.SYSTEMCTL_BIN and "restart" in c and base.GOBGP_SERVICE in c
    ]


def _connector_restarts(runs):
    return [
        c
        for c in runs
        if c[0] == base.SYSTEMCTL_BIN and "restart" in c and base.CONNECTOR_SERVICE in c
    ]


def test_host_renders_connector_conf():
    host = _host(as_number=65001)
    conf = host._render_connector_conf("10.77.0.11")
    assert base.GOBGP_CONF_MARKER in conf
    assert 'source_ip = "10.77.0.11"' in conf
    assert "as_number = 65001" in conf
    assert 'gobgp_channel = "unix:///run/gobgpd/api.sock"' in conf
    # router MAC is a stable locally-administered unicast derived from the IP
    assert 'router_mac_type5 = "02:00:0a:4d:00:0b"' in conf
    assert 'switch_name = "%s"' % base.EVPN_BRIDGE in conf
    assert "configs_dir = %s/" % base.CLIENT_CONF_DIR in conf


def test_legacy_gobgp_unit_and_connector_channel_are_reconciled(dirs):
    host = _host()
    runs = []
    with mock.patch.object(
        sh, "run", side_effect=lambda c: runs.append(c) or _host_run(c)
    ):
        host.dump_to_dp()
        Path(base.GOBGP_UNIT_PATH).write_text(
            "[Service]\nExecStart=gobgpd --api-hosts :50051\n"
        )
        Path(base.CONNECTOR_CONF_FILE).write_text(
            host._render_connector_conf("10.77.0.11").replace(
                base.GOBGP_API, "localhost:50051"
            )
        )
        with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
            host.restore_from_dp()
        runs.clear()
        host.update_on_dp()
        assert Path(base.GOBGP_UNIT_PATH).read_text() == fabric.render_gobgp_unit()
        assert Path(
            base.CONNECTOR_CONF_FILE
        ).read_text() == host._render_connector_conf("10.77.0.11")
        assert len(_gobgp_restarts(runs)) == len(_connector_restarts(runs)) == 1
        reload_at = runs.index([base.SYSTEMCTL_BIN, "daemon-reload"])
        assert (
            reload_at
            < runs.index(_gobgp_restarts(runs)[0])
            < runs.index(_connector_restarts(runs)[0])
        )
        host.restore_from_dp()
        host.update_on_dp()
        assert len(_gobgp_restarts(runs)) == len(_connector_restarts(runs)) == 1


@pytest.mark.parametrize("failed_action", ["daemon-reload", "restart"])
def test_gobgp_unit_upgrade_retries_a_failed_service_update(dirs, failed_action):
    host = _host()
    Path(base.GOBGP_UNIT_PATH).unlink()
    failed = False
    calls = []

    def run(cmd):
        nonlocal failed
        calls.append(cmd)
        if failed_action in cmd and not failed:
            failed = True
            raise subprocess.CalledProcessError(1, cmd)
        return _host_run(cmd)

    with mock.patch.object(sh, "run", side_effect=run):
        with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
            host.dump_to_dp()
        assert fabric._GOBGP_UNIT_PENDING
        with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
            host.restore_from_dp()
        host.update_on_dp()
        assert not fabric._GOBGP_UNIT_PENDING
        assert calls.count([base.SYSTEMCTL_BIN, "daemon-reload"]) == 2
        host.restore_from_dp()


def test_gobgp_upgrade_recovers_after_the_agent_loses_pending_state(dirs):
    host = _host()
    with (
        mock.patch.object(sh, "run", side_effect=_host_run) as run,
        mock.patch.object(fabric, "gobgp_api_ready", return_value=False) as ready,
    ):
        host.dump_to_dp()
        assert not fabric._GOBGP_UNIT_PENDING
        run.reset_mock()
        with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
            host.restore_from_dp()
        host.update_on_dp()
        run.assert_any_call([base.SYSTEMCTL_BIN, "daemon-reload"])
        run.assert_any_call([base.SYSTEMCTL_BIN, "restart", base.GOBGP_SERVICE])
        ready.return_value = True
        host.restore_from_dp()


@pytest.mark.parametrize(
    ("directory_mode", "directory_uid", "endpoint_mode", "endpoint_uid", "ready"),
    [
        (0o700, 0, stat.S_IFSOCK | 0o700, 0, True),
        (0o755, 0, stat.S_IFSOCK | 0o700, 0, False),
        (0o700, 1000, stat.S_IFSOCK | 0o700, 0, False),
        (0o700, 0, stat.S_IFSOCK | 0o777, 0, False),
        (0o700, 0, stat.S_IFSOCK | 0o700, 1000, False),
        (0o700, 0, stat.S_IFREG | 0o600, 0, False),
        (0o700, 0, stat.S_IFLNK | 0o700, 0, False),
    ],
)
def test_gobgp_api_requires_a_private_root_socket(
    directory_mode, directory_uid, endpoint_mode, endpoint_uid, ready
):
    directory = os.stat_result(
        (stat.S_IFDIR | directory_mode, 0, 0, 1, directory_uid, 0, 0, 0, 0, 0)
    )
    endpoint = os.stat_result((endpoint_mode, 0, 0, 1, endpoint_uid, 0, 0, 0, 0, 0))
    with mock.patch.object(os, "lstat", side_effect=[directory, endpoint]):
        assert fabric.gobgp_api_ready() is ready


def test_unreadable_gobgp_socket_state_is_unknown():
    with mock.patch.object(os, "lstat", side_effect=PermissionError):
        with pytest.raises(PermissionError):
            fabric.gobgp_api_ready()


def test_the_connectors_own_files_are_roots_alone(dirs):
    """Under fail-static the connector replays the flows it last synced,
    read back from its scratch file, which defaulted to a fixed name in
    /tmp: any local user could create it first and have their OpenFlow
    installed on the EVPN bridge. The anycast statuses sat there too."""
    host = _host()
    conf = host._render_connector_conf("10.77.0.11")
    assert "/tmp/evpn_tmp_flow_file" not in conf
    assert "/tmp/anycast_status_file" not in conf
    run_dir = base.CONNECTOR_RUN_DIR
    assert "tmp_flow_file_path = %s/evpn_tmp_flow_file" % run_dir in conf
    assert "anycast_status_file = %s/anycast_status_file" % run_dir in conf
    # the setting is the [ovs] group's, where the connector reads it
    ovs = conf.split("[ovs]", 1)[1].split("\n[", 1)[0]
    assert "tmp_flow_file_path" in ovs

    os.makedirs(run_dir, mode=0o777)
    os.chmod(run_dir, 0o777)
    with mock.patch.object(sh, "run", side_effect=lambda c: ""):
        host._ensure_connector("10.77.0.11")
    assert os.stat(run_dir).st_mode & 0o777 == 0o700


def test_a_host_whose_run_directory_went_with_a_reboot_is_missing(dirs):
    host = _host()
    with mock.patch.object(sh, "run", side_effect=_host_run):
        host.dump_to_dp()
    with _dp_with(host):
        host.restore_from_dp()
    os.rmdir(base.CONNECTOR_RUN_DIR)
    with _dp_with(host):
        with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
            host.restore_from_dp()


def test_one_session_is_enough_when_there_is_more_than_one_to_lose():
    """The connector trusts the RIB for deletion only once enough of its
    sessions have converged, and its default is all of them — right for peers
    that each feed their own routes, wrong for reflectors. With two, losing
    one would put every hypervisor into fail-static: no flow deleted
    anywhere, a guest that was removed still reachable, while the surviving
    session carries the whole RIB and knows it is gone."""
    one = _host(rr_addresses=["10.77.0.10"])
    assert "fail_static = True" in one._render_connector_conf("10.77.0.11")
    assert "restrict_tunnel_sources = True" in one._render_connector_conf("10.77.0.11")
    assert "fail_static_min_peers = 0" in one._render_connector_conf("10.77.0.11")

    two = _host(rr_addresses=["10.77.0.10", "10.77.0.20"])
    assert "fail_static_min_peers = 1" in two._render_connector_conf("10.77.0.11")

    # Counted over the sessions gobgpd is actually given: this host is one of
    # the two reflectors, so it opens one session and all of them is right.
    itself = _host(rr_addresses=["10.77.0.11", "10.77.0.20"])
    assert "fail_static_min_peers = 0" in itself._render_connector_conf("10.77.0.11")


def test_host_apply_restarts_only_on_change(dirs):
    host = _host()
    runs = []

    def run(cmd):
        runs.append(cmd)
        if cmd[0] == base.IP_BIN and "route" in cmd:
            return json.dumps([{"prefsrc": "10.77.0.11"}])
        if cmd[0] == base.OVSVSCTL_BIN and "ofport" in cmd:
            return "9"
        return ""

    with mock.patch.object(sh, "run", side_effect=run):
        host.dump_to_dp()
        assert len(_gobgp_restarts(runs)) == 1
        # the evpn_connector daemon config is rendered + the service started
        assert (dirs / "evpn_connector.cfg").exists()
        assert len(_connector_restarts(runs)) == 1
        assert any(
            c[0] == base.SYSTEMCTL_BIN and "enable" in c and base.CONNECTOR_SERVICE in c
            for c in runs
        )
        # The host runs the fabric and nothing else: the responder belongs
        # to the guests that need it, so applying a host neither starts it
        # nor claims any traffic for it.
        assert not any(
            c[0] == base.SYSTEMCTL_BIN and base.NF_SERVICE in c for c in runs
        )
        assert not any(
            c[0] == base.OVSOFCTL_BIN and "add-flow" in c and "tp_dst=53" in c[-1]
            for c in runs
        )

        host.update_on_dp()

    # unchanged gobgp -> the service is not restarted again
    assert len(_gobgp_restarts(runs)) == 1
    assert host.status == "ACTIVE"


def test_nf_unit_takes_no_tenant_configuration():
    """No network's settings may reach the daemon's command line: it is
    shared by every guest on the hypervisor, and it is written as root. The
    unit is therefore the same text everywhere — which also means editing a
    function never restarts it."""
    unit = nf.render_nf_unit()
    assert "--records-dir" in unit
    assert "--forwarder" not in unit and "--proxy-forward" not in unit
    assert "--no-dhcp" not in unit and "--no-dns" not in unit
    assert unit == nf.render_nf_unit()


def _host_run(cmd):
    if cmd[0] == base.IP_BIN and "route" in cmd:
        return json.dumps([{"prefsrc": "10.77.0.11"}])
    if cmd[0] == base.OVSVSCTL_BIN and "ofport" in cmd:
        return "9"
    return ""


def test_a_port_starts_the_responder_it_needs(dirs):
    """The daemon belongs to the guests that need it: the first port
    carrying one of its functions brings it up together with its VNI's
    namespace and port, so a port never waits for anything host-scope to
    happen first."""
    calls = []
    port = _port(nfs=STOCK_NFS)
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run(calls=calls)):
        port.dump_to_dp()
    assert (dirs / "nf.service").exists()
    assert any(c[:3] == [base.IP_BIN, "netns", "add"] for c in calls)
    assert any(
        c[:4] == [base.OVSVSCTL_BIN, "--may-exist", "add-port", base.BR_INT]
        and hostns.nf_port(port.vni) in c
        for c in calls
    )
    # ... and the port is moved out of the root namespace into the VNI's
    assert any(
        c[:3] == [base.IP_BIN, "link", "set"]
        and "netns" in c
        and hostns.nf_port(port.vni) in c
        for c in calls
    )
    assert any(
        c[0] == base.SYSTEMCTL_BIN and "enable" in c and base.NF_SERVICE in c
        for c in calls
    )


def test_a_port_without_functions_starts_nothing(dirs):
    calls = []
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run(calls=calls)):
        _port(nfs=[]).dump_to_dp()
    assert not (dirs / "nf.service").exists()
    assert not any(c[0] == base.SYSTEMCTL_BIN and base.NF_SERVICE in c for c in calls)


def test_the_last_served_guest_takes_the_responder_with_it(dirs):
    """The records are the whole of what the daemon serves, so an empty
    directory is what "nobody needs it any more" looks like."""
    stays, goes = _port(nfs=STOCK_NFS), _port(nfs=STOCK_NFS)
    calls = []
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run(calls=calls)):
        stays.dump_to_dp()
        goes.dump_to_dp()
        calls.clear()
        goes.delete_from_dp()
        # another guest is still served: the daemon stays up
        assert not any(
            c[0] == base.SYSTEMCTL_BIN and "disable" in c and base.NF_SERVICE in c
            for c in calls
        )
        stays.delete_from_dp()
    assert any(
        c[0] == base.SYSTEMCTL_BIN and "disable" in c and base.NF_SERVICE in c
        for c in calls
    )


def test_host_restore_checks_marker(dirs):
    host = _host()
    with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
        host.restore_from_dp()

    host._write_part("10.77.0.11")
    (dirs / "gobgp.conf").write_text("# some foreign config\n")
    with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
        host.restore_from_dp()

    (dirs / "gobgp.conf").write_text(fabric.assemble_gobgp_conf())
    # ... and the connector half of the fabric has to be there too
    with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
        host.restore_from_dp()
    (dirs / "evpn_connector.cfg").write_text(host._render_connector_conf("10.77.0.11"))
    (dirs / "run_evpn").mkdir()
    host.restore_from_dp()


def test_a_role_whose_config_was_taken_over_reports_itself_missing(dirs):
    """The check that catches the two roles fighting over the one file: a
    config that no longer carries this role's sections is not this role's
    config, however well-formed it looks."""
    host, rr = _host(), _rr()
    host._write_part("10.77.0.11")
    (dirs / "gobgp.conf").write_text(fabric.assemble_gobgp_conf())
    (dirs / "evpn_connector.cfg").write_text(host._render_connector_conf("10.77.0.11"))
    (dirs / "run_evpn").mkdir()
    host.restore_from_dp()

    # the reflector renders the file for itself alone, the pre-fragment way
    rr._write_part("10.77.0.11")
    parts = [p for p in fabric.gobgp_parts() if p["body"].startswith("[[peer-groups]]")]
    (dirs / "gobgp.conf").write_text(
        "\n".join([base.GOBGP_CONF_MARKER + "\n", parts[0]["global"], parts[0]["body"]])
    )
    with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
        host.restore_from_dp()


def test_both_fabric_roles_share_one_daemon_without_flapping(dirs):
    """A node can be its own route reflector *and* a hypervisor (the
    single-host stand). Each role owns a fragment, the daemon's config is
    assembled from both, and a second iteration that changes nothing must
    not restart gobgpd — the two roles overwriting one file is what made it
    flap for ever."""
    host, rr = _host(rr_addresses=["10.77.0.10"]), _rr()
    runs = []

    def run(cmd):
        runs.append(cmd)
        if cmd[0] == base.IP_BIN and "route" in cmd:
            return json.dumps([{"prefsrc": "10.77.0.11"}])
        return ""

    with mock.patch.object(sh, "run", side_effect=run):
        host.dump_to_dp()
        rr.dump_to_dp()
        restarts_after_first_pass = len(_gobgp_restarts(runs))
        host.update_on_dp()
        rr.update_on_dp()

    conf = (dirs / "gobgp.conf").read_text()
    # one daemon, one identity, both roles' sections
    assert conf.count("[global.config]") == 1
    assert "[[neighbors]]" in conf and "[[peer-groups]]" in conf
    assert 'neighbor-address = "10.77.0.10"' in conf
    assert 'prefix = "10.77.0.0/24"' in conf
    # the reflector's identity wins on a node that holds both roles
    assert 'route-reflector-cluster-id = "10.77.0.11"' in conf
    # ... and nothing changed on the second pass, so nothing was restarted
    assert len(_gobgp_restarts(runs)) == restarts_after_first_pass
    assert host.status == "ACTIVE" and rr.status == "ACTIVE"


def test_a_host_pointed_at_itself_does_not_peer_with_itself(dirs):
    """A session to one's own address never comes up; the RR's own gobgpd
    already holds the fabric's RIB, which is all its evpn_connector reads."""
    host = _host(rr_addresses=["10.77.0.11", "10.77.0.20"])
    conf = _rendered_conf(host, "10.77.0.11")
    assert 'neighbor-address = "10.77.0.11"' not in conf
    assert 'neighbor-address = "10.77.0.20"' in conf


def test_removing_one_role_leaves_the_other_running(dirs):
    """Deleting the hypervisor role from a node that is also the reflector
    must not take the reflector's sessions down with it."""
    host, rr = _host(), _rr()
    with mock.patch.object(sh, "run", side_effect=lambda c: ""):
        host._write_part("10.77.0.11")
        rr._write_part("10.77.0.11")
        (dirs / "gobgp.conf").write_text(fabric.assemble_gobgp_conf())

        runs = []
        with mock.patch.object(sh, "run", side_effect=lambda c: runs.append(c) or ""):
            host.delete_from_dp()
        conf = (dirs / "gobgp.conf").read_text()
        assert "[[peer-groups]]" in conf and "[[neighbors]]" not in conf
        assert not any(
            c[0] == base.SYSTEMCTL_BIN and "stop" in c and base.GOBGP_SERVICE in c
            for c in runs
        )

        # ... and the last role to go takes the daemon with it
        runs.clear()
        with mock.patch.object(sh, "run", side_effect=lambda c: runs.append(c) or ""):
            rr.delete_from_dp()
        assert not (dirs / "gobgp.conf").exists()
        assert any(
            c[0] == base.SYSTEMCTL_BIN and "stop" in c and base.GOBGP_SERVICE in c
            for c in runs
        )


def test_a_configured_but_dead_daemon_is_not_a_fabric(dirs):
    """A config on disk with nothing running behind it used to read as
    healthy. It must be reported *and* repaired by the reapply that
    follows — a check whose apply cannot fix it would recreate the resource
    on every iteration for ever."""
    host = _host()
    host._write_part("10.77.0.11")
    (dirs / "gobgp.conf").write_text(fabric.assemble_gobgp_conf())
    (dirs / "evpn_connector.cfg").write_text(host._render_connector_conf("10.77.0.11"))

    down = {
        base.GOBGP_SERVICE: "failed",
        base.CONNECTOR_SERVICE: "active",
        base.NF_SERVICE: "active",
    }
    with mock.patch.object(state, "_DP_SNAPSHOT", _dp(services=down)):
        with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
            host.restore_from_dp()

        # the repair: an apply that rewrites nothing still starts it
        runs = []

        def run(cmd):
            runs.append(cmd)
            if cmd[0] == base.IP_BIN and "route" in cmd:
                return json.dumps([{"prefsrc": "10.77.0.11"}])
            return ""

        with mock.patch.object(sh, "run", side_effect=run):
            host.dump_to_dp()
        assert _gobgp_restarts(runs)
        assert not (dirs / "gobgp.conf.tmp").exists()

    # a stopped forwarder is reported too — every guest is off the fabric
    with mock.patch.object(
        state,
        "_DP_SNAPSHOT",
        _dp(services=dict(down, **{base.GOBGP_SERVICE: "active"})),
    ):
        host.restore_from_dp()
    with mock.patch.object(
        state,
        "_DP_SNAPSHOT",
        _dp(
            services={
                base.GOBGP_SERVICE: "active",
                base.CONNECTOR_SERVICE: "inactive",
                base.NF_SERVICE: "active",
            }
        ),
    ):
        with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
            host.restore_from_dp()


def test_a_daemon_systemd_cannot_be_asked_about_is_never_missing(dirs):
    """Unknown is not "down": a host without systemd (a container, CI) must
    not have every resource recreated on every iteration."""
    host = _host()
    host._write_part("10.77.0.11")
    (dirs / "gobgp.conf").write_text(fabric.assemble_gobgp_conf())
    (dirs / "evpn_connector.cfg").write_text(host._render_connector_conf("10.77.0.11"))
    (dirs / "run_evpn").mkdir()
    with mock.patch.object(state, "_DP_SNAPSHOT", _dp(services=None)):
        host.restore_from_dp()


def test_service_states_survive_a_nonzero_exit(dirs):
    """`systemctl is-active` exits non-zero as soon as one unit is down —
    the states are still on stdout, and losing them would make every check
    blind."""
    err = subprocess.CalledProcessError(3, "systemctl", output=b"active\nfailed\n")
    with mock.patch.object(sh, "run", side_effect=err):
        assert state.service_states(("a", "b")) == {"a": "active", "b": "failed"}
    # a truncated answer says nothing about any particular unit
    with mock.patch.object(sh, "run", side_effect=lambda c: "active\n"):
        assert state.service_states(("a", "b")) is None
    with mock.patch.object(sh, "run", side_effect=FileNotFoundError):
        assert state.service_states(("a",)) is None


def test_driver_capabilities():
    with (
        mock.patch.object(evpn_driver.os, "makedirs"),
        mock.patch.object(
            evpn_driver.meta.storage_common.JsonFileStorageSingleton,
            "get_instance",
            return_value={},
        ),
    ):
        driver = evpn.EvpnAgentCapabilityDriver()
    assert sorted(driver.get_capabilities()) == [
        "bgp_rr",
        "evpn_host",
        "evpn_nf",
        "evpn_port",
    ]


def test_bgp_rr_renders_reflector_conf(dirs):
    rr = _rr(peer_prefixes=["10.77.0.0/24", "10.78.0.0/24"])
    conf = _rendered_conf(rr, "10.77.0.10")
    assert base.GOBGP_CONF_MARKER in conf
    assert "route-reflector-client = true" in conf
    assert 'route-reflector-cluster-id = "10.77.0.10"' in conf
    assert conf.count("[[dynamic-neighbors]]") == 2
    assert 'prefix = "10.78.0.0/24"' in conf
    assert "long-lived-graceful-restart" in conf
    # No per-neighbor blocks: passive peering only
    assert "[[neighbors]]" not in conf


def test_bgp_rr_registered_in_driver_map():
    assert evpn.EvpnAgentCapabilityDriver.__model_map__["bgp_rr"] is evpn.BgpRr


def test_egress_transfer_is_deterministic_30():
    cidr, root, ns = hostns.egress_transfer(10000)
    assert cidr.endswith("/30")
    octets = cidr.split("/")[0].rsplit(".", 1)
    assert root == "%s.%d" % (octets[0], int(octets[1]) + 1)
    assert ns == "%s.%d" % (octets[0], int(octets[1]) + 2)
    # distinct VNIs never share a transfer net
    assert hostns.egress_transfer(10001)[0] != cidr


@pytest.mark.parametrize("default_route", [True, False])
def test_port_with_gateway_installs_vni_egress(dirs, default_route):
    port = _port(
        fips=[{"public": "203.0.113.7"}],
        dhcp={
            "default_route": default_route,
            "cidr": "10.42.0.0/24",
            "routers": [{"to": "0.0.0.0/0", "via": "10.42.0.1"}],
            "mtu": 1450,
        },
    )
    calls = []
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run(calls=calls)):
        port.dump_to_dp()

    assert any("DNAT" in c and "203.0.113.7" in c for c in calls)
    record = json.loads(Path(port._nf_record_path).read_text())
    assert record["default_route"] is default_route
    ns = hostns.vni_ns(port.vni)
    assert any(c[:3] == ["ip", "netns", "add"] and ns in c for c in calls)
    # gateway leg plugged into the EVPN bridge with a high per-VNI ofport
    # (must not collide with the guests' low patch ofports)
    assert any(
        c[:3] == ["ovs-vsctl", "--may-exist", "add-port"]
        and base.EVPN_BRIDGE in c
        and "gw%d-br" % port.vni in c
        and "ofport_request=%d" % (base.EGRESS_OFPORT_BASE + port.vni % 10000) in c
        for c in calls
    )
    # double NAT: masquerade inside the namespace and for the transfer /30
    tr_cidr, _, _ = hostns.egress_transfer(port.vni)
    assert any("MASQUERADE" in c and ns in c for c in calls)
    assert any("MASQUERADE" in c and tr_cidr in c for c in calls)

    gw_conf = json.loads((dirs / "vm_conf" / ("gw%d.json" % port.vni)).read_text())
    assert gw_conf["routes"] == ["10.42.0.1/32", "0.0.0.0/0"]
    assert gw_conf["vni"] == port.vni
    assert gw_conf["imp_rt"] == ["65001:10001"]
    # the egress leg's export RT is host-unique and imported by nobody
    assert gw_conf["exp_rt"] != gw_conf["imp_rt"]
    asn, num = gw_conf["exp_rt"][0].split(":")
    assert asn == "65001"
    assert 900000000 <= int(num) < 2**32

    # a second apply with everything in place must be a no-op
    calls2 = []

    def run_existing(cmd):
        calls2.append(cmd)
        if cmd[:3] == ["ip", "netns", "list"]:
            return ns + "\n"
        return _two_bridge_run()(cmd)

    with mock.patch.object(sh, "run", side_effect=run_existing):
        port.dump_to_dp()
    assert not any(c[:3] == ["ip", "netns", "add"] for c in calls2)


def test_egress_gc_only_after_last_vni_port(dirs):
    port = _port(
        dhcp={
            "cidr": "10.42.0.0/24",
            "routers": [{"to": "0.0.0.0/0", "via": "10.42.0.1"}],
            "mtu": 1450,
        }
    )
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run()):
        port.dump_to_dp()

    # another guest of the same VNI still present
    sibling = dirs / "vm_conf" / "sibling.json"
    sibling.write_text(json.dumps({"vni": port.vni}))
    sibling_gateway = hostns.egress_port_path(port.vni, "sibling")
    sh.write_atomic(sibling_gateway, json.dumps(port.dhcp))

    dels = []
    with (
        mock.patch.object(sh, "run", side_effect=lambda c: dels.append(c) or ""),
        mock.patch.object(hostns, "egress_leg_plugged", return_value=True),
        mock.patch.object(hostns, "link_in_ns", return_value={"flags": ["UP"]}),
    ):
        port.delete_from_dp()
    ns = hostns.vni_ns(port.vni)
    assert not any(c[:3] == ["ip", "netns", "del"] for c in dels)
    assert (dirs / "vm_conf" / ("gw%d.json" % port.vni)).exists()

    # last one out tears the namespace down
    sibling.unlink()
    sh.remove(sibling_gateway)
    dels = []
    with mock.patch.object(sh, "run", side_effect=lambda c: dels.append(c) or ""):
        port.delete_from_dp()
    assert any(c[:3] == ["ip", "netns", "del"] and ns in c for c in dels)
    assert not (dirs / "vm_conf" / ("gw%d.json" % port.vni)).exists()


def test_source_ip_resolves_toward_the_rr(dirs):
    """The underlay (RR) may live on a different interface than the
    default route (realm node: RR = nested core on the inner bridge), so
    the source address must be resolved toward the RR, not 1.1.1.1."""
    host = _host(rr_addresses=["192.168.100.2"])
    calls = []
    with mock.patch.object(
        sh,
        "run",
        side_effect=lambda c: calls.append(c) or '[{"prefsrc": "192.168.100.1"}]',
    ):
        assert host._source_ip() == "192.168.100.1"
    assert calls[0][-1] == "192.168.100.2"

    rr = evpn.BgpRr(uuid=sys_uuid.uuid4(), as_number=65001, peer_prefixes=[])
    calls = []
    with mock.patch.object(
        sh,
        "run",
        side_effect=lambda c: calls.append(c) or '[{"prefsrc": "10.0.0.1"}]',
    ):
        assert rr._source_ip() == "10.0.0.1"
    assert calls[0][-1] == "1.1.1.1"


def test_fip_nat_programs_dnat_and_snat_inside_the_vrf():
    # A floating IP compiles to 1:1 NAT in the guest's own egress namespace —
    # the only place its private address is routable — plus a host route that
    # gets inbound traffic there (SDN CP API §7).
    calls = []
    with mock.patch.object(sh, "run", side_effect=lambda c: calls.append(c) or ""):
        hostns.ensure_fip_nat(
            "10.100.0.10", [{"public": "203.0.113.7"}], "evpn-fip-p1", 10000
        )
    joined = [" ".join(str(x) for x in c) for c in calls]
    assert any("route replace 203.0.113.7/32 via" in j for j in joined)
    dnat = [j for j in joined if "DNAT" in j]
    snat = [j for j in joined if "SNAT" in j]
    assert dnat and "netns exec vni10000" in dnat[0]
    assert "-d 203.0.113.7" in dnat[0] and "--to-destination 10.100.0.10" in dnat[0]
    assert snat and "netns exec vni10000" in snat[0]
    assert "-s 10.100.0.10" in snat[0] and "--to-source 203.0.113.7" in snat[0]
    # Inserted, not appended: the namespace's blanket masquerade comes after.
    assert "-I POSTROUTING" in snat[0]
    assert all("evpn-fip-p1" in j for j in dnat + snat)


def test_fip_nat_reapplied_unchanged_touches_nothing():
    """Applying a port runs this every time, and everything else about a port
    changes for reasons a floating address knows nothing about — a group
    edited, a rule added. Rebuilding the mapping drops the DNAT and puts it
    back milliseconds later, so an inbound connection arriving in that window
    finds nobody, and the address is announced again for no reason."""
    saved = (
        "-A PREROUTING -d 203.0.113.7/32 -m comment --comment evpn-fip-p1 "
        "-j DNAT --to-destination 10.100.0.10\n"
        "-A POSTROUTING -s 10.100.0.10/32 -m comment --comment evpn-fip-p1 "
        "-j SNAT --to-source 203.0.113.7\n"
    )
    calls = []
    with (
        mock.patch.object(hostns, "fip_rules", return_value=saved),
        mock.patch.object(sh, "run", side_effect=lambda c: calls.append(c) or ""),
        mock.patch.object(hostns, "announce_fip") as announce,
    ):
        hostns.ensure_fip_nat(
            "10.100.0.10", [{"public": "203.0.113.7"}], "evpn-fip-p1", 10000
        )
    joined = [" ".join(str(x) for x in c) for c in calls]
    assert not any("iptables" in j for j in joined)
    announce.assert_not_called()
    # Only the root half, which is re-asserted in place.
    assert any("route replace 203.0.113.7/32 via" in j for j in joined)

    # A different address is a different mapping, and is programmed.
    calls = []
    with (
        mock.patch.object(hostns, "fip_rules", return_value=saved),
        mock.patch.object(sh, "run", side_effect=lambda c: calls.append(c) or ""),
    ):
        hostns.ensure_fip_nat(
            "10.100.0.10", [{"public": "203.0.113.8"}], "evpn-fip-p1", 10000
        )
    joined = [" ".join(str(x) for x in c) for c in calls]
    assert any(
        "--to-destination 10.100.0.10" in j and "203.0.113.8" in j for j in joined
    )


def test_a_gone_egress_takes_its_floating_answers_with_it(dirs):
    """The kernel drops the transfer routes with the links, and those routes
    are how a VNI's floating addresses are found: released first."""
    _, _, tr_ns = hostns.egress_transfer(9200)
    calls = []

    def run(cmd):
        calls.append(cmd)
        if cmd[1:4] == ["route", "show", "via"]:
            return "198.51.100.50 via %s dev upl9200\n" % tr_ns
        if cmd[1:4] == ["neigh", "show", "proxy"]:
            return "198.51.100.50 dev eth0 proxy\n"
        return ""

    with mock.patch.object(sh, "run", side_effect=run):
        hostns.gc_vni_egress(9200)

    arp = [base.IP_BIN, "neigh", "del", "proxy", "198.51.100.50", "dev", "eth0"]
    links = [c for c in calls if c[:3] == [base.IP_BIN, "link", "del"]]
    assert arp in calls and links
    assert calls.index(arp) < calls.index(links[0])


def test_fip_nat_without_addresses_touches_nothing():
    calls = []
    with mock.patch.object(sh, "run", side_effect=lambda c: calls.append(c) or ""):
        hostns.ensure_fip_nat("10.100.0.10", [], "evpn-fip-p2", 10000)
    joined = [" ".join(str(x) for x in c) for c in calls]
    assert not any("DNAT" in j or "SNAT" in j for j in joined)


def _fip_host(saved, routes):
    calls = []

    def run(cmd):
        calls.append(cmd)
        if "iptables-save" in " ".join(cmd):
            return saved
        if cmd[:3] == ["ip", "route", "show"] and "via" in cmd:
            return routes
        return ""

    return calls, run


def test_fip_delete_removes_the_rules_and_the_host_route():
    saved = (
        "-A PREROUTING -d 203.0.113.7/32 -m comment --comment evpn-fip-p1 "
        "-j DNAT --to-destination 10.100.0.10\n"
        "-A POSTROUTING -s 10.100.0.10/32 -m comment --comment evpn-fip-p1 "
        "-j SNAT --to-source 203.0.113.7\n"
        "-A POSTROUTING -o upl10000-ns -j MASQUERADE\n"
    )
    _, _, tr_ns = hostns.egress_transfer(10000)
    # What iproute prints for a host route: no /32.
    calls, run = _fip_host(saved, "203.0.113.7 dev upl10000 \n")
    after_delete = saved.split("\n")[2] + "\n"
    rules = iter([saved, after_delete])

    with (
        mock.patch.object(sh, "run", side_effect=run),
        mock.patch.object(hostns, "fip_rules", side_effect=lambda vni: next(rules)),
        mock.patch.object(hostns, "delete_fip_arp") as arp,
    ):
        hostns.delete_fip_rules("evpn-fip-p1", 10000)
    joined = [" ".join(str(x) for x in c) for c in calls]
    assert any("route del 203.0.113.7/32 via %s" % tr_ns in j for j in joined)
    arp.assert_called_once_with("203.0.113.7")
    deletes = [j for j in joined if "-D " in j]
    assert len(deletes) == 2
    # The namespace's own masquerade is not ours to remove.
    assert not any("MASQUERADE" in j for j in deletes)


def test_an_address_another_guest_took_over_is_left_to_it():
    """A floating address moved to another guest of the same VNI on this
    host: the old port's delete must not take the new one's route and ARP
    answer with it — nor anyone else's in the VNI."""
    remaining = (
        "-A PREROUTING -d 203.0.113.7/32 -m comment --comment evpn-fip-p2 "
        "-j DNAT --to-destination 10.100.0.20\n"
        "-A PREROUTING -d 203.0.113.9/32 -m comment --comment evpn-fip-p3 "
        "-j DNAT --to-destination 10.100.0.30\n"
    )
    before = (
        "-A PREROUTING -d 203.0.113.7/32 -m comment --comment evpn-fip-p1 "
        "-j DNAT --to-destination 10.100.0.10\n" + remaining
    )
    rules = iter([before, remaining])
    calls, run = _fip_host(
        before, "203.0.113.7 dev upl10000 \n203.0.113.9 dev upl10000 \n"
    )
    with (
        mock.patch.object(sh, "run", side_effect=run),
        mock.patch.object(hostns, "fip_rules", side_effect=lambda vni: next(rules)),
        mock.patch.object(hostns, "delete_fip_arp") as arp,
    ):
        hostns.delete_fip_rules("evpn-fip-p1", 10000)
    joined = [" ".join(str(x) for x in c) for c in calls]
    assert any("-D PREROUTING" in j and "evpn-fip-p1" in j for j in joined)
    assert not any("route del" in j for j in joined)
    arp.assert_not_called()


def test_a_lost_arp_answer_is_put_back_by_the_next_apply():
    """The snapshot reports a missing answer; the apply that follows has to
    restore it even though the NAT is unchanged, or the port is found
    missing and recreated on every pass."""
    saved = (
        "-A PREROUTING -d 203.0.113.7/32 -m comment --comment evpn-fip-p1 "
        "-j DNAT --to-destination 10.100.0.10\n"
    )
    calls = []
    with (
        mock.patch.object(hostns, "fip_rules", return_value=saved),
        mock.patch.object(hostns, "fip_link", return_value="eth0"),
        mock.patch.object(sh, "run", side_effect=lambda c: calls.append(c) or ""),
    ):
        hostns.ensure_fip_nat(
            "10.100.0.10", [{"public": "203.0.113.7"}], "evpn-fip-p1", 10000
        )
    joined = [" ".join(str(x) for x in c) for c in calls]
    assert any("neigh replace proxy 203.0.113.7 dev eth0" in j for j in joined)


def test_a_port_announces_only_its_own_route():
    import json as _json

    port = evpn.EvpnPort(
        uuid=__import__("uuid").UUID("00000000-0000-0000-0000-0000000000ab"),
        mac="52:54:00:aa:bb:cd",
        ipv4="10.100.0.11",
        vni=10000,
    )
    conf = _json.loads(port._render_client_conf(8))
    assert conf["routes"] == ["10.100.0.11/32"]


# --- network-function driven port behaviour ---------------------------------


def test_port_without_functions_writes_no_record(dirs):
    """The `simple` kind's toggles reach the host as missing functions, and
    a port served by none is invisible to the responder."""
    port = _port(nfs=[])
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run()):
        port.dump_to_dp()
    assert not (dirs / "nf" / str(port.vni) / ("%s.json" % port.uuid)).exists()


def test_a_function_of_unknown_kind_writes_and_removes_nothing(dirs):
    """The kind names the file (`nf-<kind>.json`) the agent writes and
    removes as root: `../../x` would have put it anywhere."""
    victim = dirs / "victim.json"
    victim.write_text("{}")
    nf = evpn.EvpnNf(uuid=sys_uuid.uuid4(), kind="../../victim", vni=10001)
    with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
        nf.dump_to_dp()
    nf.delete_from_dp()
    assert victim.read_text() == "{}"
    assert not (dirs / "nf").exists()


def test_a_forged_mac_never_reaches_a_flow(dirs):
    """The MAC is written into flow specs as text: a comma adds match fields
    of the tenant's choosing, a newline whole flow lines."""
    port = _port(mac="52:54:00:aa:bb:cc,in_port=1")
    calls = []
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run(calls=calls)):
        with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
            port.dump_to_dp()
    assert calls == []


def test_a_functions_settings_land_beside_the_guests_it_serves(dirs):
    """A guest's record says who it is; the function's own resource says
    what it answers with. Editing a network's resolver therefore rewrites
    one file per VNI and touches no port at all."""
    nf = evpn.EvpnNf(
        uuid=sys_uuid.uuid4(),
        kind="dns",
        vni=10001,
        config={"forwarders": ["10.0.0.53"], "zone_suffix": "corp"},
    )
    nf.dump_to_dp()
    written = json.loads((dirs / "nf" / "10001" / "nf-dns.json").read_text())
    assert written == {
        "forwarders": ["10.0.0.53"],
        "zone_suffix": "corp",
        "_resource_configs": {str(nf.uuid): dict(nf.config)},
    }
    assert nf.status == "ACTIVE"

    # it is checked like everything else the driver installs...
    nf.restore_from_dp()
    (dirs / "nf" / "10001" / "nf-dns.json").unlink()
    with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
        nf.restore_from_dp()

    # ... and the guest's own record carries no function configuration
    port = _port(vni=10001, nfs=[{"kind": "dns", "config": {"forwarders": ["x"]}}])
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run()):
        port.dump_to_dp()
    record = json.loads(
        (dirs / "nf" / str(port.vni) / ("%s.json" % port.uuid)).read_text()
    )
    assert "forwarders" not in record and "proxy_forwards" not in record
    assert record["dns_enabled"] is True
    assert record["dhcp_enabled"] is False


def test_port_record_carries_which_functions_serve_it(dirs):
    port = _port(nfs=[{"kind": "dhcp", "config": {}}])
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run()):
        port.dump_to_dp()
    record = json.loads(
        (dirs / "nf" / str(port.vni) / ("%s.json" % port.uuid)).read_text()
    )
    assert record["dhcp_enabled"] is True
    assert record["dns_enabled"] is False


def test_port_record_is_removed_when_the_functions_go_away(dirs):
    port = _port()
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run()):
        port.dump_to_dp()
    assert (dirs / "nf" / str(port.vni) / ("%s.json" % port.uuid)).exists()
    # the port is re-compiled with no functions at all (toggled off on the
    # CP side)
    port.nfs = []
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run()):
        port.update_on_dp()
    assert not (dirs / "nf" / str(port.vni) / ("%s.json" % port.uuid)).exists()


def test_port_without_proxy_function_has_no_metadata_intercept(dirs):
    port = _port(
        dhcp={"cidr": "10.42.0.0/24", "mtu": 1450},
        nfs=[{"kind": "dhcp", "config": {}}],
    )
    bundles = []
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run(bundles=bundles)):
        port.dump_to_dp()
    lines = [ln for b in bundles for ln in b]
    assert not any(base.METADATA_IP in ln for ln in lines)
    # ... and a punt from a previous compile is withdrawn by the same
    # cookie that carries the guest's whole NF path
    assert lines[0] == "flow delete cookie=%s/-1" % evpn_flows.port_cookie(
        port.uuid, "nf"
    )


def test_ingress_rules_build_their_own_allow_list(dirs):
    port = _port(
        dhcp={"cidr": "10.42.0.0/24"},
        security_rules=[
            {"direction": "egress", "proto": "any"},
            {"direction": "ingress", "proto": "tcp", "port": 443},
        ],
    )
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run()):
        port.dump_to_dp()
    with open(base.FLOW_BUNDLE_FILE) as fl:
        flows = [ln for ln in fl.read().splitlines() if ln.startswith("flow add ")]
    # the ingress rule lands in the ingress table, matched by the guest MAC
    assert any(
        "table=11" in f
        and "tp_dst=443" in f
        and "dl_dst=52:54:00:aa:bb:cc" in f
        and "ct(commit,zone=1)" in f
        for f in flows
    )
    # ... and anything else inbound is dropped (the direction *is* filtered)
    assert any(
        "table=11" in f and "priority=10" in f and "actions=drop" in f for f in flows
    )
    # the egress rule stays in the egress table
    assert any("table=10" in f and "ct(commit,zone=1)" in f for f in flows)


def test_ingress_entry_is_scoped_to_the_delivery_port(dirs):
    """The ingress entry outranks the egress one (91 > 90). Unless it is
    scoped to the patch traffic is delivered over, a guest addressing a
    local neighbour that has a group attached would enter *that* guest's
    ingress table and skip its own egress allow-list entirely."""

    def run(cmd):
        if "find" in cmd and "Interface" in cmd:
            return "vethg\n"
        if "get" in cmd and "interface" in cmd and cmd[-1] == "ofport":
            return "77\n" if cmd[-2].startswith("pi") else "1001\n"
        return ""

    port = _port(
        dhcp={"cidr": "10.42.0.0/24"},
        security_rules=[{"direction": "ingress", "proto": "tcp", "port": 443}],
    )
    with mock.patch.object(sh, "run", side_effect=run):
        port.dump_to_dp()
    with open(base.FLOW_BUNDLE_FILE) as fl:
        flows = [ln for ln in fl.read().splitlines() if ln.startswith("flow add ")]
    entry = [f for f in flows if "table=0" in f and "priority=91" in f]
    assert len(entry) == 1
    assert "in_port=77" in entry[0] and "dl_dst=52:54:00:aa:bb:cc" in entry[0]


def test_a_group_denies_the_direction_it_does_not_mention(dirs):
    """AWS-SG semantics: attaching a group at all means default-deny both
    ways, so an ingress-only ruleset still blocks the guest's egress."""
    port = _port(
        dhcp={"cidr": "10.42.0.0/24"},
        security_rules=[{"direction": "ingress", "proto": "icmp"}],
    )
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run()):
        port.dump_to_dp()
    with open(base.FLOW_BUNDLE_FILE) as fl:
        flows = [ln for ln in fl.read().splitlines() if ln.startswith("flow add ")]
    assert any("table=11" in f and "icmp" in f for f in flows)
    # egress is filtered too, with nothing allowed but established replies
    assert any("table=10" in f and "priority=10" in f and "drop" in f for f in flows)
    assert not any(
        "table=10" in f and "ct(commit" in f and "priority=90" in f for f in flows
    )


def test_functions_take_their_traffic_above_the_filter(dirs):
    """A function intercepts what it serves, so a default-deny security
    group filters only what is left — it never has to carve out the
    guest's own DHCP or its resolver's replies."""
    port = _port(nfs=STOCK_NFS)
    bundles = []
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run(bundles=bundles)):
        port.dump_to_dp()
    flows = [ln for b in bundles for ln in b if ln.startswith("flow add ")]
    intercepts = [
        f for f in flows if "priority=%d" % evpn_flows._NF_INTERCEPT_PRIORITY in f
    ]
    # DHCP is stolen to the responder rather than merely flooded to it, and
    # both intercepts are scoped to this guest's own port
    assert any("tp_dst=67" in f and "in_port=1001" in f for f in intercepts)
    assert any("tp_dst=53" in f and "in_port=1001" in f for f in intercepts)
    # ... above the security-group pipeline, which starts at 91
    assert evpn_flows._NF_INTERCEPT_PRIORITY > 91


def test_only_the_declared_functions_intercept(dirs):
    """A port whose network dropped the `dns` function keeps its own
    resolver: nothing on the bridge claims its port 53."""
    port = _port(nfs=[{"kind": "dhcp", "config": {}}])
    bundles = []
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run(bundles=bundles)):
        port.dump_to_dp()
    flows = [ln for b in bundles for ln in b if ln.startswith("flow add ")]
    assert any("tp_dst=67" in f for f in flows)
    assert not any("tp_dst=53" in f for f in flows)


def test_a_record_the_responder_cannot_encode_does_not_kill_it():
    """One responder serves every guest of the hypervisor, so a lease no
    DHCP option can carry (or a filename longer than the BOOTP field) must
    cost that one reply, not everyone else's DHCP and DNS."""
    assert evpn_nf._lease_seconds({"lease_seconds": 2**40}) == 0xFFFFFFFF
    assert evpn_nf._lease_seconds({"lease_seconds": -5}) == 60
    assert evpn_nf._lease_seconds({"lease_seconds": "nonsense"}) == 86400
    reply = evpn_nf.build_reply(
        b"abcd",
        0,
        "52:54:00:aa:bb:cc",
        5,
        {"ipv4": "10.0.0.5", "lease_seconds": 2**40, "filename": "ы" * 200},
        "10.0.0.0/24",
        "10.0.0.1",
    )
    assert reply


def test_an_unchanged_port_rewrites_no_record(dirs):
    """The responder reloads every guest's record when the directory mtime
    moves, so an apply that changes nothing must leave the file alone."""
    port = _port()
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run()):
        port.dump_to_dp()
    record = dirs / "nf" / str(port.vni) / ("%s.json" % port.uuid)
    before = record.stat().st_mtime_ns

    with mock.patch.object(sh, "run", side_effect=_two_bridge_run()):
        port.update_on_dp()
    assert record.stat().st_mtime_ns == before

    # ... and a real change still lands
    port.dhcp = dict(port.dhcp or {}, mtu=1400)
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run()):
        port.update_on_dp()
    assert json.loads(record.read_text())["mtu"] == 1400


def _dp_with(port, *, cookies=(), ports=(), fip="", **facts):
    """Pretend the host holds exactly this.

    Facts the caller does not name stay unknown (``None``): the driver
    never reports a resource missing on the strength of something it could
    not read off the host.
    """
    snapshot = _dp(
        # None stays None: that is how a caller says "the host could not be
        # asked", which must never read as "the host holds nothing".
        cookies=None if cookies is None else set(cookies),
        ports=None if ports is None else set(ports),
        fip=fip,
        **facts,
    )
    return mock.patch.object(state, "_DP_SNAPSHOT", snapshot)


def _healthy_dp(port, vlan, **facts):
    """A host on which everything this port asked for is installed.

    Tests take one thing away from it: that, and nothing else, is what a
    restore check has to notice.
    """
    installed = {
        evpn_flows.port_cookie(port.uuid, "sg"),
        evpn_flows.port_cookie(port.uuid, "nf"),
    }
    live = {"pi%d" % vlan, hostns.nf_port(port.vni)}
    defaults = {
        "cookies": installed,
        "ports": live,
        "evpn_ports": {"pe%d" % vlan},
        "tags": {"vethg": vlan},
        "ifaces": {str(port.uuid): "vethg"},
        "netns": {hostns.vni_ns(port.vni)},
        "neigh": {port.vni: {port.ipv4: port.mac}},
        "forwarded": {port.vni},
        "services": {
            base.NF_SERVICE: "active",
            base.GOBGP_SERVICE: "active",
            base.CONNECTOR_SERVICE: "active",
        },
    }
    defaults.update(facts)
    # The NF port is tagged like a guest — it is not a trunk carrying every
    # other tenant's VLAN into this namespace. It belongs to the VNI, not to
    # any one guest, so a test taking a guest's tag away (or unplugging it
    # entirely) leaves this one alone; a test that means to remove it says so
    # by naming the port.
    if isinstance(defaults["tags"], dict):
        defaults["tags"].setdefault(
            hostns.nf_port(port.vni),
            hostns.lookup_local_vlan(hostns.nf_vlan_key(port.vni)),
        )
    return _dp_with(port, **defaults)


def test_port_checks_share_vlan_and_fip_reads_within_a_cycle(dirs):
    ports = [_port(nfs=[], security_rules=[], port_security=False) for _ in range(3)]
    agent = object.__new__(evpn.EvpnAgentCapabilityDriver)
    agent._storage = mock.Mock()
    vlan_map = {str(port.uuid): index + 1 for index, port in enumerate(ports)}
    Path(base.VLAN_MAP_PATH).write_text(json.dumps(vlan_map))
    rules = "\n".join(
        "-A PREROUTING --comment %s -j DNAT" % hostns.fip_tag(port.uuid)
        for port in ports
    )
    with (
        mock.patch.object(state, "snapshot_dataplane", side_effect=_dp),
        mock.patch.object(hostns, "_vlan_map", wraps=hostns._vlan_map) as vlans,
        mock.patch.object(hostns, "fip_rules", return_value=rules) as nat,
        mock.patch.object(
            hostns, "proxied_fips", return_value={"203.0.113.7": "eth0"}
        ) as arp,
    ):
        for _ in range(2):
            agent.start()
            try:
                for port in ports:
                    port.fips = [{"public": "203.0.113.7"}]
                    assert port._missing_on_dp() == ["client config"]
            finally:
                agent.finalize()
            assert state._DP_SNAPSHOT is None

        assert vlans.call_count == 2
        assert nat.call_count == 2
        assert arp.call_count == 2


def test_a_port_whose_flows_were_wiped_reports_itself_missing(dirs):
    """Reconciliation compares the actual with the target: a guest whose
    security-group flows were removed by hand must not read back as
    healthy, or nothing would ever reinstall them."""
    port = _port(security_rules=[{"direction": "egress", "proto": "icmp"}])
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run()):
        port.dump_to_dp()
    vlan = hostns.lookup_local_vlan(port.uuid)

    installed = {
        evpn_flows.port_cookie(port.uuid, "sg"),
        evpn_flows.port_cookie(port.uuid, "nf"),
    }
    live_ports = {"pi%d" % vlan, hostns.nf_port(port.vni)}
    with _dp_with(port, cookies=installed, ports=live_ports):
        port.restore_from_dp()

    with _dp_with(
        port,
        cookies=installed - {evpn_flows.port_cookie(port.uuid, "sg")},
        ports=live_ports,
    ):
        with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
            port.restore_from_dp()

    # the guest's patch is just as load-bearing
    with _dp_with(port, cookies=installed, ports={hostns.nf_port(port.vni)}):
        with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
            port.restore_from_dp()


def test_lost_anti_spoof_and_identity_flows_are_drift(dirs):
    """A port with no group still has flows under its `sg` cookie: port
    security (on by default) pins what it may send, and an identity is
    stamped on it. The check looked at the rules alone, so a flow loss took
    a guest's anti-spoofing away for good."""
    for kwargs in (
        {},  # port security, by default
        {"port_security": False, "identity": 7},
    ):
        port = _port(nfs=[], security_rules=[], fips=[], **kwargs)
        with mock.patch.object(sh, "run", side_effect=_two_bridge_run()):
            port.dump_to_dp()
        vlan = hostns.lookup_local_vlan(port.uuid)
        sg = evpn_flows.port_cookie(port.uuid, "sg")
        with _dp_with(port, cookies={sg}, ports={"pi%d" % vlan}):
            port.restore_from_dp()
        with _dp_with(port, cookies=set(), ports={"pi%d" % vlan}):
            with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
                port.restore_from_dp()


@pytest.mark.parametrize("rules", [[], [{"direction": "egress", "proto": "icmp"}]])
@pytest.mark.parametrize("identity", [0, 5])
@pytest.mark.parametrize("port_security", [False, True])
@pytest.mark.parametrize("sources", [["10.42.0.7"], [], ["bogus"]])
@pytest.mark.parametrize("mac", ["52:54:00:aa:bb:cc", "not-a-mac"])
def test_the_sg_check_asks_exactly_what_the_apply_installs(
    rules, identity, port_security, sources, mac
):
    with (
        mock.patch.object(sh, "run_flow_bundle") as bundle,
        mock.patch.object(evpn_flows, "cut_revoked_sessions"),
    ):
        evpn_flows.ensure_security_group(
            "7",
            3,
            rules,
            "0x1",
            mac,
            delivery_ofport="9",
            identity=identity,
            sources=sources,
            port_security=port_security,
        )
    lines = bundle.call_args[0][0] if bundle.called else []
    installs = any(line.startswith("flow add") for line in lines)
    assert installs == evpn_flows.security_group_installs(
        rules, mac, identity=identity, sources=sources, port_security=port_security
    )


def test_a_port_is_not_asked_for_what_it_never_wanted(dirs):
    """Only what the port asked for is checked — otherwise a guest with no
    floating address, or none of the functions, would be recreated on every
    single iteration."""
    port = _port(nfs=[], security_rules=[], fips=[], port_security=False)
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run()):
        port.dump_to_dp()
    vlan = hostns.lookup_local_vlan(port.uuid)
    with _dp_with(port, cookies=set(), ports={"pi%d" % vlan}):
        port.restore_from_dp()


def test_a_check_never_asks_for_what_apply_skipped(dirs):
    """A false "missing" is worse than a missed drift: it recreates the port
    on every iteration, for ever. The metadata path is skipped without a
    subnet CIDR, so it must not be demanded either."""
    port = _port(nfs=STOCK_NFS, dhcp={"mtu": 1450})  # a `proxy` port, no cidr
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run()):
        port.dump_to_dp()
    vlan = hostns.lookup_local_vlan(port.uuid)

    with _dp_with(
        port,
        cookies={
            evpn_flows.port_cookie(port.uuid, "nf"),
            # port security is on by default and has flows of its own
            evpn_flows.port_cookie(port.uuid, "sg"),
        },
        ports={"pi%d" % vlan, hostns.nf_port(port.vni)},
    ):
        port.restore_from_dp()


def _installed(dirs, **kwargs):
    """A port applied to a mocked host, with the local VLAN it was given."""
    port = _port(**kwargs)
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run()):
        port.dump_to_dp()
    return port, hostns.lookup_local_vlan(port.uuid)


def test_a_guest_wired_to_nothing_reports_itself_missing(dirs):
    """The patch pair is two halves: with `pe` gone from the EVPN bridge
    the guest's `pi` leads nowhere, and the fabric never reaches it — a
    state the port used to report as healthy."""
    port, vlan = _installed(dirs)
    with _healthy_dp(port, vlan):
        port.restore_from_dp()

    with _healthy_dp(port, vlan, evpn_ports=set()):
        with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
            port.restore_from_dp()

    # The bridge belongs to evpn_connector: while it cannot be read at all,
    # no guest port can fix anything by being recreated.
    with _healthy_dp(port, vlan, evpn_ports=None):
        port.restore_from_dp()


def test_a_guest_knocked_off_its_vlan_reports_itself_missing(dirs):
    """An OVS restart or a re-plug by libvirt drops the port back to the
    default VLAN, where its frames reach neither its own patch nor its
    security group — and where another tenant's are."""
    port, vlan = _installed(dirs)
    with _healthy_dp(port, vlan, tags={"vethg": None}):
        with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
            port.restore_from_dp()

    # A stopped VM is not drift: nothing about the port needs reinstalling
    # before the guest comes back, tag or no tag.
    with _healthy_dp(port, vlan, ifaces={}, tags={}):
        port.restore_from_dp()


def test_the_egress_a_subnet_asked_for_is_checked(dirs):
    """The DVR egress is the guest's whole way off its subnet; a namespace
    someone removed left every guest on the VNI black-holed while the port
    read back healthy."""
    port, vlan = _installed(
        dirs,
        dhcp={
            "cidr": "10.42.0.0/24",
            "routers": [{"to": "0.0.0.0/0", "via": "10.42.0.1"}],
            "mtu": 1450,
        },
    )
    leg = {hostns.egress_leg(port.vni), "pe%d" % vlan}
    with _healthy_dp(port, vlan, evpn_ports=leg):
        port.restore_from_dp()

    with _healthy_dp(port, vlan, evpn_ports=leg, netns=set()):
        with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
            port.restore_from_dp()

    # the gateway leg is as load-bearing as the namespace
    with _healthy_dp(port, vlan, evpn_ports={"pe%d" % vlan}):
        with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
            port.restore_from_dp()


def test_an_egress_the_root_chain_refuses_to_route_reports_itself(dirs):
    """The namespace, its config and its leg can all be perfect while the
    root chain still refuses to route to it — libvirt puts a blanket REJECT
    back at the top of its own chains whenever one of its networks is
    recreated. Nothing else asks for that rule again, so unchecked it stayed
    missing for good: the guest netbooted into an endless iPXE retry while
    the boot API answered fine one hop away."""
    port, vlan = _installed(
        dirs,
        dhcp={
            "cidr": "10.42.0.0/24",
            "routers": [{"to": "0.0.0.0/0", "via": "10.42.0.1"}],
            "mtu": 1450,
        },
    )
    leg = {hostns.egress_leg(port.vni), "pe%d" % vlan}
    with _healthy_dp(port, vlan, evpn_ports=leg):
        port.restore_from_dp()

    with _healthy_dp(port, vlan, evpn_ports=leg, forwarded=set()):
        with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
            port.restore_from_dp()

    # A chain that could not be read says nothing — an unknown must never
    # recreate every guest on the VNI, once per iteration, for ever.
    with _healthy_dp(port, vlan, evpn_ports=leg, forwarded=None):
        port.restore_from_dp()


def test_only_both_legs_count_as_routable():
    """Half the pair is a namespace that can be answered but not reached, or
    the reverse — either one is as broken as neither."""
    both = (
        "-A FORWARD -i upl9105 -m comment --comment %s -j ACCEPT\n"
        "-A FORWARD -o upl9105 -m comment --comment %s -j ACCEPT\n"
        % (hostns.TRANSFER_FORWARD_TAG, hostns.TRANSFER_FORWARD_TAG)
    )
    one = "-A FORWARD -i upl9106 -m comment --comment %s -j ACCEPT\n" % (
        hostns.TRANSFER_FORWARD_TAG
    )
    # Somebody else's accept on the same interface is not ours to count.
    untagged = "-A FORWARD -o upl9107 -j ACCEPT\n"
    with mock.patch.object(sh, "run", return_value=both + one + untagged):
        assert hostns.forwarded_vnis() == {9105}

    with mock.patch.object(sh, "run", side_effect=FileNotFoundError):
        assert hostns.forwarded_vnis() is None


def test_a_subnet_without_a_gateway_is_never_asked_for_an_egress(dirs):
    """`_ensure_vni_egress` builds nothing without a gateway, so demanding
    one would recreate every guest on such a subnet, for ever."""
    port, vlan = _installed(dirs, dhcp={"cidr": "10.42.0.0/24", "mtu": 1450})
    with _healthy_dp(port, vlan, netns=set()):
        port.restore_from_dp()


def test_a_guest_whose_responder_died_reports_itself_missing(dirs):
    """A record on disk is served by nothing while the daemon that reads it
    is gone — and the guest is worse off than with no function at all,
    because its intercepts still take its DHCP and DNS into silence."""
    port, vlan = _installed(dirs)  # STOCK_NFS: dhcp + dns + proxy

    with _healthy_dp(port, vlan, services={base.NF_SERVICE: "failed"}):
        with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
            port.restore_from_dp()

    # restarting is not dying: `Restart=always` brings it back on its own
    with _healthy_dp(port, vlan, services={base.NF_SERVICE: "activating"}):
        port.restore_from_dp()

    # ... and the port of its own VNI that it answers on
    with _healthy_dp(port, vlan, ports={"pi%d" % vlan}):
        with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
            port.restore_from_dp()


def test_a_guest_whose_neighbour_was_flushed_reports_itself_missing(dirs):
    """The namespace cannot ARP for a guest that lives behind a flow, so
    the entry is programmed once — and a permanent entry does not survive
    its interface going down. Nothing noticed: the guest kept its address,
    its flows and its records while the metadata proxy, which answers over
    the kernel stack, had no way to send a single packet back to it."""
    port, vlan = _installed(dirs)  # STOCK_NFS: dhcp + dns + proxy

    with _healthy_dp(port, vlan):
        port.restore_from_dp()

    with _healthy_dp(port, vlan, neigh={port.vni: {}}):
        with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
            port.restore_from_dp()

    # an entry for the right address with the wrong mac is drift too:
    # the proxy would answer into a guest that is no longer there
    with _healthy_dp(port, vlan, neigh={port.vni: {port.ipv4: "52:54:00:de:ad:00"}}):
        with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
            port.restore_from_dp()


def test_a_namespace_that_cannot_be_asked_keeps_its_guests(dirs):
    """Unknown is not missing, here as everywhere: a namespace whose table
    could not be read must not recreate every guest on it."""
    port, vlan = _installed(dirs)
    for unknown in (None, {port.vni: None}):
        with _healthy_dp(port, vlan, neigh=unknown):
            port.restore_from_dp()


def test_the_snapshot_keeps_only_permanent_neighbours(dirs):
    """A guest's entry is programmed, never learned: an INCOMPLETE or
    FAILED row is the kernel trying to ARP for something it cannot reach —
    the very state this check exists to catch, not evidence against it."""
    listing = json.dumps(
        [
            {"dst": "10.42.0.7", "lladdr": "52:54:00:AA:BB:CC", "state": ["PERMANENT"]},
            {"dst": "10.42.0.8", "state": ["INCOMPLETE"]},
            {"dst": "10.42.0.9", "lladdr": "52:54:00:aa:bb:dd", "state": ["STALE"]},
        ]
    )
    with mock.patch.object(sh, "run", return_value=listing):
        found = state.snapshot_neighbours({"vni10001", "notavni", "vni"})
    assert found == {10001: {"10.42.0.7": "52:54:00:aa:bb:cc"}}

    with mock.patch.object(
        sh, "run", side_effect=subprocess.CalledProcessError(1, "ip")
    ):
        assert state.snapshot_neighbours({"vni10001"}) == {10001: None}
    assert state.snapshot_neighbours(None) is None


def test_every_flow_call_names_its_openflow_version(dirs):
    """ovs-ofctl negotiates OpenFlow 1.0 unless told otherwise, and a call
    site that forgets the flag differs in what the switch accepts without
    ever failing — so no call site gets to write the argv itself."""
    port = _port(security_rules=[{"direction": "egress", "proto": "icmp"}])
    calls = []
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run(calls=calls)):
        port.dump_to_dp()
        port.delete_from_dp()
    flow_calls = [c for c in calls if c[0] == base.OVSOFCTL_BIN]
    assert flow_calls
    assert all(c[1:3] == ["-O", base.OPENFLOW_VERSION] for c in flow_calls)


def test_an_ovs_that_cannot_be_asked_reports_nothing_missing(dirs):
    """The whole point of the unknown/absent distinction: while OVS is
    restarting, every fact about it is unreadable — and a port that reports
    its flows and patches gone would be recreated on every iteration of the
    outage, rewriting each guest's client config as it went."""
    port, vlan = _installed(dirs)
    with _dp_with(port, cookies=None, ports=None):
        port.restore_from_dp()

    # ... while a table that WAS read and simply has no br-int is a drift
    # the apply repairs (it recreates the bridge).
    with _dp_with(port, cookies=set(), ports=set()):
        with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
            port.restore_from_dp()


def test_the_snapshot_marks_an_unreadable_ovs_unknown(dirs):
    """`_snapshot_ovs` must not turn a failed read into an empty host."""
    with mock.patch.object(state, "ovs_rows", return_value=None):
        assert state.snapshot_ovs() == {
            "ports": None,
            "evpn_ports": None,
            "tags": None,
        }


def test_a_guests_return_leg_is_its_own(dirs):
    """The reply path is per guest and explicit: from this VNI's port to
    this guest's ofport, matched on its MAC. Nothing host-wide can hand one
    tenant's answers to another tenant's port, and nothing has to forge a
    VLAN to get them there."""
    port = _port(nfs=STOCK_NFS)
    bundles = []
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run(bundles=bundles)):
        port.dump_to_dp()
    lines = [ln for b in bundles for ln in b]
    ret = [ln for ln in lines if "dl_dst=%s" % port.mac in ln]
    assert ret and "actions=output:" in ret[0]
    assert evpn_flows.port_cookie(port.uuid, "nf") in ret[0]
    # ... and the whole path is replaced as one bundle, never appended to
    assert lines[0] == "flow delete cookie=%s/-1" % evpn_flows.port_cookie(
        port.uuid, "nf"
    )


def test_a_guest_that_asked_for_no_function_ignores_the_responder(dirs):
    """The daemon is nothing to a guest that manages its own addressing."""
    port, vlan = _installed(dirs, nfs=[])
    with _healthy_dp(
        port, vlan, ports={"pi%d" % vlan}, services={base.NF_SERVICE: "failed"}
    ):
        port.restore_from_dp()


def test_an_egress_that_lost_its_leg_is_rebuilt(dirs):
    """The repair behind the check: with the leg gone the namespace serves
    nothing, and it cannot be half-rebuilt — the veths still inside it
    would collide with the new ones — so it is torn down and built again."""
    dhcp = {
        "cidr": "10.42.0.0/24",
        "routers": [{"to": "0.0.0.0/0", "via": "10.42.0.1"}],
        "mtu": 1450,
    }
    ns = hostns.vni_ns(9001)
    calls = []

    live = {ns}

    def run(cmd):
        calls.append(cmd)
        if cmd[:3] in (
            [base.IP_BIN, "-json", "route"],
            [base.IP_BIN, "-json", "addr"],
        ):
            return "[]"
        if cmd[:3] == [base.IP_BIN, "netns", "del"]:
            live.discard(cmd[-1])
        if cmd[:3] == [base.IP_BIN, "netns", "list"]:
            return "".join("%s (id: 0)\n" % n for n in live)
        if "ofport" in cmd:
            # The leg is gone from the EVPN bridge until it is plugged again.
            plugged = any(
                "add-port" in c and hostns.egress_leg(9001) in c for c in calls
            )
            return "30001\n" if plugged else ""
        if "mtu" in cmd:
            return "1450\n"
        return ""

    sh.write_atomic(
        hostns.egress_conf_path(9001),
        json.dumps({"routes": ["10.42.0.1/32"], "vni": 9001}),
    )
    with mock.patch.object(sh, "run", side_effect=run):
        hostns.ensure_vni_egress(9001, dhcp, ["65001:9001"])

    # The namespace stays — it belongs to the VNI, not to its egress — but
    # the legs are torn down and built again inside it.
    assert not any(c[:3] == [base.IP_BIN, "netns", "del"] and ns in c for c in calls)
    assert any(
        c[:5] == [base.IP_BIN, "netns", "exec", ns, base.IP_BIN] and "del" in c
        for c in calls
    )
    assert any(c[:4] == [base.IP_BIN, "link", "add", "gw9001"] for c in calls)


EGRESS_DHCP = {
    "cidr": "10.42.0.0/24",
    "routers": [{"to": "0.0.0.0/0", "via": "10.42.0.1"}],
    "mtu": 1450,
}


def _guard_run(calls, connected=("10.20.0.0/24",), saved=""):
    """A `_run` mock that answers the two questions the egress guard asks.

    ``connected`` is what the host reports as its own networks and ``saved``
    what iptables-save already finds in the namespace. Every `-C` check
    fails, so each wanted rule is one visible insert.
    """
    plain = _two_bridge_run(calls=calls)

    def run(cmd):
        if "route" in cmd and "scope" in cmd:
            calls.append(cmd)
            return json.dumps([{"dst": dst, "dev": "eth0"} for dst in connected])
        if base.IPTABLES_SAVE_BIN in cmd:
            calls.append(cmd)
            return saved
        if base.IPTABLES_BIN in cmd and "-C" in cmd:
            calls.append(cmd)
            raise subprocess.CalledProcessError(1, cmd)
        return plain(cmd)

    return run


def _inserted(calls):
    """The guard rules this run put into the namespace's FORWARD chain."""
    return [
        c
        for c in calls
        if base.IPTABLES_BIN in c and "-I" in c and hostns.FABRIC_GUARD_TAG in c
    ]


def test_the_egress_keeps_a_guest_out_of_everything_internal(dirs):
    """The overlay's isolation is worth nothing while the guest can address
    the transport that carries it — and "the transport" is not only what
    this host is plugged into. An internal network behind a router is in no
    routing table here, so a guard built from what the host can see would
    have covered it by service port and by nothing else."""
    calls = []
    with mock.patch.object(
        sh,
        "run",
        side_effect=_guard_run(calls, connected=("10.20.0.0/24", "203.0.113.0/24")),
    ):
        hostns.ensure_vni_egress(9100, EGRESS_DHCP, ["65001:9100"])

    rules = [" ".join(c) for c in _inserted(calls)]
    ns = hostns.vni_ns(9100)
    assert all(ns in rule for rule in rules)
    # The whole of the space that is not globally routable, whatever
    # protocol a guest tries and whether or not this host is on it.
    for prefix in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"):
        assert any("-d %s" % prefix in rule and "DROP" in rule for rule in rules)
    # A network inside that space is not repeated as a rule of its own: the
    # set must not grow with every interface the host gains.
    assert not any("-d 10.20.0.0/24" in rule for rule in rules)
    # A publicly-addressed one this host *is* on still needs its own rule —
    # there the space above covers nothing.
    assert any("-d 203.0.113.0/24" in rule and "DROP" in rule for rule in rules)
    # ... including the fabric's own plumbing: a transfer address belongs to
    # the root namespace, so reaching one is reaching the host itself, one
    # hop short of everything else this guard covers.
    assert any("-d %s" % hostns.transfer_net() in rule for rule in rules)
    # ... and the fabric's own protocols, wherever they are addressed: a
    # second site's VTEPs are not in this host's routing table, and a guest
    # that reaches port 4789 injects a frame into whatever VNI it names.
    assert any("-p udp --dport 4789" in rule for rule in rules)
    assert any("-p tcp --dport 179" in rule for rule in rules)
    assert any("-p tcp --dport 50051" in rule for rule in rules)


def test_the_guard_covers_an_address_the_host_holds_off_any_link(dirs):
    """A /32 on `lo` (a router id, a service address) has no on-link route,
    so the guard built from routes left it -- the host itself, every service
    it binds to 0.0.0.0 -- one hop from a guest's gateway."""
    calls = []
    guard = _guard_run(calls, connected=("203.0.113.0/24",))
    held = _addr_show(["198.51.100.7", "203.0.113.5", "10.20.0.3"])

    def run(cmd):
        if cmd[:4] == [base.IP_BIN, "-json", "addr", "show"]:
            return held
        return guard(cmd)

    with mock.patch.object(sh, "run", side_effect=run):
        hostns.ensure_vni_egress(9100, EGRESS_DHCP, ["65001:9100"])

    rules = [" ".join(c) for c in _inserted(calls)]
    assert any("-d 198.51.100.7/32" in rule and "DROP" in rule for rule in rules)
    # already covered by the space and the on-link network: no rule of its own
    assert not any("-d 203.0.113.5/32" in rule for rule in rules)
    assert not any("-d 10.20.0.3/32" in rule for rule in rules)


def test_the_guard_is_repaired_on_an_egress_that_is_already_built(dirs):
    """A firewall flushed inside the namespace leaves the egress config, the
    leg and the routes exactly as they were — and the guest free. So the
    guard is asserted before the "nothing changed" shortcut, not after it."""
    sh.write_atomic(
        hostns.egress_conf_path(9101),
        json.dumps({"routes": ["10.42.0.1/32"], "vni": 9101}),
    )
    calls = []

    def run(cmd):
        if "ofport" in cmd:
            return "30001\n"  # the leg is plugged: nothing to rebuild
        if cmd[-1].endswith("/mtu"):
            return "1450\n"  # ... and built with the MTU the subnet asks for
        return _guard_run(calls)(cmd)

    with mock.patch.object(sh, "run", side_effect=run):
        hostns.ensure_vni_egress(9101, EGRESS_DHCP, ["65001:9101"])

    assert not any(c[:4] == [base.IP_BIN, "link", "add", "gw9101"] for c in calls)
    assert _inserted(calls)


def test_a_network_the_host_left_stops_being_blocked_and_only_then(dirs):
    """The set follows the host's interfaces, so it has to be reconciled —
    but a guard is removed only after the one that replaces it is in, or the
    reconcile itself is the window."""
    stale = (
        "-A FORWARD -i gw9102 -d 10.99.0.0/24 -m comment --comment %s -j DROP"
        % hostns.FABRIC_GUARD_TAG
    )
    keep = (
        "-A FORWARD -i gw9102 -p udp -m udp --dport 4789 "
        "-m comment --comment %s -j DROP" % hostns.FABRIC_GUARD_TAG
    )
    # The same drop as `keep`, from before the guard was scoped to the leg
    # the guests are on: it blocked the way *in* to a floating address as
    # well, so it is a rule to be replaced and not one to be left alone.
    unscoped = (
        "-A FORWARD -p udp -m udp --dport 4789 -m comment --comment %s -j DROP"
        % hostns.FABRIC_GUARD_TAG
    )
    foreign = "-A FORWARD -d 10.99.0.0/24 -j DROP"  # somebody else's rule
    calls = []
    with mock.patch.object(
        sh,
        "run",
        side_effect=_guard_run(
            calls,
            connected=("10.20.0.0/24",),
            saved="\n".join([stale, keep, unscoped, foreign]),
        ),
    ):
        hostns.ensure_vni_egress(9102, EGRESS_DHCP, ["65001:9102"])

    order = [" ".join(c) for c in calls]
    deletes = [rule for rule in order if base.IPTABLES_BIN in rule and " -D " in rule]
    assert any("10.99.0.0/24" in rule for rule in deletes)
    # the rule that is still wanted stays, and an untagged rule is not ours
    assert not any("4789" in rule and "-i gw9102" in rule for rule in deletes), (
        "the scoped drop that is still wanted is left alone"
    )
    assert any("4789" in rule and "-i gw9102" not in rule for rule in deletes), (
        "its unscoped predecessor is replaced, not kept beside it"
    )
    assert not any(rule.endswith("-d 10.99.0.0/24 -j DROP") for rule in deletes)
    inserted_at = min(i for i, rule in enumerate(order) if " -I " in rule)
    assert inserted_at < min(i for i, rule in enumerate(order) if " -D " in rule)


@pytest.mark.parametrize("query", ["route", "addr"])
@pytest.mark.parametrize(
    "failure",
    ["command", "missing", "json", "shape", "row", "partial", "record", "nested", "ip"],
)
def test_the_guard_survives_a_host_that_cannot_be_asked(dirs, query, failure):
    """A command that failed is not an answer: an unreadable routing table
    must not be read as "this host is on no networks", which would take the
    guard down at exactly the moment nothing else is working either."""
    stale = (
        "-A FORWARD -i gw9103 -d 203.0.113.0/24 -m comment --comment %s -j DROP\n"
        "-A FORWARD -i gw9103 -d 198.51.100.7/32 -m comment --comment %s -j DROP"
    ) % (hostns.FABRIC_GUARD_TAG, hostns.FABRIC_GUARD_TAG)
    sh.write_atomic(
        hostns.egress_conf_path(9103),
        json.dumps({"routes": ["10.42.0.1/32"], "vni": 9103}),
    )
    calls = []
    guard = _guard_run(calls, saved=stale)

    def run(cmd):
        if cmd[:4] == [base.IP_BIN, "-json", query, "show"]:
            calls.append(cmd)
            if failure == "command":
                raise subprocess.CalledProcessError(1, cmd)
            if failure == "missing":
                raise FileNotFoundError(base.IP_BIN)
            if failure == "json":
                return "not json"
            if failure == "shape":
                return "{}"
            if failure == "row":
                return "[null]"
            if failure == "partial":
                valid = (
                    {"dst": "203.0.113.0/24"}
                    if query == "route"
                    else {
                        "ifname": "lo",
                        "addr_info": [{"family": "inet", "local": "198.51.100.7"}],
                    }
                )
                return json.dumps([valid, None])
            if failure == "record":
                return "[{}]"
            if failure == "nested":
                return json.dumps(
                    [{"dst": "not a prefix"}]
                    if query == "route"
                    else [{"ifname": "lo", "addr_info": {}}]
                )
            return json.dumps(
                [{"dst": None}]
                if query == "route"
                else [
                    {
                        "ifname": "lo",
                        "addr_info": [{"family": "inet", "local": "invalid"}],
                    }
                ]
            )
        return guard(cmd)

    with mock.patch.object(sh, "run", side_effect=run):
        with pytest.raises(
            (subprocess.CalledProcessError, FileNotFoundError, ValueError, RuntimeError)
        ):
            hostns.ensure_vni_egress(9103, EGRESS_DHCP, ["65001:9103"])

    assert not any(base.IPTABLES_BIN in c for c in calls)
    assert not any("del" in c or "add-port" in c for c in calls)
    assert 9103 not in hostns._GUARDED_VNIS

    # A successful retry may collect a route the host has really left.
    calls.clear()
    with mock.patch.object(sh, "run", side_effect=guard):
        hostns.ensure_vni_egress(9103, EGRESS_DHCP, ["65001:9103"])
    assert any(
        base.IPTABLES_BIN in c and "-D" in c and "203.0.113.0/24" in c for c in calls
    )


def test_a_new_egress_waits_for_the_host_network_snapshot(dirs):
    calls = []
    guard = _guard_run(calls)

    def run(cmd):
        if "route" in cmd and "scope" in cmd:
            raise subprocess.CalledProcessError(1, cmd)
        return guard(cmd)

    with mock.patch.object(sh, "run", side_effect=run):
        with pytest.raises(subprocess.CalledProcessError):
            hostns.ensure_vni_egress(9103, EGRESS_DHCP, ["65001:9103"])

    assert not any(base.IPTABLES_BIN in c for c in calls)
    assert not any("del" in c or "add-port" in c for c in calls)
    assert not os.path.exists(hostns.egress_conf_path(9103))


def test_an_egress_left_half_built_is_cleared_before_the_next_build(dirs):
    """The config is written last: a build that failed after `link add`
    leaves veth ends and no config, and building on top of them fails on
    EEXIST for as long as they are there."""
    calls = []
    with mock.patch.object(sh, "run", side_effect=_guard_run(calls)):
        hostns.ensure_vni_egress(9105, EGRESS_DHCP, ["65001:9105"])
    joined = [" ".join(str(x) for x in c) for c in calls]
    first_add = next(i for i, j in enumerate(joined) if "link add gw9105" in j)
    assert any("link del gw9105-br" in j for j in joined[:first_add])


def test_the_guard_is_asserted_once_per_vni_in_an_iteration(dirs):
    """It belongs to the VNI, and a hypervisor with fifty guests on one
    network must not pay for it fifty times."""
    calls = []
    with mock.patch.object(sh, "run", side_effect=_guard_run(calls)):
        hostns.ensure_vni_egress(9104, EGRESS_DHCP, ["65001:9104"])
        first = len(_inserted(calls))
        hostns.ensure_vni_egress(9104, EGRESS_DHCP, ["65001:9104"])
    assert first and len(_inserted(calls)) == first


def test_the_vni_port_is_created_once_and_moved_into_its_namespace(dirs):
    """The port is br-int's (so flows can output to it) and the namespace's
    (so its address and neighbours belong to the tenant). A port that OVS
    recreated in the root namespace is moved back."""
    calls = []
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run(calls=calls)):
        ofport = hostns.ensure_vni_nf_port(7)
    assert ofport == 1001  # what the mocked OVS reports
    assert any(
        c[:4] == [base.OVSVSCTL_BIN, "--may-exist", "add-port", base.BR_INT]
        and hostns.nf_port(7) in c
        for c in calls
    )
    assert any(
        c[:3] == [base.IP_BIN, "link", "set"] and "netns" in c and hostns.vni_ns(7) in c
        for c in calls
    )
    # the metadata address is on it, inside the namespace
    assert any(
        c[:4] == [base.IP_BIN, "netns", "exec", hostns.vni_ns(7)]
        and "%s/32" % base.METADATA_IP in c
        for c in calls
    )


def test_the_vni_port_routes_nothing_it_is_handed(dirs):
    """Intercepts claim ports whatever the address, so a guest could punt
    `core:11011` or a host's 179 into the namespace, which routed it on
    past the fabric guard (it watches the gateway leg only). The port may
    reach the namespace's own sockets and nothing through it, from before
    the port exists."""
    calls = []
    inner = _two_bridge_run(calls=calls)

    def run(cmd):
        if "-C" in cmd:
            calls.append(cmd)
            raise subprocess.CalledProcessError(1, cmd)  # nothing installed yet
        return inner(cmd)

    with mock.patch.object(sh, "run", side_effect=run):
        hostns.ensure_vni_nf_port(7)
    drop = next(
        i
        for i, c in enumerate(calls)
        if c[:4] == [base.IP_BIN, "netns", "exec", hostns.vni_ns(7)]
        and ("-I" in c or "-A" in c)
    )
    rule = calls[drop]
    assert rule[rule.index("-t") + 1 :][:3] in (
        ["mangle", "-I", "FORWARD"],
        ["mangle", "-A", "FORWARD"],
    )
    assert rule[-2:] == ["-j", "DROP"]
    assert rule[rule.index("-i") + 1] == hostns.nf_port(7)
    plugged = next(
        i
        for i, c in enumerate(calls)
        if c[:3] == [base.OVSVSCTL_BIN, "--may-exist", "add-port"]
    )
    assert drop < plugged


def test_a_vni_port_that_lost_its_drop_reports_itself_missing(dirs):
    port, vlan = _installed(dirs)  # STOCK_NFS: dhcp + dns + proxy

    with _healthy_dp(port, vlan, nf_guarded={port.vni: True}):
        port.restore_from_dp()
    # a namespace that could not be asked is not one without the drop
    with _healthy_dp(port, vlan, nf_guarded={port.vni: None}):
        port.restore_from_dp()
    with _healthy_dp(port, vlan, nf_guarded={port.vni: False}):
        with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
            port.restore_from_dp()


def test_guest_without_an_ofport_is_retriable(dirs):
    """OVS knows the interface but has not assigned a datapath port yet."""
    port = _port()

    def run(cmd):
        if "find" in cmd and "Interface" in cmd:
            return "vethg\n"
        if cmd[:3] == ["ovs-vsctl", "get", "interface"] and cmd[3] == "vethg":
            return "-1\n"
        if cmd[:3] == ["ovs-vsctl", "get", "interface"]:
            return "1001\n"
        return ""

    with mock.patch.object(sh, "run", side_effect=run):
        with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
            port.dump_to_dp()


def test_egress_export_rt_is_derived_from_the_underlay_address():
    """Two hosts must never share the leg RT (a collision re-creates the
    hairpin the unique RT exists to prevent)."""

    def run_for(ip):
        def run(cmd):
            if cmd[0] == base.IP_BIN and "route" in cmd:
                return json.dumps([{"prefsrc": ip}])
            return ""

        return run

    with mock.patch.object(sh, "run", side_effect=run_for("10.77.0.11")):
        first = hostns.egress_exp_rt("65001", 10000)
    with mock.patch.object(sh, "run", side_effect=run_for("10.77.0.12")):
        second = hostns.egress_exp_rt("65001", 10000)
    assert first != second
    assert int(first.split(":")[1]) < 2**32


def test_a_second_guest_does_not_disturb_the_shared_vni_port(dirs):
    """The VNI's port is shared by every guest on it, and `ip link set …
    address` bounces the link even when it sets the address already there —
    taking the interface's neighbour table, and with it the metadata path
    of every guest whose apply is not the one running, down with it. A port
    that is already as it should be gets no commands at all."""
    port = _port()
    calls = []
    healthy = json.dumps(
        [
            {
                "ifname": hostns.nf_port(port.vni),
                "address": base.METADATA_MAC,
                "flags": ["BROADCAST", "MULTICAST", "UP", "LOWER_UP"],
            }
        ]
    )

    def run(cmd):
        calls.append(cmd)
        if "-json" in cmd and "link" in cmd and "show" in cmd:
            return healthy
        if "find" in cmd and "Interface" in cmd:
            return "vethg\n"
        if "get" in cmd and "interface" in cmd and cmd[-1] == "ofport":
            return "1001\n"
        return ""

    with mock.patch.object(sh, "run", side_effect=run):
        port.dump_to_dp()

    touched = [
        c
        for c in calls
        if hostns.nf_port(port.vni) in c
        and "link" in c
        and ("address" in c or c[-1] == "up")
    ]
    assert not touched

    # ... while a port that is not where it belongs is still built
    calls.clear()
    other = _port()
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run(calls=calls)):
        other.dump_to_dp()
    assert [c for c in calls if "address" in c and base.METADATA_MAC in c]


def test_the_boot_script_a_guest_is_sent_for_exists():
    """The control plane has pointed guests at `/boot/boot.ipxe` since the
    function existed, and it was served out of a directory on disk that only
    ever got created empty — so netboot over an overlay answered 404 and said
    nothing about why. The script is a constant: iPXE fills in `${uuid}` from
    the machine's own SMBIOS, which is the uuid the boot API answers for. So
    the daemon composes it, and there is no file to be missing."""
    assert evpn_proxy.BOOT_SCRIPT.startswith("#!ipxe")
    assert "chain http://169.254.169.254/v1/boots/${uuid}" in evpn_proxy.BOOT_SCRIPT


def test_the_netboot_rom_is_answered_where_it_asks(dirs):
    """A guest with no disk runs iPXE from its ROM, and that ROM carries a
    script of its own: it asks the address DHCP gave it, on the boot API's
    port, without ever reading the boot filename it was handed. On an
    overlay there is nothing at that address, so it retried for ever and the
    console said nothing at all. The port is claimed like DNS is — whatever
    address the guest believes it is talking to."""
    port, _vlan = _installed(dirs)  # STOCK_NFS: dhcp + dns + proxy
    bundles = []
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run(bundles=bundles)):
        port.dump_to_dp()
    installed = "\n".join(line for bundle in bundles for line in bundle)
    assert "tp_dst=%d" % base.BOOT_CHAIN_PORT in installed

    # A port with no proxy function does not have its boot traffic taken.
    bare = _port(nfs=[{"kind": "dhcp", "config": {}}])
    bundles.clear()
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run(bundles=bundles)):
        bare.dump_to_dp()
    installed = "\n".join(line for bundle in bundles for line in bundle)
    assert "tp_dst=%d" % base.BOOT_CHAIN_PORT not in installed


def test_a_port_stamps_what_it_is_on_everything_it_sends(dirs):
    """A rule that names a group only works if the packets carry one. The
    identity is the port's, not its filter's: a port with no group of its own
    is still a sender others may name, so the stamp goes on either way — and
    before the pipeline, because the mark has to be on the packet by the time
    it reaches the fabric."""
    filtered = _port(
        identity=42, security_rules=[{"direction": "egress", "proto": "icmp"}]
    )
    bundles = []
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run(bundles=bundles)):
        filtered.dump_to_dp()
    entry = [
        line
        for bundle in bundles
        for line in bundle
        # The IP way into the pipeline. Port security's ARP allow sits a
        # priority below on purpose, and carries no mark — the identity is
        # what the fabric reads off an IP packet.
        if "table=0,priority=90" in line and "in_port" in line
    ]
    assert entry and all("set_field:42->pkt_mark" in line for line in entry)

    # ... and a port with no rules at all still says who it is.
    bare = _port(identity=7, security_rules=[])
    bundles.clear()
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run(bundles=bundles)):
        bare.dump_to_dp()
    stamped = [
        line for bundle in bundles for line in bundle if "set_field:7->pkt_mark" in line
    ]
    assert stamped, "an unfiltered port is still an identified one"

    # An unidentified port stamps nothing rather than stamping zero.
    plain = _port(security_rules=[])
    bundles.clear()
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run(bundles=bundles)):
        plain.dump_to_dp()
    assert not [ln for bundle in bundles for ln in bundle if "pkt_mark" in ln]


def test_a_sender_in_several_groups_is_admitted_by_each(dirs):
    """The mark is a set, not a name. A guest that is both a web server and
    a database carries both bits, and a rule that named either one has to
    admit it without knowing about the other — which is the whole reason the
    match carries its own mask."""
    both = _port(identity=0x6, security_rules=[])  # bits 2 and 4
    bundles = []
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run(bundles=bundles)):
        both.dump_to_dp()
    assert [
        line for bundle in bundles for line in bundle if "set_field:6->pkt_mark" in line
    ], "the sender carries every group it is in"

    # Either bit admits it; a bit it does not carry does not.
    assert (
        evpn_flows.compile_sg_match(
            {"direction": "ingress", "proto": "any", "remote_identity": 0x2}
        )
        == "ip,pkt_mark=2/2"
    )
    assert (
        evpn_flows.compile_sg_match(
            {"direction": "ingress", "proto": "any", "remote_identity": 0x4}
        )
        == "ip,pkt_mark=4/4"
    )


def test_a_function_may_not_be_reached_under_a_forged_address(dirs):
    """The intercepts sit above the port-security drop, so they need its match.

    Otherwise a guest spoofing a neighbour's address could ask the metadata
    proxy about that neighbour. DHCP is exempt: the guest has no address yet.
    """
    port = _port(dhcp={"cidr": "10.42.0.0/24"})
    bundles = []
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run(bundles=bundles)):
        port.dump_to_dp()
    # Every bundle: the intercepts and the security group pass through one
    # scratch file, so reading the file back would only show the last of them.
    lines = [
        line for bundle in bundles for line in bundle if line.startswith("flow add")
    ]

    def _claimed(match):
        return [line for line in lines if match in line]

    # DNS, the platform ports and the metadata punt all carry the source.
    for match in ("tp_dst=53", "tp_dst=11011", "nw_dst=%s" % base.METADATA_IP):
        claimed = _claimed(match)
        assert claimed, match
        assert all(
            "nw_src=10.42.0.7" in line and "dl_src=52:54:00:aa:bb:cc" in line
            for line in claimed
        ), (match, claimed)

    # ... and DHCP does not, because it cannot.
    dhcp = _claimed("tp_dst=67")
    assert dhcp and all("nw_src=" not in line for line in dhcp), dhcp


def test_the_last_guest_takes_the_responder_with_it(dirs):
    """A VNI's directory holds guests *and* what its functions answer with,
    and the two are collected on different schedules. Counting a function's
    settings as a guest meant the last guest of a host never looked like the
    last one, so the daemon serving nobody was never stopped."""
    records = dirs / "nf"
    (records / "10001").mkdir(parents=True)
    (records / "10001" / "nf-dhcp.json").write_text("{}")
    (records / "10001" / "nf-dns.json").write_text("{}")
    assert not nf.has_served_guests()

    (records / "10001" / "abc.json").write_text("{}")
    assert nf.has_served_guests()


def _addr_show(addresses):
    return json.dumps(
        [
            {
                "ifname": "eth0",
                "addr_info": [{"family": "inet", "local": a} for a in addresses],
            }
        ]
    )


def test_the_fabrics_address_does_not_follow_the_route_table(dirs):
    """A route-table twitch — a second uplink coming up, a lease renewed —
    changes what `ip route get` prefers without this host losing anything.
    That answer reaches both rendered configs, and a change in it restarts
    gobgpd *and* evpn_connector, which rewrites the whole flow table of the
    EVPN bridge: every guest on the host pays for it."""
    host = _host(rr_addresses=["10.77.0.10"])
    sh.write_atomic(
        os.path.join(base.GOBGP_PARTS_DIR, "%s.json" % host.PART_NAME),
        json.dumps({"global": '  router-id = "10.20.0.2"\n', "body": ""}),
    )

    def run(cmd):
        if "route" in cmd and "get" in cmd:
            return json.dumps([{"prefsrc": "192.168.100.5"}])
        if "addr" in cmd and "show" in cmd:
            return _addr_show(["10.20.0.2", "192.168.100.5"])
        return ""

    with mock.patch.object(sh, "run", side_effect=run):
        assert host._source_ip() == "10.20.0.2"

    # An address the host has actually lost is a different matter.
    def gone(cmd):
        if "addr" in cmd and "show" in cmd:
            return _addr_show(["192.168.100.5"])
        return run(cmd)

    with mock.patch.object(sh, "run", side_effect=gone):
        assert host._source_ip() == "192.168.100.5"


def test_a_host_that_cannot_be_asked_keeps_the_address_it_has(dirs):
    """ "I could not check" is not "tear the fabric down and rebuild it"."""
    host = _host(rr_addresses=["10.77.0.10"])
    sh.write_atomic(
        os.path.join(base.GOBGP_PARTS_DIR, "%s.json" % host.PART_NAME),
        json.dumps({"global": '  router-id = "10.20.0.2"\n', "body": ""}),
    )

    def run(cmd):
        if "route" in cmd and "get" in cmd:
            return json.dumps([{"prefsrc": "192.168.100.5"}])
        if "addr" in cmd and "show" in cmd:
            raise subprocess.CalledProcessError(1, cmd)
        return ""

    with mock.patch.object(sh, "run", side_effect=run):
        assert host._source_ip() == "10.20.0.2"


def test_the_first_render_takes_what_the_route_says(dirs):
    """Stickiness is about not moving, not about refusing to start."""
    host = _host(rr_addresses=["10.77.0.10"])
    with mock.patch.object(
        sh,
        "run",
        side_effect=lambda cmd: json.dumps([{"prefsrc": "10.20.0.2"}]),
    ):
        assert host._source_ip() == "10.20.0.2"


def test_the_host_resolves_its_address_once_for_both_daemons(dirs):
    """Two resolutions can answer twice, and the two answers are a gobgpd
    and a forwarder that disagree about which address this host is."""
    host = _host(rr_addresses=["10.77.0.10"])
    answers = iter(["10.20.0.2", "192.168.100.5"])
    seen = []

    def run(cmd):
        if "route" in cmd and "get" in cmd:
            resolved = next(answers)
            seen.append(resolved)
            return json.dumps([{"prefsrc": resolved}])
        if "addr" in cmd and "show" in cmd:
            return _addr_show(["10.20.0.2", "192.168.100.5"])
        return ""

    with mock.patch.object(sh, "run", side_effect=run):
        host.dump_to_dp()

    assert seen == ["10.20.0.2"], "the address is resolved once per apply"
    conf = open(base.CONNECTOR_CONF_FILE).read()
    assert 'source_ip = "10.20.0.2"' in conf


def test_a_port_the_bridge_does_not_claim_is_said_out_loud(dirs, caplog):
    """The platform's ports are claimed by number on br-int and answered by
    the proxy on the same numbers; the control plane derives its own list
    from the services' settings. An installation that moves an API lands
    here with a port nothing steals and nothing listens on — which is a
    black hole, not an error, and impossible to guess from inside a guest."""
    nf = evpn.EvpnNf(
        uuid=sys_uuid.uuid4(),
        kind="proxy",
        vni=10001,
        config={"ports": {"11011": "http://core:11011", "9999": "http://core:9999"}},
    )
    with caplog.at_level("WARNING"):
        nf.dump_to_dp()
    assert "9999" in caplog.text
    assert "11011" not in caplog.text.split("which this data")[0].split("serves")[-1]


def test_the_guard_is_a_set_of_destinations_not_of_services(dirs):
    """The requirement, stated once: what leaves the fabric may not be
    addressed to anything internal — not "not on these ports".

    A service list is wrong the day somebody moves a service, and an
    internal network behind a router is invisible from this host. So the
    check is that every private destination is refused whether or not the
    host has ever heard of it, and that a public one is not.
    """
    import ipaddress
    import re

    calls = []
    with mock.patch.object(sh, "run", side_effect=_guard_run(calls, connected=())):
        hostns.ensure_vni_egress(9105, EGRESS_DHCP, ["65001:9105"])
    blocked = [
        ipaddress.ip_network(re.search(r"-d (\S+)", " ".join(c)).group(1))
        for c in _inserted(calls)
        if "-d " in " ".join(c)
    ]

    def refused(address):
        addr = ipaddress.ip_address(address)
        return any(addr in net for net in blocked)

    # the platform's own world, in every shape it takes
    for address in (
        "10.20.0.2",  # a core on a flat underlay
        "192.168.100.2",  # a nested core behind a border
        "172.20.5.1",  # somebody's management vlan
        "100.100.0.1",  # a provider's CGNAT range
        "169.254.169.254",  # the metadata address of another host
        "10.199.0.1",  # the fabric's own transfer plumbing
    ):
        assert refused(address), address
    # ... and the internet, which is what the leak is for
    for address in ("8.8.8.8", "1.1.1.1", "93.184.216.34"):
        assert not refused(address), address


# --- which route is the gateway ---------------------------------------------


GATEWAY_CASES = (
    # the plain case, and the one that was broken: the default written second
    ([{"to": "0.0.0.0/0", "via": "10.100.0.1"}], "10.100.0.1"),
    (
        [
            {"to": "10.0.0.0/8", "via": "10.100.0.9"},
            {"to": "0.0.0.0/0", "via": "10.100.0.1"},
        ],
        "10.100.0.1",
    ),
    # spelled as `default`, as a route may be
    ([{"to": "default", "via": "10.100.0.2"}], "10.100.0.2"),
    # no default route: no gateway, and so no egress — the same answer the
    # guest gets, rather than a namespace holding an address nobody was told
    ([{"to": "10.0.0.0/8", "via": "10.100.0.9"}], None),
    ([], None),
    (None, None),
    # junk in the list is skipped, not crashed on
    (["not-a-route", {"to": "0.0.0.0/0", "via": "10.100.0.1"}], "10.100.0.1"),
)


@pytest.mark.parametrize("routers,expected", GATEWAY_CASES)
def test_the_gateway_is_the_default_route_not_the_first_one(routers, expected):
    """The host builds its egress on the address the guest was handed.

    Taking the first route with a `via` instead meant a subnet whose default
    was written second told the guest 10.100.0.1 and held 10.100.0.9 — the
    guest's traffic left for an address no namespace had.
    """
    assert evpn_records.default_gateway(routers) == expected


def test_the_namespace_is_made_routable_in_the_root_chain(dirs):
    """A hypervisor that runs libvirt refuses to forward into its networks
    by default — libvirt puts a blanket REJECT at the top of its own chains
    and re-adds it whenever a network is created. So the platform's own path
    out of the namespace (the proxy fetching what a guest was told to fetch)
    and the way in to a floating address are asserted at the head of the
    root's FORWARD chain, above the jumps into libvirt's."""
    calls = []
    with mock.patch.object(sh, "run", side_effect=_guard_run(calls)):
        hostns.ensure_vni_egress(9103, EGRESS_DHCP, ["65001:9103"])

    root = [
        " ".join(c)
        for c in calls
        if base.IPTABLES_BIN in c
        and "netns" not in c
        and hostns.TRANSFER_FORWARD_TAG in " ".join(c)
        and " -I " in " ".join(c)
    ]
    assert any("-i upl9103" in rule for rule in root), "what leaves the namespace"
    assert any("-o upl9103" in rule for rule in root), "and what is delivered into it"
    for rule in root:
        assert "-I FORWARD" in rule, "at the head, above libvirt's own chains"


@pytest.mark.parametrize("port_security", [False, True])
def test_nf_identity_is_pinned_even_without_general_anti_spoof(port_security):
    with mock.patch.object(sh, "run_flow_bundle") as bundle:
        evpn_flows.ensure_port_nf_intercepts(
            {"dhcp": {}, "dns": {}, "proxy": {"ports": [11020]}},
            7,
            42,
            "52:54:00:aa:bb:cc",
            "0x1",
            sources=["10.42.0.7"],
            port_security=port_security,
        )
    intercepts = [line for line in bundle.call_args.args[0] if "tp_dst=" in line]
    assert intercepts
    for line in intercepts:
        if "tp_dst=67," in line:
            assert "nw_src=" not in line
        else:
            assert "nw_src=10.42.0.7," in line
            assert "dl_src=52:54:00:aa:bb:cc," in line


def test_nf_without_an_address_only_allows_dhcp():
    with mock.patch.object(sh, "run_flow_bundle") as bundle:
        evpn_flows.ensure_port_nf_intercepts(
            {"dhcp": {}, "dns": {}, "proxy": {}},
            7,
            42,
            "52:54:00:aa:bb:cc",
            "0x1",
            sources=[],
        )
    intercepts = [line for line in bundle.call_args.args[0] if "tp_dst=" in line]
    assert len(intercepts) == 1
    assert "tp_dst=67," in intercepts[0]


def test_failed_sg_revocation_is_retried(tmp_path):
    path = tmp_path / "rules.json"
    previous = [{"direction": "egress", "proto": "tcp", "port": 443}]
    wanted = [{"direction": "egress", "proto": "none"}]
    with mock.patch.object(base, "SG_RULES_PATH", str(path)):
        evpn_flows.cut_revoked_sessions("port", 42, previous, "10.42.0.7")
        with mock.patch.object(sh, "appctl") as flush:
            flush.side_effect = subprocess.CalledProcessError(1, "ovs-appctl")
            with pytest.raises(subprocess.CalledProcessError):
                evpn_flows.cut_revoked_sessions("port", 42, wanted, "10.42.0.7")
            assert json.loads(path.read_text())["port"] == evpn_flows._canonical_rules(
                previous
            )
            flush.side_effect = None
            evpn_flows.cut_revoked_sessions("port", 42, wanted, "10.42.0.7")
            assert flush.call_count == 2
            assert json.loads(path.read_text())["port"] == evpn_flows._canonical_rules(
                wanted
            )
            evpn_flows.cut_revoked_sessions("port", 42, wanted, "10.42.0.7")
            assert flush.call_count == 2


def test_old_unguarded_nf_flows_are_repaired(dirs):
    port = _port(port_security=False)
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run()):
        port.dump_to_dp()
    vlan = hostns.lookup_local_vlan(port.uuid)
    nf_cookie = evpn_flows.port_cookie(port.uuid, "nf")
    with _healthy_dp(
        port,
        vlan,
        cookies={nf_cookie},
        ports={"pi%d" % vlan, hostns.nf_port(port.vni)},
        unprotected_nf={nf_cookie},
    ):
        with pytest.raises(driver_exc.InvalidDataPlaneObjectError):
            port.restore_from_dp()


@pytest.mark.parametrize("suffix", [",actions=output:42", " actions=output:42"])
def test_snapshot_distinguishes_dhcp_from_unguarded_platform_flows(suffix):
    dumped = (
        "cookie=0x1,priority=100,udp,in_port=7,tp_dst=67" + suffix + "\n"
        "cookie=0x2,priority=110,tcp,in_port=8,tp_dst=80" + suffix + "\n"
        "cookie=0x3,priority=110,tcp,dl_src=52:54:00:aa:bb:cc,"
        "nw_src=10.42.0.7,in_port=9,tp_dst=80" + suffix
    )
    with (
        mock.patch.object(sh, "ofctl", return_value=dumped),
        mock.patch.object(state, "snapshot_ovs", return_value={}),
        mock.patch.object(state, "snapshot_plugged_ifaces", return_value=None),
        mock.patch.object(hostns, "netns_names", return_value=set()),
        mock.patch.object(hostns, "forwarded_vnis", return_value=None),
        mock.patch.object(state, "service_states", return_value={}),
    ):
        snapshot = state.snapshot_dataplane()
    assert snapshot["unprotected_nf"] == {"0x2"}
    assert snapshot["cookies"] == {1, 2, 3}


@pytest.mark.parametrize("input_guard", [False, True])
def test_nf_guard_closes_the_gateway_path_to_listeners(input_guard):
    saved = "-A FORWARD -i nf10000 -m comment --comment evpn-nf-guard -j DROP\n"
    if input_guard:
        saved += (
            "-A INPUT -i gw10000 -p tcp -m comment --comment evpn-nf-guard -j DROP\n"
        )
    with mock.patch.object(hostns, "_iptables_save", return_value=saved):
        assert hostns.nf_guarded("vni10000") is input_guard


def test_nf_apply_blocks_gateway_tcp_input_without_blocking_egress():
    with mock.patch.object(hostns, "ensure_rule") as rules:
        hostns.ensure_nf_guard(10000)
    assert (
        mock.call(
            "INPUT",
            "-i",
            "gw10000",
            "-p",
            "tcp",
            "-m",
            "comment",
            "--comment",
            hostns.NF_GUARD_TAG,
            "-j",
            "DROP",
            ns="vni10000",
            table="mangle",
        )
        in rules.call_args_list
    )


def test_nf_unit_uses_the_agents_existing_host_key(tmp_path):
    key = tmp_path / "private_key"
    key.write_text("secret")
    host = sys_uuid.uuid4()
    with (
        mock.patch.object(
            nf.cfg,
            "CONF",
            mock.Mock(universal_agent=mock.Mock(private_key_path=str(key))),
        ),
        mock.patch.object(nf.ua_utils, "node_uuid", return_value=host),
    ):
        unit = nf.render_nf_unit()
    assert "EXORDOS_BOOT_PROXY_KEY_FILE=%s" % key in unit
    assert "EXORDOS_BOOT_PROXY_HOST=%s" % host in unit
    assert "secret" not in unit


def test_existing_nf_unit_without_delegation_is_reconciled(tmp_path):
    unit = tmp_path / "nf.service"
    unit.write_text("old responder unit")
    with (
        mock.patch.object(base, "NF_UNIT_PATH", str(unit)),
        mock.patch.object(nf, "render_nf_unit", return_value="signed responder unit"),
        mock.patch.object(state, "service_is_down", return_value=False),
    ):
        assert nf.missing_nf_responder() == ["nf responder credentials"]
        unit.write_text("signed responder unit")
        assert nf.missing_nf_responder() == []


# Shared gateway state must include every subnet served on the host.
def test_two_subnets_keep_both_gateways_and_shared_nat(dirs):
    first = _port(dhcp=EGRESS_DHCP)
    second = _port(
        ipv4="10.43.0.7",
        dhcp={
            **EGRESS_DHCP,
            "cidr": "10.43.0.0/24",
            "routers": [{"to": "default", "via": "10.43.0.1"}],
        },
    )
    calls = []
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run(calls=calls)):
        first.dump_to_dp()
        calls.clear()
        second.dump_to_dp()
        first.dump_to_dp()
    conf = json.loads(Path(hostns.egress_conf_path(first.vni)).read_text())
    assert conf["routes"] == ["10.42.0.1/32", "10.43.0.1/32", "0.0.0.0/0"]
    assert not any("link" in c and "del" in c for c in calls)
    assert not any("-D" in c and "MASQUERADE" in c for c in calls)
    assert any("10.43.0.1/24" in c and "replace" in c for c in calls)


def test_deleting_last_port_of_subnet_removes_only_its_gateway(dirs):
    first = _port(dhcp=EGRESS_DHCP)
    second = _port(
        ipv4="10.43.0.7",
        dhcp={
            **EGRESS_DHCP,
            "cidr": "10.43.0.0/24",
            "routers": [{"to": "default", "via": "10.43.0.1"}],
        },
    )
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run()):
        first.dump_to_dp()
        second.dump_to_dp()
    calls = []

    def run(cmd):
        calls.append(cmd)
        if "-json" in cmd and "addr" in cmd:
            return json.dumps(
                [
                    {
                        "addr_info": [
                            {"family": "inet", "local": "10.42.0.1", "prefixlen": 24},
                            {"family": "inet", "local": "10.43.0.1", "prefixlen": 24},
                        ]
                    }
                ]
            )
        return _two_bridge_run()(cmd)

    with mock.patch.object(sh, "run", side_effect=run):
        second.delete_from_dp()
    conf = json.loads(Path(hostns.egress_conf_path(first.vni)).read_text())
    assert conf["routes"] == ["10.42.0.1/32", "0.0.0.0/0"]
    assert any("10.43.0.1/24" in c and "del" in c for c in calls)
    assert not any("link" in c and "del" in c and "gw10001" in c for c in calls)
    assert not any("-D" in c and "MASQUERADE" in c for c in calls)


def test_egress_declarations_work_without_network_functions(dirs):
    port = _port(nfs=[], dhcp=EGRESS_DHCP)
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run()):
        port.dump_to_dp()
    assert os.path.exists(hostns.egress_port_path(port.vni, port.uuid))
    assert not os.path.exists(port._nf_record_path)
    assert hostns._egress_demand(port.vni).render({})[0] == {
        "10.42.0.1": "10.42.0.1/24"
    }


def test_existing_nf_records_preserve_gateways_during_upgrade(dirs):
    path = dirs / "nf" / "10001" / "old-port.json"
    sh.write_atomic(str(path), json.dumps(EGRESS_DHCP))
    new = {
        **EGRESS_DHCP,
        "cidr": "10.43.0.0/24",
        "routers": [{"to": "default", "via": "10.43.0.1"}],
    }
    assert hostns._egress_demand(10001).render(new)[0] == {
        "10.42.0.1": "10.42.0.1/24",
        "10.43.0.1": "10.43.0.1/24",
    }
    sh.write_atomic(hostns.egress_port_path(10001, "old-port"), "{}")
    assert hostns._egress_demand(10001).render(new)[0] == {"10.43.0.1": "10.43.0.1/24"}


def test_egress_reads_peer_records_and_asserts_shared_gateway_once_per_cycle(dirs):
    ports = [sys_uuid.uuid4() for _ in range(4)]
    for port in ports:
        sh.write_atomic(hostns.egress_port_path(10001, port), json.dumps(EGRESS_DHCP))
        sh.write_atomic(
            str(dirs / "nf" / "10001" / (str(port) + ".json")), json.dumps(EGRESS_DHCP)
        )
    sh.write_atomic(
        hostns.egress_conf_path(10001),
        json.dumps({"routes": ["10.42.0.1/32", "0.0.0.0/0"]}),
    )
    agent = object.__new__(evpn.EvpnAgentCapabilityDriver)
    agent._storage = mock.Mock()
    reads, calls = [], []
    load = json.load

    def counted_load(stream):
        if str(stream.name).startswith((base.NF_RECORDS_DIR, base.EGRESS_RECORDS_DIR)):
            reads.append(stream.name)
        return load(stream)

    with (
        mock.patch.object(state, "snapshot_dataplane", side_effect=_dp),
        mock.patch.object(json, "load", side_effect=counted_load),
        mock.patch.object(sh, "run", side_effect=_two_bridge_run(calls=calls)),
    ):
        for _ in range(2):
            agent.start()
            try:
                for port in ports:
                    hostns.ensure_vni_egress(10001, EGRESS_DHCP, ["65001:10001"], port)
            finally:
                agent.finalize()
    assert len(reads) == 16
    assert sum("addr" in call and "replace" in call for call in calls) == 2


def test_egress_cycle_tracks_gateway_and_mtu_changes_and_deletion(dirs, monkeypatch):
    monkeypatch.setattr(hostns, "_EGRESS_DEMANDS", {})
    second = {
        **EGRESS_DHCP,
        "cidr": "10.43.0.0/24",
        "routers": [{"to": "default", "via": "10.43.0.1"}],
        "mtu": 1400,
    }
    calls = []
    normal_run = _two_bridge_run(calls=calls)

    def run(cmd):
        if "-json" in cmd and "addr" in cmd:
            calls.append(cmd)
            return json.dumps(
                [
                    {
                        "addr_info": [
                            {"family": "inet", "local": "10.42.0.1", "prefixlen": 24},
                            {"family": "inet", "local": "10.43.0.1", "prefixlen": 24},
                        ]
                    }
                ]
            )
        return normal_run(cmd)

    with mock.patch.object(sh, "run", side_effect=run):
        hostns.ensure_vni_egress(10001, EGRESS_DHCP, ["65001:10001"], "first")
        hostns.ensure_vni_egress(10001, EGRESS_DHCP, ["65001:10001"], "second")
        hostns.ensure_vni_egress(10001, second, ["65001:10001"], "second")
        assert hostns._egress_demand(10001).render({}) == (
            {"10.42.0.1": "10.42.0.1/24", "10.43.0.1": "10.43.0.1/24"},
            "1400",
        )
        hostns.ensure_vni_egress(
            10001, {**EGRESS_DHCP, "mtu": 1300}, ["65001:10001"], "first"
        )
        assert hostns._egress_demand(10001).render({})[1] == "1300"
        hostns.forget_egress_port(10001, "first")
        assert hostns._egress_demand(10001).render({}) == (
            {"10.43.0.1": "10.43.0.1/24"},
            "1400",
        )
        conf = json.loads(Path(hostns.egress_conf_path(10001)).read_text())
        assert conf["routes"] == ["10.43.0.1/32", "0.0.0.0/0"]
        assert any("del" in call and "10.42.0.1/24" in call for call in calls)
        hostns.forget_egress_port(10001, "second")
        assert os.path.exists(hostns.egress_conf_path(10001))
        assert hostns._egress_demand(10001).render({}) == ({}, "1450")
        hostns.gc_vni(10001)
        assert 10001 not in hostns._EGRESS_DEMANDS
        hostns.ensure_vni_egress(10001, EGRESS_DHCP, ["65001:10001"], "first")
        assert hostns._egress_demand(10001).render({}) == (
            {"10.42.0.1": "10.42.0.1/24"},
            "1450",
        )


def test_failed_egress_change_does_not_skip_restoring_previous_demand(
    dirs, monkeypatch
):
    monkeypatch.setattr(hostns, "_EGRESS_DEMANDS", {})
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run()):
        hostns.ensure_vni_egress(10001, EGRESS_DHCP, ["65001:10001"], "port")
        with mock.patch.object(hostns, "_sync_egress_gateways") as sync:
            sync.side_effect = subprocess.CalledProcessError(1, "ip addr")
            with pytest.raises(subprocess.CalledProcessError):
                hostns.ensure_vni_egress(
                    10001, {**EGRESS_DHCP, "mtu": 1300}, ["65001:10001"], "port"
                )
            sync.side_effect = None
            hostns.ensure_vni_egress(10001, EGRESS_DHCP, ["65001:10001"], "port")
            assert sync.call_count == 2


def test_egress_prefix_choice_keeps_file_order_during_cidr_change(dirs, monkeypatch):
    monkeypatch.setattr(hostns, "_EGRESS_DEMANDS", {})
    changed = {**EGRESS_DHCP, "cidr": "10.42.0.0/16"}
    for port in ("a", "b"):
        sh.write_atomic(hostns.egress_port_path(10001, port), json.dumps(EGRESS_DHCP))
    assert hostns._egress_demand(10001).render({})[0] == {"10.42.0.1": "10.42.0.1/24"}
    with mock.patch.object(sh, "run", side_effect=_two_bridge_run()):
        hostns.ensure_vni_egress(10001, changed, ["65001:10001"], "a")
        assert hostns._egress_demand(10001).render({})[0] == {
            "10.42.0.1": "10.42.0.1/24"
        }
        hostns.forget_egress_port(10001, "b")
        assert hostns._egress_demand(10001).render({})[0] == {
            "10.42.0.1": "10.42.0.1/16"
        }


def test_unreadable_gateway_declaration_does_not_destroy_egress(dirs):
    sh.write_atomic(hostns.egress_port_path(10001, "peer"), "broken")
    with mock.patch.object(hostns, "gc_vni_egress") as collect:
        with pytest.raises(ValueError):
            hostns.ensure_vni_egress(10001, EGRESS_DHCP, ["65001:10001"])
        collect.assert_not_called()


def test_no_gateway_demand_does_not_collect_shared_fips(dirs):
    path = hostns.egress_conf_path(10001)
    sh.write_atomic(path, json.dumps({"imp_rt": ["65001:10001"]}))
    with mock.patch.object(hostns, "gc_vni_egress") as collect:
        hostns.ensure_vni_egress(10001, {}, ["65001:10001"])
        collect.assert_not_called()
    assert os.path.exists(path)


def test_missing_namespace_gateway_is_rebuilt(dirs):
    path = hostns.egress_conf_path(10001)
    sh.write_atomic(
        path, json.dumps({"vni": 10001, "routes": ["10.42.0.1/32", "0.0.0.0/0"]})
    )
    calls = []
    with (
        mock.patch.object(sh, "run", side_effect=_two_bridge_run(calls=calls)),
        mock.patch.object(hostns, "link_in_ns", return_value=None),
    ):
        hostns.ensure_vni_egress(10001, EGRESS_DHCP, ["65001:10001"])
    assert any("link" in c and "del" in c and "gw10001" in c for c in calls)
    assert any("link" in c and "add" in c and "gw10001" in c for c in calls)


def test_vni_collection_discards_guard_cache_before_same_iteration_reuse(dirs):
    hostns._GUARDED_VNIS.add(10001)
    with mock.patch.object(sh, "run", return_value=""):
        hostns.gc_vni(10001)
    assert 10001 not in hostns._GUARDED_VNIS


@pytest.mark.parametrize("stale_first", [True, False])
@pytest.mark.parametrize("stale_ofport", [-1, 0, ["set", []]])
def test_snapshot_guest_identity_ignores_stale_taps(stale_first, stale_ofport):
    uid = str(sys_uuid.uuid4())
    rows = [
        {"name": "live-tap", "ofport": 7, "external_ids": ["map", [["iface-id", uid]]]},
        {
            "name": "stale-tap",
            "ofport": stale_ofport,
            "external_ids": ["map", [["iface-id", uid]]],
        },
    ]
    if stale_first:
        rows.reverse()
    with mock.patch.object(state, "ovs_rows", return_value=rows):
        assert state.snapshot_plugged_ifaces() == {uid: "live-tap"}


def test_resolve_guest_identity_ignores_stale_taps():
    port = _port()

    def ovs(*args):
        return "live-tap\n" if "ofport>0" in args else "stale-tap\nlive-tap\n"

    with mock.patch.object(sh, "ovs", side_effect=ovs):
        assert port._resolve_guest_port() == "live-tap"


def test_membership_update_preserves_tracked_sessions(dirs):
    rules = [
        {"direction": "ingress", "proto": "tcp", "port": 8080, "remote_identity": 2}
    ]
    with (
        mock.patch.object(sh, "run_flow_bundle") as bundle,
        mock.patch.object(sh, "appctl") as appctl,
    ):
        for identity in (2, 4):
            evpn_flows.ensure_security_group(
                "7",
                42,
                rules,
                "0x123",
                "52:54:00:aa:bb:cc",
                delivery_ofport="9",
                identity=identity,
                sources=["10.42.0.7"],
            )
        appctl.assert_not_called()
        installed = bundle.call_args.args[0]
        established = [
            line
            for line in installed
            if "ct_state=+est+trk" in line and "dl_dst=" in line
        ]
        assert established and all("pkt_mark" not in line for line in established)
        new_connections = [
            line
            for line in installed
            if "ct_state=+new+trk" in line and "dl_dst=" in line
        ]
        assert new_connections and all(
            "pkt_mark=2/2" in line for line in new_connections
        )
        evpn_flows.ensure_security_group(
            "7",
            42,
            [],
            "0x123",
            "52:54:00:aa:bb:cc",
            delivery_ofport="9",
            identity=4,
            sources=["10.42.0.7"],
        )
        appctl.assert_called_once()
        assert appctl.call_args.args[0] == "dpctl/flush-conntrack"


@pytest.mark.parametrize("initially_invalid", [False, True])
@pytest.mark.parametrize("failure", ["flow", "patch", "nat"])
def test_removed_port_cleanup_retries_after_partial_failure(
    dirs, initially_invalid, failure
):
    from gcl_sdk.agents.universal.services import agent as agent_service

    port = _port()
    capability = base.EVPN_PORT_KIND
    driver = object.__new__(evpn.EvpnAgentCapabilityDriver)
    driver._storage = {
        capability: {"resources": {str(port.uuid): port.dump_to_simple_view()}}
    }
    driver._meta_file = "test-meta"
    service = object.__new__(agent_service.UniversalAgentService)
    failed = False

    def restore(self):
        if initially_invalid or failed:
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})

    def ofctl(*args):
        nonlocal failed
        if failure == "flow" and not failed:
            failed = True
            raise subprocess.CalledProcessError(1, "ovs-ofctl")
        return ""

    def vlan(_):
        nonlocal failed
        if failure == "patch" and not failed:
            failed = True
            return 42
        return None

    def nat(*_):
        nonlocal failed
        if failure == "nat" and not failed:
            failed = True
            raise RuntimeError("unreadable NAT")

    with (
        mock.patch.object(evpn.EvpnPort, "restore_from_dp", restore),
        mock.patch.object(sh, "ofctl", side_effect=ofctl),
        mock.patch.object(hostns, "gc_guest"),
        mock.patch.object(hostns, "lookup_local_vlan", side_effect=vlan),
        mock.patch.object(hostns, "delete_fip_rules", side_effect=nat),
        mock.patch.object(sh, "ovs", return_value=base.BR_INT),
        mock.patch.object(hostns, "vni_in_use", return_value=True),
        mock.patch.object(nf, "has_served_guests", return_value=True),
    ):
        service._actualize_capability(driver, capability, [])
        assert str(port.uuid) in driver._storage[capability]["resources"]
        service._actualize_capability(driver, capability, [])
        assert driver._storage[capability]["resources"] == {}


def test_unreadable_nat_does_not_delete_new_owners_route_or_arp():
    with (
        mock.patch.object(hostns, "_iptables_save", return_value=None),
        mock.patch.object(sh, "run", return_value="vni10001\n") as run,
        mock.patch.object(hostns, "delete_fip_arp") as arp,
    ):
        with pytest.raises(RuntimeError, match="NAT"):
            hostns.delete_fip_rules("evpn-fip-old", 10001)
    assert not any("del" in call.args[0] for call in run.call_args_list)
    arp.assert_not_called()


def test_invalid_desired_port_is_updated_from_retained_metadata(dirs):
    from gcl_sdk.agents.universal.services import agent as agent_service

    port = _port()
    capability = base.EVPN_PORT_KIND
    target = port.to_ua_resource(capability)
    driver = object.__new__(evpn.EvpnAgentCapabilityDriver)
    driver._storage = {
        capability: {"resources": {str(port.uuid): port.dump_to_simple_view()}}
    }
    driver._meta_file = "test-meta"
    service = object.__new__(agent_service.UniversalAgentService)
    with (
        mock.patch.object(
            evpn.EvpnPort,
            "restore_from_dp",
            side_effect=driver_exc.InvalidDataPlaneObjectError(obj={}),
        ),
        mock.patch.object(evpn.EvpnPort, "update_on_dp") as update,
    ):
        applied = service._actualize_capability(driver, capability, [target])
    update.assert_called_once()
    assert len(applied) == 1 and applied[0].hash == target.hash
    assert str(port.uuid) in driver._storage[capability]["resources"]


@pytest.mark.parametrize("failed_read", ["vni-routes", "remaining-route"])
def test_unreadable_routes_preserve_fip_arp(failed_read):
    error = subprocess.CalledProcessError(1, "ip route show")
    replies = (
        [error]
        if failed_read == "vni-routes"
        else ["203.0.113.7 via 10.0.0.1\n", error]
    )
    with (
        mock.patch.object(sh, "run", side_effect=replies),
        mock.patch.object(hostns, "delete_fip_arp") as arp,
    ):
        with pytest.raises(subprocess.CalledProcessError):
            hostns.release_fips(10001, keep=set())
    arp.assert_not_called()


def test_fip_arp_cleanup_retries_before_releasing_its_route():
    _, _, tr_ns = hostns.egress_transfer(10001)
    routed, proxied, attempts = True, True, 0

    def run(cmd):
        nonlocal routed
        if cmd[1:3] == ["route", "show"]:
            return "203.0.113.7 via %s\n" % tr_ns if routed else ""
        if cmd[1:3] == ["route", "del"]:
            routed = False
        return ""

    def delete_arp(_):
        nonlocal proxied, attempts
        attempts += 1
        if attempts == 1:
            raise subprocess.CalledProcessError(1, "ip neigh del proxy")
        proxied = False

    with (
        mock.patch.object(sh, "run", side_effect=run),
        mock.patch.object(hostns, "delete_fip_arp", side_effect=delete_arp),
    ):
        with pytest.raises(subprocess.CalledProcessError):
            hostns.release_fips(10001, keep=set())
        hostns.release_fips(10001, keep=set())
    assert not routed and not proxied and attempts == 2


def test_old_vni_cleanup_preserves_new_vni_fip_route_and_arp():
    _, _, old_next_hop = hostns.egress_transfer(10001)
    _, _, new_next_hop = hostns.egress_transfer(10002)
    with (
        mock.patch.object(
            sh,
            "run",
            side_effect=[
                "203.0.113.7 via %s\n" % old_next_hop,
                "203.0.113.7 via %s\n" % new_next_hop,
                "",
            ],
        ) as run,
        mock.patch.object(hostns, "delete_fip_arp") as arp,
    ):
        hostns.release_fips(10001, keep=set())
    arp.assert_not_called()
    assert run.call_args.args[0] == [
        base.IP_BIN,
        "route",
        "del",
        "203.0.113.7/32",
        "via",
        old_next_hop,
    ]


def test_old_nf_delete_preserves_reused_vni_file(dirs):
    old = evpn.EvpnNf(
        uuid=sys_uuid.uuid4(), kind="dns", vni=10005, config={"zone_suffix": "old"}
    )
    new = evpn.EvpnNf(
        uuid=sys_uuid.uuid4(), kind="dns", vni=10005, config={"zone_suffix": "new"}
    )
    old.dump_to_dp()
    new.dump_to_dp()
    path = dirs / "nf" / "10005" / "nf-dns.json"
    old.delete_from_dp()
    assert json.loads(path.read_text())["zone_suffix"] == "new"
    new.delete_from_dp()
    assert not path.exists()


@pytest.mark.parametrize("kind", ["dhcp", "dns", "proxy"])
def test_legacy_shared_nf_claims_preserve_remaining_slice(dirs, kind):
    first = evpn.EvpnNf(uuid=sys_uuid.uuid4(), kind=kind, vni=10005, config={})
    second = evpn.EvpnNf(uuid=sys_uuid.uuid4(), kind=kind, vni=10005, config={})
    path = dirs / "nf" / "10005" / ("nf-%s.json" % kind)
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    capability = base.EVPN_NF_KIND
    agent = object.__new__(evpn.EvpnAgentCapabilityDriver)
    agent._meta_file = "test-meta"
    agent._storage = {
        capability: {
            "resources": {
                str(obj.uuid): obj.dump_to_simple_view() for obj in (first, second)
            }
        }
    }
    resources = {resource.uuid: resource for resource in agent.list(capability)}
    mtime = path.stat().st_mtime_ns
    agent.list(capability)
    assert path.stat().st_mtime_ns == mtime
    agent.delete(resources[first.uuid])
    assert json.loads(path.read_text())["_resource_configs"] == {
        str(second.uuid): dict(second.config)
    }
    agent.delete(resources[second.uuid])
    assert not path.exists()
    assert agent._storage[capability]["resources"] == {}


def test_withdrawing_last_nf_writer_restores_surviving_settings(dirs):
    first = evpn.EvpnNf(
        uuid=sys_uuid.uuid4(), kind="dns", vni=10005, config={"zone_suffix": "first"}
    )
    second = evpn.EvpnNf(
        uuid=sys_uuid.uuid4(), kind="dns", vni=10005, config={"zone_suffix": "second"}
    )
    first.dump_to_dp()
    second.dump_to_dp()
    second.delete_from_dp()
    path = dirs / "nf" / "10005" / "nf-dns.json"
    assert json.loads(path.read_text())["zone_suffix"] == "first"
