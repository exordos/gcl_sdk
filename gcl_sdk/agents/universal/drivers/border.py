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
from __future__ import annotations

import logging
import os
import subprocess

from gcl_sdk.agents.universal import constants
from gcl_sdk.agents.universal.drivers import exceptions as driver_exc
from gcl_sdk.agents.universal.drivers import meta
from gcl_sdk.infra import constants as ic
from gcl_sdk.paas.dm import border as border_models

LOG = logging.getLogger(__name__)

BORDER_AGENT_TARGET_KIND = "border_agent"
BORDER_NODE_TARGET_KIND = "border_node"

# Dedicated nftables table fully owned (and atomically replaced) by the agent.
NFT_TABLE = "exordos_border"
NFT_CONFIG_FILE = os.path.join(constants.WORK_DIR, "exordos_border.nft")
NFT_BIN = "/usr/sbin/nft"
SYSCTL_BIN = "/usr/sbin/sysctl"


def _run(cmd: list[str]) -> str:
    LOG.debug("border: run %s", " ".join(cmd))
    return subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode()


class Border(border_models.Border, meta.MetaDataPlaneModel):
    """Renders NAT (SNAT/DNAT) + ip_forward from the border capability.

    The whole ``exordos_border`` nft table is regenerated and atomically
    replaced on every apply, which keeps reconciliation idempotent without
    per-rule diffing (mirrors how evpn_connector owns its OVS table).
    """

    META_PATH = os.path.join(constants.WORK_DIR, "border_meta.json")

    def _render_nft(self) -> str:
        snat_lines = []
        for rule in self.snat_rules or []:
            src = rule["source_cidr"]
            if rule.get("mode") == "snat" and rule.get("snat_to"):
                snat_lines.append(
                    "        ip saddr %s snat to %s" % (src, rule["snat_to"])
                )
            else:
                snat_lines.append("        ip saddr %s masquerade" % src)

        dnat_lines = []
        for fwd in self.forwards or []:
            proto = fwd.get("proto", "tcp")
            match = ""
            if fwd.get("public_ip"):
                match += "ip daddr %s " % fwd["public_ip"]
            match += "%s dport %s" % (proto, fwd["listen_port"])
            dnat_lines.append(
                "        %s dnat to %s:%s" % (match, fwd["to_host"], fwd["to_port"])
            )

        return (
            "add table ip %(t)s\n"
            "delete table ip %(t)s\n"
            "table ip %(t)s {\n"
            "    chain prerouting {\n"
            "        type nat hook prerouting priority dstnat; policy accept;\n"
            "%(dnat)s\n"
            "    }\n"
            "    chain postrouting {\n"
            "        type nat hook postrouting priority srcnat; policy accept;\n"
            "%(snat)s\n"
            "    }\n"
            "}\n"
        ) % {
            "t": NFT_TABLE,
            "dnat": "\n".join(dnat_lines),
            "snat": "\n".join(snat_lines),
        }

    def _apply(self) -> None:
        content = self._render_nft()
        os.makedirs(os.path.dirname(NFT_CONFIG_FILE), exist_ok=True)
        with open(NFT_CONFIG_FILE, "w") as fl:
            fl.write(content)
        try:
            _run([SYSCTL_BIN, "-w", "net.ipv4.ip_forward=1"])
            _run([NFT_BIN, "-f", NFT_CONFIG_FILE])
        except subprocess.CalledProcessError as e:
            LOG.error("border: failed to apply nftables: %s", e.output)
            self.status = ic.InstanceStatus.ERROR.value
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})
        self.status = ic.InstanceStatus.ACTIVE.value

    def dump_to_dp(self) -> None:
        self._apply()

    def update_on_dp(self) -> None:
        self._apply()

    def restore_from_dp(self) -> None:
        # The table is fully owned by us; if it disappeared (e.g. manual flush
        # or reboot without persistence) treat the resource as missing so the
        # agent recreates it.
        try:
            _run([NFT_BIN, "list", "table", "ip", NFT_TABLE])
        except subprocess.CalledProcessError:
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})
        except FileNotFoundError:
            # nft not installed -> nothing we can assert, keep meta as-is.
            pass

    def delete_from_dp(self) -> None:
        try:
            _run([NFT_BIN, "delete", "table", "ip", NFT_TABLE])
        except subprocess.CalledProcessError:
            # Table already absent.
            pass
        except FileNotFoundError:
            pass
        try:
            os.remove(NFT_CONFIG_FILE)
        except FileNotFoundError:
            pass


class BorderAgentCapabilityDriver(meta.MetaFileStorageAgentDriver):
    """Border capability served by the core node's agent (step 0)."""

    META_PATH = os.path.join(constants.WORK_DIR, "border_meta.json")

    __model_map__ = {BORDER_AGENT_TARGET_KIND: Border}

    def __init__(self, *args, **kwargs) -> None:
        os.makedirs(constants.WORK_DIR, exist_ok=True)
        super().__init__(*args, meta_file=self.META_PATH, **kwargs)


class BorderCapabilityDriver(BorderAgentCapabilityDriver):
    """Same capability advertised by hypervisor agents (distributed egress)."""

    __model_map__ = {BORDER_NODE_TARGET_KIND: Border}
