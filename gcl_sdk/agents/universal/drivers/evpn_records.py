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

"""Reading a VNI's records directory (guest records + ``nf-<kind>.json``).

Shared by the driver, the responder and its proxy; stdlib-only so the
root responder cannot break on the driver's imports.
"""

import glob
import json
import os
import threading
import time

NF_CONFIG_PREFIX = "nf-"

# What a host serves guests with; a kind also names a file on the host.
NF_KINDS = ("dhcp", "dns", "proxy")

# One namespace per VNI per host, holding that VNI's port of br-int.
VNI_NS_PREFIX = "vni"

NF_PORT_PREFIX = "nf"

DEFAULT_ROUTE_DESTINATIONS = ("0.0.0.0/0", "default")


def vni_ns(vni):
    """The host's namespace for this VNI — its tenant boundary."""
    return "%s%s" % (VNI_NS_PREFIX, vni)


def nf_port(vni):
    """This VNI's internal port of br-int, living in its namespace."""
    return "%s%s" % (NF_PORT_PREFIX, vni)


def ns_vni(name):
    """The VNI a namespace name is for, or None when it is not a VNI's."""
    rest = name[len(VNI_NS_PREFIX) :]
    if name.startswith(VNI_NS_PREFIX) and rest.isdigit():
        return int(rest)
    return None


def guest_records(records_dir):
    """Every readable guest record of one VNI (not its ``nf-*.json``)."""
    records = []
    for path in sorted(glob.glob(os.path.join(records_dir, "*.json"))):
        if os.path.basename(path).startswith(NF_CONFIG_PREFIX):
            continue
        try:
            with open(path) as fl:
                rec = json.load(fl)
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records


def nf_config_file(records_dir, kind):
    """Where a VNI's copy of one function's settings lives."""
    return os.path.join(records_dir, "%s%s.json" % (NF_CONFIG_PREFIX, kind))


def nf_config(records_dir, kind):
    """One function's settings for this VNI, as the control plane sent them."""
    try:
        with open(nf_config_file(records_dir, kind)) as fl:
            config = json.load(fl)
    except (OSError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


def default_gateway(routers):
    """The subnet's default route's next hop, or None if it has none.

    The *default* route, not the first one: the responder and the egress
    namespace must agree on the gateway. No default route, no egress.
    """
    for route in routers or []:
        if not isinstance(route, dict):
            continue
        if str(route.get("to")) in DEFAULT_ROUTE_DESTINATIONS:
            return route.get("via")
    return None


# A stamp younger than this is not cached: the kernel's coarse mtime clock
# lets two renames in one tick leave an unchanged stamp.
SETTLE_NS = 1_000_000_000


def stamp(path):
    """What a cache compares a path by, or None when it cannot be read."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def is_settled(stamps, now_ns=None):
    """Whether no change can still arrive under these stamps unseen."""
    now_ns = time.time_ns() if now_ns is None else now_ns
    return all(s is None or now_ns - s[0] >= SETTLE_NS for s in stamps)


class DirCache:
    """What a VNI's directory says, re-read only when its stamp moves.

    Stamps cover the directory (writes are temp+rename) plus any file read
    out of, so an in-place rewrite is seen too. Unsettled stamps are not kept.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._state = {}

    def get(self, directory, key, load, watch=()):
        stamps = tuple(stamp(p) for p in (directory,) + tuple(watch))
        with self._lock:
            cached = self._state.get((directory, key))
            if cached is not None and cached[0] == stamps:
                return cached[1]
        settled = is_settled(stamps)
        value = load(directory)
        with self._lock:
            if settled:
                self._state[(directory, key)] = (stamps, value)
            else:
                self._state.pop((directory, key), None)
        return value
