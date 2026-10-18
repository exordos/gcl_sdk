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

"""Running commands and writing files on the host (tests replace `run`)."""

import logging
import subprocess
import os

from gcl_sdk.agents.universal.drivers.evpn import base

LOG = logging.getLogger(__name__)


def ip(*args: str) -> str:
    """`ip` in the root namespace — the host's own links and routes."""
    return run([base.IP_BIN, *args])


def ns_run(ns: str, *args: str) -> str:
    """Run a command inside a network namespace."""
    return run([base.IP_BIN, "netns", "exec", ns, *args])


def ns_ip(ns: str, *args: str) -> str:
    """`ip` inside a namespace."""
    return ns_run(ns, base.IP_BIN, *args)


def ovs(*args: str) -> str:
    """`ovs-vsctl`."""
    return run([base.OVSVSCTL_BIN, *args])


def ofport(iface: str) -> int | None:
    """``iface``'s OpenFlow port, or None while OVS has none for it."""
    out = ovs("--if-exists", "get", "interface", iface, "ofport").strip()
    try:
        number = int(out)
    except ValueError:
        return None
    return number if number >= 0 else None


def run(cmd: list[str]) -> str:
    LOG.debug("evpn: run %s", " ".join(cmd))
    return subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode()


def try_run(cmd: list[str]) -> str | None:
    """`run`, best-effort: None when the command failed or is missing."""
    try:
        return run(cmd)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def remove(path: str) -> bool:
    """Remove a file; False when it was already gone."""
    try:
        os.remove(path)
    except FileNotFoundError:
        return False
    return True


def appctl(*args: str) -> str:
    """`ovs-appctl`: flushing one conntrack zone here is one call, where the
    conntrack tool would read every entry on the host.
    """
    return run(["ovs-appctl", *args])


def ofctl(*args: str) -> str:
    """`ovs-ofctl` pinned to our OpenFlow version (it defaults to 1.0).

    The bridge itself keeps OVS's permissive protocol list on purpose, so a
    plain `ovs-ofctl dump-flows br-int` still works for an operator.
    """
    return run([base.OVSOFCTL_BIN, "-O", base.OPENFLOW_VERSION, *args])


def run_flow_bundle(lines: list[str]) -> None:
    """Apply ``flow delete``/``flow add`` lines to br-int as one atomic
    OpenFlow bundle: packets never see a half-updated table.
    """
    write_atomic(base.FLOW_BUNDLE_FILE, "\n".join(lines) + "\n")
    ofctl("bundle", base.BR_INT, base.FLOW_BUNDLE_FILE)


def write_if_changed(path: str, content: str) -> bool:
    """Write only when the content differs (the responder reloads every
    record when the directory's mtime moves); say whether it did.
    """
    try:
        with open(path) as fl:
            if fl.read() == content:
                return False
    except (OSError, ValueError):
        pass
    write_atomic(path, content)
    return True


def write_atomic(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fl:
        fl.write(content)
        # fsync before rename: after a power loss the rename can survive
        # without the data.

        fl.flush()
        os.fsync(fl.fileno())
    os.replace(tmp, path)
