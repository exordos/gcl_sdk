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
import shlex
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
NFT_BIN = "nft"
SYSCTL_BIN = "sysctl"
IPTABLES_BIN = "iptables"
# Marks border-owned rules in the shared filter FORWARD chain so they can be
# found and removed without touching anyone else's rules.
FORWARD_COMMENT = "exordos_border"


def _run(cmd: list[str]) -> str:
    LOG.debug("border: run %s", " ".join(cmd))
    return subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode()


class Border(border_models.Border, meta.MetaDataPlaneModel):
    """Renders NAT (SNAT/DNAT) + ip_forward from the border capability.

    The whole ``exordos_border`` nft table is regenerated and atomically
    replaced on every apply, which keeps reconciliation idempotent without
    per-rule diffing (mirrors how evpn_connector owns its OVS table).

    DNAT lands the forwarded packet on ``to_host:to_port``, but another
    firewall on the box (typically a libvirt NAT network) installs a
    ``FORWARD`` reject for inbound traffic to its bridge. nftables verdicts
    are not terminal across tables, so an accept in our own table can't undo
    that reject -- the accept has to sit in the *same* chain, ahead of it.
    We therefore also insert comment-tagged ACCEPT rules at the top of the
    shared filter ``FORWARD`` chain via ``iptables`` (the historical
    ``iptables -I FORWARD 1 ... ACCEPT`` hook), tagged so we can remove only
    our own rules.
    """

    META_PATH = os.path.join(constants.WORK_DIR, "border_meta.json")

    def _render_nft(self) -> str:
        snat_lines = []
        for rule in self.snat_rules or []:
            src = rule.get("source_cidr")
            if not src:
                continue
            if rule.get("mode") == "snat" and rule.get("snat_to"):
                snat_lines.append(
                    "        ip saddr %s snat to %s" % (src, rule["snat_to"])
                )
            else:
                snat_lines.append("        ip saddr %s masquerade" % src)

        dnat_lines = []
        for fwd in self.forwards or []:
            proto = fwd.get("proto", "tcp")
            listen_port = fwd.get("listen_port")
            to_host = fwd.get("to_host")
            to_port = fwd.get("to_port")
            if not listen_port or not to_host or not to_port:
                continue
            match = ""
            if fwd.get("public_ip"):
                match += "ip daddr %s " % fwd["public_ip"]
            match += "%s dport %s" % (proto, listen_port)
            dnat_lines.append("        %s dnat to %s:%s" % (match, to_host, to_port))
            # Out-of-path DNAT (the target does not route its replies back
            # through this border, e.g. a dedicated VM gateway forwarding
            # to a host on the same L2): full-NAT the flow so replies
            # return here instead of racing to the client directly.
            if fwd.get("full_nat"):
                snat_lines.append(
                    "        ip daddr %s %s dport %s masquerade"
                    % (to_host, proto, to_port)
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

    def _forward_accept_specs(self) -> list[list[str]]:
        """iptables rule bodies accepting the inbound forwarded connections.

        One ACCEPT per forward, matched on the post-DNAT destination
        (``to_host:to_port``) and tagged with our owner comment.
        """
        specs = []
        for fwd in self.forwards or []:
            proto = fwd.get("proto", "tcp")
            to_host = fwd.get("to_host")
            to_port = fwd.get("to_port")
            if not to_host or not to_port:
                continue
            specs.append(
                [
                    "-p",
                    proto,
                    "-d",
                    str(to_host),
                    "--dport",
                    str(to_port),
                    "-m",
                    "comment",
                    "--comment",
                    FORWARD_COMMENT,
                    "-j",
                    "ACCEPT",
                ]
            )
        return specs

    def _clear_forward_accepts(self) -> None:
        """Remove every border-owned FORWARD accept (found by our comment)."""
        try:
            listing = _run([IPTABLES_BIN, "-S", "FORWARD"])
        except (subprocess.CalledProcessError, FileNotFoundError):
            return
        for line in listing.splitlines():
            if FORWARD_COMMENT not in line:
                continue
            spec = shlex.split(line)
            if not spec or spec[0] != "-A":
                continue
            spec[0] = "-D"
            try:
                _run([IPTABLES_BIN, *spec])
            except subprocess.CalledProcessError:
                pass

    def _apply_forward_accepts(self) -> None:
        """Re-sync our FORWARD accepts: drop the old ones, insert the current."""
        try:
            self._clear_forward_accepts()
            for spec in self._forward_accept_specs():
                _run([IPTABLES_BIN, "-I", "FORWARD", "1", *spec])
        except FileNotFoundError:
            # No iptables on the box -> presumably no competing FORWARD reject
            # either; the nft DNAT is enough.
            LOG.warning("border: iptables not found, skipping FORWARD accepts")
        except subprocess.CalledProcessError as e:
            LOG.error("border: failed to add FORWARD accepts: %s", e.output)
            self.status = ic.InstanceStatus.ERROR.value
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})

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
        self._apply_forward_accepts()
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
        self._clear_forward_accepts()
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
