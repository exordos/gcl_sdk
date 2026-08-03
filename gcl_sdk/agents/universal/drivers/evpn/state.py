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

"""What the host actually holds, read once per iteration.

``None`` everywhere means *not determined*, never *absent*: a false
"missing" recreates a resource on every iteration, for ever.
"""

import json
import logging
import re
import subprocess

from gcl_sdk.agents.universal.drivers import evpn_records
from gcl_sdk.agents.universal.drivers.evpn import base
from gcl_sdk.agents.universal.drivers.evpn import hostns
from gcl_sdk.agents.universal.drivers.evpn import sh

LOG = logging.getLogger(__name__)


# One read of the host per driver iteration, shared by every port
# (see `restore_from_dp`) instead of asking OVS once per guest.
_DP_SNAPSHOT: dict | None = None


# `activating` counts as up: a check during a `Restart=always` gap would
# otherwise recreate every guest for a daemon that is coming back anyway.
_SERVICE_UP = ("active", "activating", "reloading")


def dp_defaults() -> dict:
    """The empty snapshot: every fact ``None`` (not determined)."""
    return {
        "cookies": None,
        "unprotected_nf": None,
        "ports": None,
        "evpn_ports": None,
        "tags": None,
        "ifaces": None,
        "netns": None,
        "neigh": None,
        "nf_guarded": None,
        "forwarded": None,
        "services": None,
    }


def snapshot_dataplane() -> dict:
    """Everything a resource checks itself against, in a fixed number of calls."""
    snapshot = dp_defaults()
    try:
        flows = sh.ofctl("dump-flows", base.BR_INT)
        snapshot["cookies"] = set(re.findall(r"cookie=(0x[0-9a-f]+)", flows))
        snapshot["unprotected_nf"] = {
            match.group(1)
            for line in flows.splitlines()
            if "tp_dst=" in line
            and not re.search(r"\btp_dst=67(?:,|\s)", line)
            and ("dl_src=" not in line or "nw_src=" not in line)
            if (match := re.search(r"cookie=(0x[0-9a-f]+)", line))
        }
    except (subprocess.CalledProcessError, FileNotFoundError):
        # A bridge that cannot be dumped says nothing about the flows on it.
        pass
    snapshot.update(snapshot_ovs())
    snapshot["ifaces"] = snapshot_plugged_ifaces()
    try:
        snapshot["netns"] = hostns.netns_names()
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    snapshot["neigh"] = snapshot_neighbours(snapshot["netns"])
    snapshot["nf_guarded"] = snapshot_nf_guards(snapshot["netns"])
    snapshot["forwarded"] = hostns.forwarded_vnis()
    snapshot["services"] = service_states(
        (base.NF_SERVICE, base.GOBGP_SERVICE, base.CONNECTOR_SERVICE)
    )
    return snapshot


def snapshot_neighbours(namespaces: set | None) -> dict | None:
    """Each VNI namespace's permanent neighbours, keyed by VNI.

    The driver programs them (the namespace can't ARP for a guest behind a
    flow) and they vanish when the interface goes down, silently cutting the
    metadata proxy off. ``None`` (here or per VNI) means not asked.
    """
    if namespaces is None:
        return None
    found: dict = {}
    for ns in namespaces:
        vni = evpn_records.ns_vni(ns)
        if vni is None:
            continue
        try:
            out = sh.ns_ip(ns, "-json", "neigh", "show")
            entries = json.loads(out)
        except (
            subprocess.CalledProcessError,
            FileNotFoundError,
            ValueError,
            TypeError,
        ):
            found[vni] = None
            continue
        found[vni] = {
            entry.get("dst"): (entry.get("lladdr") or "").lower()
            for entry in entries
            if isinstance(entry, dict)
            and entry.get("dst")
            and "PERMANENT" in (entry.get("state") or [])
        }
    return found


def snapshot_nf_guards(namespaces: set | None) -> dict | None:
    """Whether each VNI namespace holds its NF drop, keyed by VNI.

    ``None`` (here or per VNI) means not asked.
    """
    if namespaces is None:
        return None
    return {
        evpn_records.ns_vni(ns): hostns.nf_guarded(ns)
        for ns in namespaces
        if evpn_records.ns_vni(ns) is not None
    }


def ovs_rows(table: str, columns: str) -> list[dict] | None:
    """``ovs-vsctl list <table>`` as dicts, or None when it cannot be read."""
    try:
        out = sh.ovs("--format=json", "--columns=%s" % columns, "list", table)
        parsed = json.loads(out)
        headings = parsed["headings"]
        return [dict(zip(headings, row)) for row in parsed["data"]]
    except (
        subprocess.CalledProcessError,
        FileNotFoundError,
        ValueError,
        KeyError,
        TypeError,
    ):
        return None


def ovs_uuid(value: object) -> str | None:
    """``["uuid", "<id>"]`` — how ovs-vsctl renders a row reference."""
    if isinstance(value, list) and len(value) == 2 and value[0] == "uuid":
        return value[1]
    return None


def ovs_uuid_set(value: object) -> list[str]:
    """A column of row references; ovs collapses a one-element set to the row."""
    if isinstance(value, list) and len(value) == 2 and value[0] == "set":
        return [uuid for uuid in map(ovs_uuid, value[1]) if uuid]
    single = ovs_uuid(value)
    return [single] if single else []


def snapshot_ovs() -> dict:
    """Each bridge's ports and every port's access VLAN, in two queries.

    Via the Bridge table, not ``list-ports <bridge>``, which logs an error
    on every non-hypervisor. A missing br-int reads as empty (its apply
    recreates it); a missing EVPN bridge stays unknown, since recreating a
    guest port cannot bring back evpn_connector's bridge.
    """
    ports = ovs_rows("Port", "_uuid,name,tag")
    bridges = ovs_rows("Bridge", "name,ports")
    if ports is None or bridges is None:
        return {"ports": None, "evpn_ports": None, "tags": None}
    names, tags = {}, {}
    for row in ports:
        uuid, name = ovs_uuid(row.get("_uuid")), row.get("name")
        if not uuid or not isinstance(name, str):
            continue
        names[uuid] = name
        tags[name] = row["tag"] if isinstance(row.get("tag"), int) else None
    on_bridge = {}
    for row in bridges:
        name = row.get("name")
        if not isinstance(name, str):
            continue
        on_bridge[name] = {
            names[uuid] for uuid in ovs_uuid_set(row.get("ports")) if uuid in names
        }
    return {
        "ports": on_bridge.get(base.BR_INT, set()),
        "evpn_ports": on_bridge.get(base.EVPN_BRIDGE),
        "tags": tags,
    }


def snapshot_plugged_ifaces() -> dict | None:
    """Guest identity (``iface-id``) -> OVS interface name."""
    rows = ovs_rows("Interface", "name,external_ids")
    if rows is None:
        return None
    plugged = {}
    for row in rows:
        name, external = row.get("name"), row.get("external_ids")
        if not isinstance(name, str) or not isinstance(external, list):
            continue
        if len(external) != 2 or external[0] != "map":
            continue
        ids = {k: v for k, v in external[1] if isinstance(v, str)}
        if ids.get("iface-id"):
            plugged[ids["iface-id"].lower()] = name
    return plugged


def service_states(names: tuple) -> dict | None:
    """systemd's state for each unit, or None where systemd cannot answer.

    ``is-active`` exits non-zero if any unit is down; states are on stdout.
    """
    try:
        out = sh.run([base.SYSTEMCTL_BIN, "is-active"] + list(names))
    except subprocess.CalledProcessError as e:
        out = (e.output or b"").decode()
    except FileNotFoundError:
        return None
    states = [line.strip() for line in out.splitlines() if line.strip()]
    if len(states) != len(names):
        # A truncated answer says nothing about any particular unit.
        return None
    return dict(zip(names, states))


def service_is_down(name: str) -> bool:
    """True only when systemd positively reports the unit as not running."""
    states = dp()["services"]
    if not states or name not in states:
        return False
    return states[name] not in _SERVICE_UP


def ensure_service_running(name: str) -> None:
    """Start a unit that should be running but is not.

    An unchanged-config apply must still start the daemon, or a "daemon
    down" restore check would recreate the resource forever.
    """
    if not service_is_down(name):
        return
    LOG.warning("evpn: %s is not running, starting it", name)
    sh.run([base.SYSTEMCTL_BIN, "restart", name])


def ensure_service(name: str, path: str, content: str, enable=False) -> None:
    """Write a daemon's config: restart it on a change, else start it if down."""
    if not sh.write_if_changed(path, content):
        ensure_service_running(name)
        return
    if enable:
        sh.run([base.SYSTEMCTL_BIN, "enable", "--now", name])
    sh.run([base.SYSTEMCTL_BIN, "restart", name])


def dp() -> dict:
    """The iteration's snapshot, or a fresh read outside an iteration."""
    if _DP_SNAPSHOT is None:
        return snapshot_dataplane()
    return _DP_SNAPSHOT


def has_cookie(cookie: str) -> bool:
    # ovs-ofctl drops leading zeros the driver writes: compare as numbers.
    # An undumpable table is unknown, not empty.
    cookies = dp()["cookies"]
    if cookies is None:
        return True
    want = int(cookie, 16)
    return any(int(seen, 16) == want for seen in cookies)


def has_port(name: str) -> bool:
    """Is this port on br-int? Unknown counts as present."""
    ports = dp()["ports"]
    return True if ports is None else name in ports
