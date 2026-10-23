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

"""The ovs_evpn capability of a hypervisor's universal agent.

Renders the control plane's `evpn_port`, `evpn_host`, `evpn_nf` and
`bgp_rr` resources onto a host, split by what it touches:

* `base`    — where an installation puts things and what it calls them;
* `sh`      — running a command, writing a file, one flow bundle;
* `flows`   — the OpenFlow half of a port on br-int;
* `hostns`  — the per-VNI namespace and everything inside it;
* `state`   — what the host actually holds, read once per iteration;
* `nf`      — the responder daemon and what it serves guests from;
* `fabric`  — gobgpd's configuration and the forwarder's.

Tests must rebind a value (a path, `sh.run`) where it lives, not here.
"""

import json
import logging
import os
import subprocess

from gcl_sdk.agents.universal import constants
from gcl_sdk.agents.universal.drivers import evpn_records
from gcl_sdk.agents.universal.drivers import exceptions as driver_exc
from gcl_sdk.agents.universal.drivers import meta
from gcl_sdk.agents.universal.drivers.evpn import base
from gcl_sdk.agents.universal.drivers.evpn import fabric
from gcl_sdk.agents.universal.drivers.evpn import flows
from gcl_sdk.agents.universal.drivers.evpn import hostns
from gcl_sdk.agents.universal.drivers.evpn import nf
from gcl_sdk.agents.universal.drivers.evpn import sh
from gcl_sdk.agents.universal.drivers.evpn import state
from gcl_sdk.infra import constants as ic
from gcl_sdk.paas.dm import evpn as evpn_models

LOG = logging.getLogger(__name__)


class EvpnPort(evpn_models.EvpnPort, meta.MetaDataPlaneModel):
    """Renders one evpn_connector client config + a DHCP record."""

    META_PATH = os.path.join(constants.WORK_DIR, "evpn_meta.json")

    @property
    def _client_conf_path(self) -> str:
        return os.path.join(base.CLIENT_CONF_DIR, "%s.json" % self.uuid)

    @property
    def _nf_record_path(self) -> str:
        return os.path.join(base.NF_RECORDS_DIR, str(self.vni), "%s.json" % self.uuid)

    def _resolve_guest_port(self) -> str:
        """Return the guest interface name on br-int, or retry.

        Looked up by ``iface-id`` (port uuid, set by libvirt) only: the MAC
        is the tenant's to set, so a port naming a neighbour's would take
        over (retag) the neighbour's tap. Not plugged yet is a retry, not an
        error.
        """
        try:
            out = sh.ovs(
                "--bare",
                "--columns=name",
                "find",
                "Interface",
                'external_ids:iface-id="%s"' % self.uuid,
                "ofport>0",
            ).strip()
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            LOG.error("evpn: ovs-vsctl failed: %s", e)
            self.status = ic.InstanceStatus.ERROR.value
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})
        if out:
            return out.splitlines()[0]
        LOG.info(
            "evpn: port %s (mac %s) is not plugged into OVS yet",
            self.uuid,
            self.mac,
        )
        raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})

    def _ensure_guest_patch(self, vlan: int) -> int:
        """Ensure this guest's br-int↔evpn patch pair; return its evpn ofport.

        One pair per guest (keyed by its local VLAN): evpn_connector rejects
        two clients on one ofport, as it cannot hairpin to the ingress port.
        """
        pe = "pe%d" % vlan  # patch on the evpn bridge (untagged: EVPN)
        pi = "pi%d" % vlan  # patch on br-int (this guest's access VLAN)
        sh.ovs("--may-exist", "add-br", base.BR_INT)
        sh.ovs(
            "--may-exist",
            "add-port",
            base.BR_INT,
            pi,
            "--",
            "set",
            "port",
            pi,
            "tag=%d" % vlan,
            "--",
            "set",
            "interface",
            pi,
            "type=patch",
            "options:peer=" + pe,
        )
        sh.ovs(
            "--may-exist",
            "add-port",
            base.EVPN_BRIDGE,
            pe,
            "--",
            "set",
            "interface",
            pe,
            "type=patch",
            "options:peer=" + pi,
        )
        ofport = sh.ofport(pe)
        if ofport is None:
            # ofport not assigned yet; retriable next loop.
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})
        return ofport

    def _render_client_conf(self, ofport: int) -> str:
        # What the fabric announces for this guest: its own /32.
        return hostns.client_conf(
            self.mac, ["%s/32" % self.ipv4], self.vni, ofport, self.imp_rt, self.exp_rt
        )

    def _apply(self) -> None:
        if not flows.is_mac(self.mac) or not self.ipv4:
            LOG.warning("evpn: port %s has no valid mac/ipv4 yet", self.uuid)
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})
        # One guest per local VLAN, so sibling patches never flap router_mac.
        guest_port = self._resolve_guest_port()
        vlan = hostns.local_vlan_for_port(self.uuid)
        patch_ofport = self._ensure_guest_patch(vlan)
        sh.write_atomic(self._client_conf_path, self._render_client_conf(patch_ofport))
        hostns.record_client(self.uuid, self.vni)
        kinds = nf.nf_by_kind(self.nfs)
        self._write_nf_record(kinds)
        guest_ofport = sh.ofport(guest_port)
        if guest_ofport is None:
            # Flows on in_port=-1 would match nothing.
            LOG.info("evpn: guest port %s has no ofport yet", guest_port)
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})
        if nf.serves_guests(kinds):
            # The port installs everything it needs; it never waits for
            # anything host-scope to happen first.
            nf.ensure_nf_responder()
            nf_ofport = hostns.ensure_vni_nf_port(self.vni)
            hostns.ensure_guest_neighbour(self.vni, self.ipv4, self.mac)
        else:
            nf_ofport = 0
        flows.ensure_port_nf_intercepts(
            kinds,
            guest_ofport,
            nf_ofport,
            self.mac,
            flows.port_cookie(self.uuid, "nf"),
            sources=[self.ipv4],
            port_security=bool(self.port_security),
            # The SG's zone, so a function's answer returning over the
            # fabric is already established.
            zone=vlan,
        )
        hostns.ensure_vni_egress(self.vni, self.dhcp, list(self.imp_rt), self.uuid)
        delivery_ofport = sh.ofport("pi%d" % vlan)
        if delivery_ofport is None:
            # An ingress entry scoped only by MAC outranks (91 > 90) the
            # sender's egress entry, letting a local neighbour skip its own
            # allow-list — and the reconcile would never notice (it checks
            # the cookie, not correctness). Retry instead.
            LOG.info(
                "evpn: guest patch pi%d has no ofport yet, deferring port %s",
                vlan,
                self.uuid,
            )
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})
        flows.ensure_security_group(
            guest_ofport,
            vlan,
            list(self.security_rules),
            flows.port_cookie(self.uuid, "sg"),
            self.mac,
            delivery_ofport=delivery_ofport,
            identity=int(self.identity or 0),
            sources=[self.ipv4],
            port_security=bool(self.port_security),
        )
        # Last: until its flows are in, the tap stays parked off every VLAN
        # (an apply that fails above leaves no unfiltered guest on one).
        sh.ovs("set", "port", guest_port, "tag=%d" % vlan)
        hostns.ensure_fip_nat(
            self.ipv4, list(self.fips), hostns.fip_tag(self.uuid), self.vni
        )
        self.status = ic.InstanceStatus.ACTIVE.value

    def _write_nf_record(self, kinds: dict) -> None:
        """Write (or remove) the record that says who this guest is.

        What functions answer *with* is the network's (`EvpnNf`), kept apart
        so editing a resolver touches no port. A port with no functions has
        no record; the VNI directory is how the daemon learns its namespaces.
        """
        if not nf.serves_guests(kinds):
            sh.remove(self._nf_record_path)
            return
        record = {
            "mac": self.mac,
            "ipv4": self.ipv4,
            "vni": self.vni,
            "dhcp_enabled": "dhcp" in kinds,
            "dns_enabled": "dns" in kinds,
            **(self.dhcp or {}),
        }
        if self.node:
            # A guest may ask the platform about itself and nothing else.
            record["node"] = str(self.node)
        sh.write_if_changed(
            self._nf_record_path, json.dumps(record, indent=1, sort_keys=True)
        )

    def dump_to_dp(self) -> None:
        self._apply()

    def update_on_dp(self) -> None:
        self._apply()

    def restore_from_dp(self) -> None:
        """Check what is *installed* against what this port asked for.

        Missing state raises: desired ports are repaired, and removed ports
        remain eligible for cleanup retries. Checks follow the requested features.
        """
        missing = self._missing_on_dp()
        if missing:
            LOG.warning(
                "evpn: port %s is missing %s on the host, recreating",
                self.uuid,
                ", ".join(missing),
            )
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})

    def _plugged_guest_port(self) -> str | None:
        """This guest's OVS interface, or ``None`` while unplugged (not drift)."""
        plugged = state.dp()["ifaces"]
        if plugged is None:
            return None
        return plugged.get(str(self.uuid).lower())

    def _missing_on_dp(self) -> list[str]:
        missing = []
        if not os.path.exists(self._client_conf_path):
            missing.append("client config")
        kinds = nf.nf_by_kind(self.nfs)
        if nf.serves_guests(kinds):
            if not os.path.exists(self._nf_record_path):
                missing.append("nf record")
            # Without the daemon the intercepts still steal DHCP/DNS into
            # silence — worse than no function at all.
            missing.extend(nf.missing_nf_responder())
            if not state.has_port(hostns.nf_port(self.vni)):
                missing.append("vni nf port")
            elif self._nf_port_is_a_trunk():
                # Untagged it is a trunk, carrying every tenant's VLAN into
                # this namespace — silently.
                missing.append("vni nf port vlan")
            if self._missing_neighbour():
                missing.append("guest neighbour")
            guarded = state.dp()["nf_guarded"]
            if guarded is not None and guarded.get(self.vni) is False:
                missing.append("vni nf guard")
        missing.extend(self._missing_egress())
        vlan = state.local_vlan(self.uuid)
        if vlan is None:
            missing.append("local vlan")
            return missing
        if not state.has_port("pi%d" % vlan):
            missing.append("guest patch")
        # A `pi` without its `pe` never reaches the fabric.
        evpn_ports = state.dp()["evpn_ports"]
        if evpn_ports is not None and "pe%d" % vlan not in evpn_ports:
            missing.append("evpn patch")
        # An OVS restart or libvirt re-plug resets the tag to the default
        # VLAN, bypassing the SG and exposing other tenants.
        guest_port = self._plugged_guest_port()
        tags = state.dp()["tags"]
        if guest_port and tags is not None and tags.get(guest_port) != vlan:
            missing.append("guest vlan tag")
        # Port security and the identity stamp live under this cookie too.
        if flows.security_group_installs(
            list(self.security_rules or ()),
            self.mac,
            identity=int(self.identity or 0),
            sources=[self.ipv4],
            port_security=bool(self.port_security),
        ) and not state.has_cookie(flows.port_cookie(self.uuid, "sg")):
            missing.append("security group flows")
        unprotected = state.dp()["unprotected_nf"]
        if unprotected is not None and any(
            int(cookie, 16) == int(flows.port_cookie(self.uuid, "nf"), 16)
            for cookie in unprotected
        ):
            missing.append("function caller guards")
        # One cookie covers intercepts, metadata punt and the return leg.
        if nf.serves_guests(kinds) and not state.has_cookie(
            flows.port_cookie(self.uuid, "nf")
        ):
            missing.append("function intercepts")
        publics = [fip["public"] for fip in self.fips or [] if fip.get("public")]
        if publics:
            if hostns.fip_tag(self.uuid) not in state.fip_tags(self.vni):
                missing.append("floating-ip nat")
            # The proxy-ARP entry that draws the traffic here; anything may
            # flush it. Only for addresses on a link (routed ones need none).
            proxied = state.proxied_fips()
            for public in publics:
                if public not in proxied and hostns.fip_link(public):
                    missing.append("floating-ip arp")
                    break
        return missing

    def _nf_port_is_a_trunk(self) -> bool:
        """Whether this VNI's NF port lost its isolating access VLAN."""
        tags = state.dp()["tags"]
        if tags is None:
            return False
        wanted = state.local_vlan(hostns.nf_vlan_key(self.vni))
        if wanted is None:
            return False
        return tags.get(hostns.nf_port(self.vni)) != wanted

    def _missing_neighbour(self) -> bool:
        """Whether this VNI's namespace still knows where the guest is.

        Only the metadata proxy (kernel stack) needs it; without the entry it
        accepts the guest's connection and never answers.
        """
        neighbours = state.dp()["neigh"]
        if neighbours is None or not self.ipv4:
            return False
        table = neighbours.get(self.vni)
        if table is None:
            return False
        return table.get(self.ipv4) != (self.mac or "").lower()

    def _missing_egress(self) -> list[str]:
        """The VNI's DVR egress, when this port's subnet asks for one.

        Gated exactly like `hostns.ensure_vni_egress`: a subnet with no
        gateway has no namespace, and demanding one would loop forever.
        """
        dhcp = self.dhcp or {}
        gateway = evpn_records.default_gateway(dhcp.get("routers"))
        if not dhcp.get("cidr") or not gateway or not self.imp_rt:
            return []
        missing = []
        if not os.path.exists(hostns.egress_port_path(self.vni, self.uuid)):
            missing.append("egress port declaration")
        namespaces = state.dp()["netns"]
        if namespaces is not None and hostns.vni_ns(self.vni) not in namespaces:
            missing.append("egress namespace")
        if not os.path.exists(hostns.egress_conf_path(self.vni)):
            missing.append("egress gateway config")
        evpn_ports = state.dp()["evpn_ports"]
        if evpn_ports is not None and hostns.egress_leg(self.vni) not in evpn_ports:
            missing.append("egress gateway leg")
        # The root FORWARD accept: libvirt re-inserts a blanket REJECT above
        # it whenever one of its networks is recreated.
        forwarded = state.dp()["forwarded"]
        if forwarded is not None and self.vni not in forwarded:
            missing.append("egress transfer forwarding")
        return missing

    def delete_from_dp(self) -> None:
        flows.forget_sg_rules(flows.port_cookie(self.uuid, "sg"))
        sh.remove(self._client_conf_path)
        hostns.record_client(self.uuid, None)
        sh.remove(self._nf_record_path)
        hostns.forget_egress_port(self.vni, self.uuid)
        for kind in ("sg", "nf"):
            try:
                sh.ofctl(
                    "del-flows",
                    base.BR_INT,
                    "cookie=%s/-1" % flows.port_cookie(self.uuid, kind),
                )
            except subprocess.CalledProcessError:
                if base.BR_INT in sh.ovs("list-br").splitlines():
                    raise
        # Patch pair and VLAN are per guest.
        hostns.gc_guest(self.uuid)
        if hostns.lookup_local_vlan(self.uuid) is not None:
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})
        hostns.delete_fip_rules(hostns.fip_tag(self.uuid), self.vni)
        if not nf.has_served_guests():
            nf.teardown_nf_responder()
        # The VNI namespace goes with the host's last guest on the VNI.
        vni = getattr(self, "vni", None)
        if vni and not hostns.vni_in_use(vni, self.uuid):
            hostns.gc_vni(vni)


class EvpnNf(evpn_models.EvpnNf, meta.MetaDataPlaneModel):
    """One function's settings for one VNI, beside that VNI's guest records."""

    @property
    def _path(self) -> str:
        if self.kind not in evpn_records.NF_KINDS:
            # Part of a path the agent writes and removes as root.
            LOG.warning("evpn: nf %s has an unknown kind %r", self.uuid, self.kind)
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})
        return nf.nf_config_path(self.vni, self.kind)

    def _warn_about_ports_nobody_claims(self) -> None:
        """Warn when the control plane names a port the bridge does not claim.

        Such a port is a silent black hole (the guest times out), not an error.
        """
        if self.kind != "proxy":
            return
        named = set()
        for port in (self.config or {}).get("ports") or {}:
            try:
                named.add(int(port))
            except (TypeError, ValueError):
                continue
        unclaimed = named - set(base.PLATFORM_PORTS)
        if unclaimed:
            LOG.warning(
                "evpn: the control plane serves %s on %s, which this data "
                "plane does not claim (it claims %s) — a guest asking there "
                "reaches nothing",
                self.kind,
                sorted(unclaimed),
                sorted(base.PLATFORM_PORTS),
            )

    def _apply(self) -> None:
        if not self.kind or not self.vni:
            LOG.warning("evpn: nf %s has no kind or vni yet", self.uuid)
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})
        self._warn_about_ports_nobody_claims()
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        # Only on change: any write reloads the VNI for all its guests.
        self._claim_config()
        self.status = ic.InstanceStatus.ACTIVE.value

    def _configs(self) -> dict:
        try:
            with open(self._path) as stream:
                return json.load(stream).get("_resource_configs", {})
        except FileNotFoundError:
            return {}

    def _write_configs(self, configs) -> None:
        if not configs:
            sh.remove(self._path)
            return
        config = dict(configs[sorted(configs)[-1]])
        config["_resource_configs"] = configs
        sh.write_if_changed(self._path, json.dumps(config, indent=1, sort_keys=True))

    def _claim_config(self) -> None:
        configs = self._configs()
        configs[str(self.uuid)] = dict(self.config or {})
        self._write_configs(configs)

    def dump_to_dp(self) -> None:
        self._apply()

    def update_on_dp(self) -> None:
        self._apply()

    def restore_from_dp(self) -> None:
        if not os.path.exists(self._path):
            LOG.warning(
                "evpn: nf %s (%s, vni %s) is missing on the host, recreating",
                self.uuid,
                self.kind,
                self.vni,
            )
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})
        # Migrate legacy files from every local slice before withdrawals.
        self._claim_config()

    def delete_from_dp(self) -> None:
        if self.kind not in evpn_records.NF_KINDS:
            return  # never written
        configs = self._configs()
        configs.pop(str(self.uuid), None)
        self._write_configs(configs)


class EvpnAgentCapabilityDriver(meta.MetaFileStorageAgentDriver):
    """evpn_node capability of a hypervisor's universal agent.

    Consumes the evpn_port/evpn_host target resources computed by the
    ovs_evpn CP driver and renders them for the on-host EVPN stack
    (evpn_connector + gobgpd).
    """

    META_PATH = os.path.join(constants.WORK_DIR, "evpn_meta.json")

    __model_map__ = {
        base.EVPN_PORT_KIND: EvpnPort,
        base.EVPN_HOST_KIND: fabric.EvpnHost,
        base.EVPN_NF_KIND: EvpnNf,
        base.BGP_RR_KIND: fabric.BgpRr,
    }

    def __init__(self, *args, **kwargs) -> None:
        os.makedirs(constants.WORK_DIR, exist_ok=True)
        super().__init__(*args, meta_file=self.META_PATH, **kwargs)

    def list(self, capability):
        if capability != base.EVPN_PORT_KIND:
            return super().list(capability)
        resources = []
        for port in self._load_from_meta(capability):
            valid = True
            try:
                port.restore_from_dp()
            except (
                driver_exc.InvalidDataPlaneObjectError,
                driver_exc.ResourceNotFound,
            ):
                valid = False
            resource = port.to_ua_resource(capability)
            if not valid:
                # Keep partial ports visible for deletion; desired ports are updated.
                resource.hash = ""
                resource.status = ic.InstanceStatus.ERROR.value
            resources.append(resource)
        return resources

    def start(self) -> None:
        # Read the host once; every port checks itself against this.
        state._DP_SNAPSHOT = state.snapshot_dataplane()
        hostns._GUARDED_VNIS.clear()
        hostns._EGRESS_DEMANDS = {}
        nf._RESPONDER_READY = False
        nf._RESPONDER_MISSING = None
        hostns._NF_PORTS = {}
        hostns._CLIENTS = {}
        super().start()

    def finalize(self) -> None:
        state._DP_SNAPSHOT = None
        hostns._GUARDED_VNIS.clear()
        hostns._EGRESS_DEMANDS = None
        nf._RESPONDER_READY = None
        nf._RESPONDER_MISSING = None
        hostns._NF_PORTS = None
        hostns._CLIENTS = None
        super().finalize()
