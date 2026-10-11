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

from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types as ra_types

from gcl_sdk.infra import constants as ic


class EvpnPort(models.ModelWithRequiredUUID):
    """Data-plane view of one overlay port (evpn_port capability).

    The ofport is resolved on the host, never part of the CP value.
    """

    status = properties.property(
        ra_types.Enum([s.value for s in ic.InstanceStatus]),
        default=ic.InstanceStatus.NEW.value,
    )
    mac = properties.property(ra_types.AllowNone(ra_types.String()), default=None)
    ipv4 = properties.property(ra_types.AllowNone(ra_types.String()), default=None)
    vni = properties.property(ra_types.Integer(), default=0)
    imp_rt = properties.property(ra_types.List(), default=list)
    exp_rt = properties.property(ra_types.List(), default=list)
    # {routers, dns_servers, mtu}
    dhcp = properties.property(ra_types.Dict(), default=dict)
    # Network functions serving this port: [{kind, config}]. Empty means a
    # bare overlay port.
    nfs = properties.property(ra_types.List(), default=list)
    # Stateful allowlist: [{direction, proto: tcp|udp|icmp|any, port?,
    # remote?, remote_identity?}]. Empty means no filtering.
    security_rules = properties.property(ra_types.List(), default=list)
    # Floating IPs, 1:1 NAT to this guest: [{public}].
    fips = properties.property(ra_types.List(), default=list)
    # A disabled target remains until the host acknowledges withdrawal.
    enabled = properties.property(ra_types.Boolean(), default=True)
    retired_fips = properties.property(
        ra_types.TypedList(ra_types.String()), default=list
    )
    # The port's group id, stamped on its traffic (skb mark locally,
    # VXLAN-GBP between hosts) so rules can name groups, not addresses.
    # Zero means unidentified.
    identity = properties.property(
        ra_types.Integer(min_value=0, max_value=0xFFFF), default=0
    )
    # The guest's node uuid: the host-local proxy uses it to confine the
    # guest to asking about itself.
    node = properties.property(ra_types.AllowNone(ra_types.UUID()), default=None)
    # Admit only frames with this port's MAC and `ipv4`. Enforced on br-int
    # because libvirt refuses an nwfilter on an openvswitch virtualport.
    port_security = properties.property(ra_types.Boolean(), default=True)


class EvpnHost(models.ModelWithRequiredUUID):
    """Data-plane view of the per-node EVPN stack (evpn_host capability)."""

    status = properties.property(
        ra_types.Enum([s.value for s in ic.InstanceStatus]),
        default=ic.InstanceStatus.NEW.value,
    )
    as_number = properties.property(ra_types.Integer(), default=0)
    rr_addresses = properties.property(ra_types.List(), default=list)
    # The installation's published (floating) addresses. The fabric guard
    # drops non-globally-routable egress; these may be private yet are meant
    # to be reached, so they are let through.
    published_addresses = properties.property(ra_types.List(), default=list)


class BgpRr(models.ModelWithRequiredUUID):
    """Data-plane view of the route reflector (bgp_rr capability).

    Peers passively via dynamic neighbors, so it needs no hypervisor list.
    """

    status = properties.property(
        ra_types.Enum([s.value for s in ic.InstanceStatus]),
        default=ic.InstanceStatus.NEW.value,
    )
    as_number = properties.property(ra_types.Integer(), default=0)
    peer_prefixes = properties.property(ra_types.List(), default=list)


class EvpnNf(models.ModelWithRequiredUUID):
    """Data-plane view of one network function on one host (evpn_nf).

    What the responder answers *with*, kept apart from the ports so an edit
    rewrites one resource per host instead of every port.
    """

    status = properties.property(
        ra_types.Enum([s.value for s in ic.InstanceStatus]),
        default=ic.InstanceStatus.NEW.value,
    )
    # dhcp | dns | proxy — the kinds a host serves guests with.
    kind = properties.property(ra_types.String(), default="")
    # The VNI it serves (a namespace on the host).
    vni = properties.property(ra_types.Integer(), default=0)
    # Compiled settings (forwarders, netboot filename, proxy allowlist, ...).
    config = properties.property(ra_types.Dict(), default=dict)
