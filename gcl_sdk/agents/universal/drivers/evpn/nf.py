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

"""The host's responder daemon (unit, service) and its records directory."""

import logging
import os
import sys

from oslo_config import cfg

from gcl_sdk.agents.universal import constants as ua_constants
from gcl_sdk.agents.universal import utils as ua_utils
from gcl_sdk.agents.universal.drivers import evpn_records
from gcl_sdk.agents.universal.drivers.evpn import base
from gcl_sdk.agents.universal.drivers.evpn import sh
from gcl_sdk.agents.universal.drivers.evpn import state

LOG = logging.getLogger(__name__)

# None outside an agent cycle; only a successful ensure may set this True.
_RESPONDER_READY: bool | None = None
_RESPONDER_UNIT_PENDING = False
_RESPONDER_MISSING: list | None = None


def nf_by_kind(nfs: list | None) -> dict:
    """Index a target resource's ``[{kind, config}]`` list by kind."""
    indexed = {}
    for descriptor in nfs or []:
        kind = descriptor.get("kind")
        if kind:
            indexed[kind] = descriptor.get("config") or {}
    return indexed


def serves_guests(kinds: dict) -> bool:
    """Whether any function this port asks for is the responder's."""
    return bool(kinds.keys() & set(evpn_records.NF_KINDS))


def nf_config_path(vni: int, kind: str) -> str:
    """Where a VNI's copy of one function's settings lives."""
    return evpn_records.nf_config_file(
        os.path.join(base.NF_RECORDS_DIR, str(vni)), kind
    )


def render_nf_unit() -> str:
    """Render the responder unit with the host credential paths.

    No tenant config on the command line (it is read per guest from the
    records), so networks never leak into each other and editing a function
    never restarts the daemon every guest depends on.
    """
    execstart = "%(py)s -m %(mod)s --records-dir %(rec)s" % {
        "py": sys.executable,
        "mod": "gcl_sdk.agents.universal.drivers.evpn_nf",
        "rec": base.NF_RECORDS_DIR,
    }
    try:
        key_path = cfg.CONF.universal_agent.private_key_path
    except (cfg.NoSuchOptError, cfg.NoSuchGroupError):
        key_path = ua_constants.PRIVATE_KEY_PATH
    identity = (
        (
            'Environment="EXORDOS_BOOT_PROXY_KEY_FILE=%s"\n'
            'Environment="EXORDOS_BOOT_PROXY_HOST=%s"\n'
        )
        % (key_path, ua_utils.node_uuid())
        if os.path.exists(key_path)
        else ""
    )
    return (
        base.GOBGP_CONF_MARKER + "\n"
        "[Unit]\n"
        "Description=exordos evpn per-host DHCP+DNS+proxy responder\n"
        "After=openvswitch-switch.service\n"
        "[Service]\n"
        "%s"
        "ExecStart=%s\n"
        "Restart=always\n"
        "RestartSec=2\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    ) % (identity, execstart)


def ensure_nf_responder() -> None:
    """Install and run the responder unit; per-VNI ports and intercepts
    belong to the VNI and the guest's port.
    """
    global _RESPONDER_READY, _RESPONDER_UNIT_PENDING, _RESPONDER_MISSING
    if _RESPONDER_READY:
        return
    _RESPONDER_MISSING = None
    os.makedirs(base.NF_RECORDS_DIR, exist_ok=True)
    if sh.write_if_changed(base.NF_UNIT_PATH, render_nf_unit()):
        _RESPONDER_UNIT_PENDING = True
    if _RESPONDER_UNIT_PENDING:
        sh.run([base.SYSTEMCTL_BIN, "daemon-reload"])
    sh.run([base.SYSTEMCTL_BIN, "enable", "--now", base.NF_SERVICE])
    if _RESPONDER_UNIT_PENDING:
        sh.run([base.SYSTEMCTL_BIN, "restart", base.NF_SERVICE])
        _RESPONDER_UNIT_PENDING = False
    if _RESPONDER_READY is not None:
        _RESPONDER_READY = True
        _RESPONDER_MISSING = []


def missing_nf_responder() -> list[str]:
    """What the responder lacks; each item is repaired by
    `ensure_nf_responder`, so none can loop.
    """
    global _RESPONDER_MISSING
    if _RESPONDER_READY is not None and _RESPONDER_MISSING is not None:
        return list(_RESPONDER_MISSING)
    missing = []
    if not os.path.exists(base.NF_UNIT_PATH):
        missing.append("nf responder unit")
    else:
        with open(base.NF_UNIT_PATH) as unit:
            if unit.read() != render_nf_unit():
                missing.append("nf responder credentials")
    if state.service_is_down(base.NF_SERVICE) or _RESPONDER_UNIT_PENDING:
        missing.append("nf responder")
    if _RESPONDER_READY is not None:
        _RESPONDER_MISSING = missing
    return list(missing)


def has_served_guests() -> bool:
    """Whether any guest record remains on the host, across all VNIs.

    `nf-<kind>.json` config files are not guests; counting them would keep
    the responder running after its last guest.
    """
    try:
        with os.scandir(base.NF_RECORDS_DIR) as vnis:
            for vni in vnis:
                try:
                    with os.scandir(vni.path) as records:
                        if any(
                            record.name.endswith(".json")
                            and not record.name.startswith(
                                evpn_records.NF_CONFIG_PREFIX
                            )
                            for record in records
                        ):
                            return True
                except OSError:
                    continue
    except OSError:
        return False
    return False


def teardown_nf_responder() -> None:
    """Stop the responder; ports and intercepts go with their owners."""
    global _RESPONDER_READY, _RESPONDER_MISSING
    _RESPONDER_MISSING = None
    if _RESPONDER_READY is not None:
        _RESPONDER_READY = False
    sh.try_run([base.SYSTEMCTL_BIN, "disable", "--now", base.NF_SERVICE])
    if sh.remove(base.NF_UNIT_PATH):
        sh.try_run([base.SYSTEMCTL_BIN, "daemon-reload"])
