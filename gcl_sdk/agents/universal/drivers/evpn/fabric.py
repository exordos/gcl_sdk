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

"""The host's BGP: gobgpd's configuration and the forwarder's.

Each role (hypervisor, route reflector) renders a fragment; gobgpd's file
is assembled from all present, so a node holding both does not flap.
"""

import json
import logging
import os
import re
import subprocess

from gcl_sdk.agents.universal.drivers import exceptions as driver_exc
from gcl_sdk.agents.universal.drivers import meta
from gcl_sdk.agents.universal.drivers.evpn import base
from gcl_sdk.agents.universal.drivers.evpn import flows
from gcl_sdk.agents.universal.drivers.evpn import hostns
from gcl_sdk.agents.universal.drivers.evpn import nf
from gcl_sdk.agents.universal.drivers.evpn import sh
from gcl_sdk.agents.universal.drivers.evpn import state
from gcl_sdk.infra import constants as ic
from gcl_sdk.paas.dm import evpn as evpn_models

LOG = logging.getLogger(__name__)


_ROUTER_ID_RE = re.compile(r'router-id = "([^"]+)"')


def gobgp_parts() -> list[dict]:
    """Every role's fragment, sorted by kind.

    ``bgp_rr`` sorts first, so on a dual-role node the reflector's global
    section is the daemon's one BGP identity, chosen the same on every host.
    """
    try:
        names = sorted(os.listdir(base.GOBGP_PARTS_DIR))
    except OSError:
        return []
    parts = []
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(base.GOBGP_PARTS_DIR, name)) as fl:
                part = json.load(fl)
        except (OSError, ValueError):
            LOG.warning("evpn: ignoring unreadable gobgp fragment %s", name)
            continue
        if isinstance(part, dict) and part.get("body") is not None:
            parts.append(part)
    return parts


def assemble_gobgp_conf() -> str | None:
    """The daemon's whole configuration, or None when no role claims it."""
    parts = gobgp_parts()
    if not parts:
        return None
    return "\n".join(
        [base.GOBGP_CONF_MARKER + "\n", parts[0]["global"]] + [p["body"] for p in parts]
    )


class _GobgpConfModel(meta.MetaDataPlaneModel):
    """Shared apply/restore/delete machinery for gobgpd-rendering kinds."""

    PART_NAME = ""

    def _part_path(self) -> str:
        return os.path.join(base.GOBGP_PARTS_DIR, "%s.json" % self.PART_NAME)

    def _source_ip(self) -> str:
        """The address this host's fabric speaks from — and keeps speaking from.

        Resolved toward the first RR (the underlay may not be on the default
        route). Sticky: a change restarts gobgpd and the forwarder, which
        rewrites the whole flow table, so it moves only when the host has
        actually lost the address.
        """
        target = base.DEFAULT_ROUTE_PROBE_IP
        rr_addresses = getattr(self, "rr_addresses", None)
        if rr_addresses:
            target = str(rr_addresses[0])
        out = sh.ip("-json", "route", "get", target)
        resolved = json.loads(out)[0]["prefsrc"]
        current = self._rendered_source_ip()
        if not current or current == resolved:
            return resolved
        held = hostns.host_addresses()
        if held is not None and current not in held:
            LOG.info(
                "evpn: the fabric's source address %s is gone, moving to %s",
                current,
                resolved,
            )
            return resolved
        LOG.debug(
            "evpn: keeping the fabric's source address %s (the route to %s now "
            "prefers %s)",
            current,
            target,
            resolved,
        )
        return current

    def _rendered_source_ip(self) -> str | None:
        """The address this role's fragment was last written with, if any."""
        try:
            with open(self._part_path()) as fl:
                rendered = json.load(fl).get("global") or ""
        except (OSError, ValueError, AttributeError):
            return None
        match = _ROUTER_ID_RE.search(rendered)
        return match.group(1) if match else None

    def _render_global(self, source_ip: str) -> str:
        """The BGP identity of the daemon — the same text for either role."""
        return (
            "[global.config]\n"
            "  as = %(asn)d\n"
            '  router-id = "%(ip)s"\n'
            '  local-address-list = ["%(ip)s"]\n'
            % {"asn": self.as_number, "ip": source_ip}
        )

    def _render_body(self, source_ip: str) -> str:
        """This role's own sections (neighbors, peer groups, ...)."""
        raise NotImplementedError

    def _write_part(self, source_ip: str) -> None:
        sh.write_if_changed(
            self._part_path(),
            json.dumps(
                {
                    "global": self._render_global(source_ip),
                    "body": self._render_body(source_ip),
                },
                indent=1,
                sort_keys=True,
            ),
        )

    def _apply(self) -> None:
        try:
            # Resolved once so gobgpd and evpn_connector cannot disagree.
            source_ip = self._source_ip()
            self._write_part(source_ip)
        except (subprocess.CalledProcessError, FileNotFoundError, KeyError) as e:
            LOG.error("evpn: cannot determine host source ip: %s", e)
            self.status = ic.InstanceStatus.ERROR.value
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})

        try:
            # Restore reports a dead daemon, so apply must fix it.
            state.ensure_service(
                base.GOBGP_SERVICE, base.GOBGP_CONF_FILE, assemble_gobgp_conf()
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            LOG.error("evpn: failed to (re)start %s: %s", base.GOBGP_SERVICE, e)
            self.status = ic.InstanceStatus.ERROR.value
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})
        self.status = ic.InstanceStatus.ACTIVE.value
        self._apply_extra(source_ip)

    def _apply_extra(self, source_ip: str) -> None:
        """What this kind installs beyond gobgpd (nothing for a reflector)."""

    def dump_to_dp(self) -> None:
        self._apply()

    def update_on_dp(self) -> None:
        self._apply()

    def restore_from_dp(self) -> None:
        """Check the fabric against what this resource asked for.

        The fragment must be in the marked config gobgpd reads (catches the
        other role taking the file over), and the daemon must be running.
        """
        try:
            with open(self._part_path()) as fl:
                body = json.load(fl)["body"]
        except (OSError, ValueError, KeyError):
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})
        try:
            with open(base.GOBGP_CONF_FILE) as fl:
                conf = fl.read()
        except FileNotFoundError:
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})
        if base.GOBGP_CONF_MARKER not in conf or body not in conf:
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})
        if state.service_is_down(base.GOBGP_SERVICE):
            LOG.warning(
                "evpn: %s is not running, recreating %s", base.GOBGP_SERVICE, self.uuid
            )
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})
        self._restore_extra()

    def _restore_extra(self) -> None:
        """What this kind installs beyond gobgpd (nothing for a reflector)."""

    def delete_from_dp(self) -> None:
        """Drop this role's fragment; gobgpd stops only when none is left."""
        sh.remove(self._part_path())
        content = assemble_gobgp_conf()
        if content is None:
            sh.remove(base.GOBGP_CONF_FILE)
            sh.try_run([base.SYSTEMCTL_BIN, "stop", base.GOBGP_SERVICE])
            return
        sh.write_atomic(base.GOBGP_CONF_FILE, content)
        sh.try_run([base.SYSTEMCTL_BIN, "restart", base.GOBGP_SERVICE])


class EvpnHost(evpn_models.EvpnHost, _GobgpConfModel):
    """Renders gobgpd's config for this hypervisor: iBGP to the RRs, GR+LLGR."""

    PART_NAME = base.EVPN_HOST_KIND

    def _render_neighbor(self, address: str) -> str:
        return (
            "[[neighbors]]\n"
            "  [neighbors.config]\n"
            '    neighbor-address = "%(addr)s"\n'
            "    peer-as = %(asn)d\n"
            "  [neighbors.graceful-restart.config]\n"
            "    enabled = true\n"
            "    restart-time = 120\n"
            "  [[neighbors.afi-safis]]\n"
            "    [neighbors.afi-safis.config]\n"
            '      afi-safi-name = "l2vpn-evpn"\n'
            "    [neighbors.afi-safis.mp-graceful-restart.config]\n"
            "      enabled = true\n"
            "    [neighbors.afi-safis.long-lived-graceful-restart.config]\n"
            "      enabled = true\n"
            "      restart-time = 86400\n"
        ) % {"addr": address, "asn": self.as_number}

    def _peer_addresses(self, source_ip: str) -> list:
        """`rr_addresses` minus non-addresses and this host's own."""
        peers = []
        for addr in self.rr_addresses:
            if not flows.is_cidr(addr) or str(addr) == source_ip:
                continue
            peers.append(addr)
        return peers

    def _render_body(self, source_ip: str) -> str:
        # Only literal addresses: a quote or newline would inject TOML.
        if any(flows.is_cidr(a) and str(a) == source_ip for a in self.rr_addresses):
            # A session to itself never comes up; the local RIB suffices.
            LOG.info(
                "evpn: skipping the route reflector session to self (%s)", source_ip
            )
        return "\n".join(
            self._render_neighbor(addr) for addr in self._peer_addresses(source_ip)
        )

    @staticmethod
    def _router_mac(source_ip: str) -> str:
        # Type-5 router MAC: locally administered, derived from the underlay IP.
        octets = [int(o) for o in source_ip.split(".")]
        return "02:00:%02x:%02x:%02x:%02x" % tuple(octets)

    def _render_connector_conf(self, source_ip: str) -> str:
        return (
            base.GOBGP_CONF_MARKER + "\n"
            "[DEFAULT]\n"
            "verbose = True\n\n"
            "[agent]\n"
            "sync_period = 10\n\n"
            "[victoria_metrics]\n"
            "enabled = False\n\n"
            "[sentry]\n"
            "enabled = False\n\n"
            "[gobgp]\n"
            'gobgp_channel = "localhost:50051"\n'
            "grpc_timeout_sec = 100\n"
            'source_ip = "%(ip)s"\n'
            "as_number = %(asn)d\n"
            'router_mac_type5 = "%(mac)s"\n'
            # Keep the flows while the RIB cannot be trusted to say a route
            # is gone. Off by default in the connector.
            "fail_static = True\n"
            # The default (all peers) is wrong for reflectors: losing one of
            # two would put every host into fail-static while the survivor
            # carries the whole RIB. One is enough when there are several.
            "fail_static_min_peers = %(min_peers)d\n\n"
            "[ovs]\n"
            'switch_name = "%(sw)s"\n'
            "vxlan_udp_port = %(vxlan)d\n"
            # Carries the identity group in VXLAN-GBP. Fabric-wide: OVS will
            # not mix GBP and non-GBP tunnels on one UDP port.
            "gbp = True\n"
            "enable_sudo = False\n"
            'ovs_vsctl_bin_path = "/usr/bin/ovs-vsctl"\n'
            'ovs_ofctl_bin_path = "/usr/bin/ovs-ofctl"\n'
            "tmp_flow_file_path = %(run)s/evpn_tmp_flow_file\n\n"
            "[anycast]\n"
            "anycast_status_file = %(run)s/anycast_status_file\n\n"
            "[daemon]\n"
            "configs_dir = %(confdir)s/\n"
        ) % {
            "ip": source_ip,
            "asn": self.as_number,
            "mac": self._router_mac(source_ip),
            "sw": base.EVPN_BRIDGE,
            "vxlan": base.VXLAN_UDP_PORT,
            "confdir": base.CLIENT_CONF_DIR,
            "run": base.CONNECTOR_RUN_DIR,
            "min_peers": 1 if len(self._peer_addresses(source_ip)) > 1 else 0,
        }

    @staticmethod
    def _ensure_run_dir() -> None:
        """The forwarder's run directory, root's alone (it is a tmpfs:
        a reboot takes it away)."""
        os.makedirs(base.CONNECTOR_RUN_DIR, mode=0o700, exist_ok=True)
        os.chmod(base.CONNECTOR_RUN_DIR, 0o700)

    def _ensure_connector(self, source_ip: str) -> None:
        self._ensure_run_dir()
        state.ensure_service(
            base.CONNECTOR_SERVICE,
            base.CONNECTOR_CONF_FILE,
            self._render_connector_conf(source_ip),
            enable=True,
        )

    def _apply_extra(self, source_ip: str) -> None:
        # The NF responder is not here: the first/last port using it
        # starts/stops it.
        try:
            self._ensure_connector(source_ip)
        except (subprocess.CalledProcessError, FileNotFoundError, OSError) as e:
            LOG.error("evpn: failed to ensure the host fabric: %s", e)
            self.status = ic.InstanceStatus.ERROR.value
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})
        self._ensure_published()

    def _ensure_published(self) -> None:
        """Open the installation's published doors on every VNI here.

        Here rather than per port: the set changes installation-wide, and
        every VNI on the host must learn it, not only the next touched one.
        """
        if hostns.set_published_addresses(self.published_addresses):
            LOG.info(
                "evpn: the installation publishes %d address(es)",
                len(self.published_addresses or []),
            )
        for vni in hostns.vnis_on_host():
            hostns.ensure_published_reachable(vni)

    def _restore_extra(self) -> None:
        if not os.path.isdir(base.CONNECTOR_RUN_DIR):
            LOG.warning(
                "evpn: host %s has no %s, recreating",
                self.uuid,
                base.CONNECTOR_RUN_DIR,
            )
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})
        if not os.path.exists(base.CONNECTOR_CONF_FILE):
            LOG.warning(
                "evpn: host %s has no connector config on the host, recreating",
                self.uuid,
            )
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})
        if state.service_is_down(base.CONNECTOR_SERVICE):
            LOG.warning(
                "evpn: %s is not running, recreating host %s",
                base.CONNECTOR_SERVICE,
                self.uuid,
            )
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})
        # Anything on the host may flush a chain; nothing else would notice.
        if hostns.published_addresses() != sorted(
            {str(a) for a in self.published_addresses or []}
        ):
            LOG.warning("evpn: host %s has a stale published-address set", self.uuid)
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})
        for vni in hostns.vnis_on_host():
            if not hostns.published_is_installed(vni):
                LOG.warning("evpn: vni %d lost its published-address grants", vni)
                raise driver_exc.InvalidDataPlaneObjectError(
                    obj={"uuid": str(self.uuid)}
                )

    def delete_from_dp(self) -> None:
        super().delete_from_dp()
        sh.try_run([base.SYSTEMCTL_BIN, "disable", "--now", base.CONNECTOR_SERVICE])
        sh.remove(base.CONNECTOR_CONF_FILE)
        nf.teardown_nf_responder()


class BgpRr(evpn_models.BgpRr, _GobgpConfModel):
    """Renders the route reflector's gobgpd configuration.

    Passive dynamic-neighbors over the underlay prefixes, so adding a
    hypervisor touches nothing on the RR.
    """

    PART_NAME = base.BGP_RR_KIND
    PEER_GROUP = "evpn-clients"

    def _render_body(self, source_ip: str) -> str:
        parts = [
            "[[peer-groups]]\n"
            "  [peer-groups.config]\n"
            '    peer-group-name = "%(pg)s"\n'
            "    peer-as = %(asn)d\n"
            "  [peer-groups.route-reflector.config]\n"
            "    route-reflector-client = true\n"
            '    route-reflector-cluster-id = "%(ip)s"\n'
            "  [peer-groups.graceful-restart.config]\n"
            "    enabled = true\n"
            "    restart-time = 120\n"
            "  [[peer-groups.afi-safis]]\n"
            "    [peer-groups.afi-safis.config]\n"
            '      afi-safi-name = "l2vpn-evpn"\n'
            "    [peer-groups.afi-safis.mp-graceful-restart.config]\n"
            "      enabled = true\n"
            "    [peer-groups.afi-safis.long-lived-graceful-restart.config]\n"
            "      enabled = true\n"
            "      restart-time = 86400\n"
            % {"pg": self.PEER_GROUP, "asn": self.as_number, "ip": source_ip},
        ]
        parts.extend(
            "[[dynamic-neighbors]]\n"
            "  [dynamic-neighbors.config]\n"
            '    prefix = "%s"\n'
            '    peer-group = "%s"\n' % (prefix, self.PEER_GROUP)
            for prefix in self.peer_prefixes
            if flows.is_cidr(prefix)
        )
        return "\n".join(parts)
