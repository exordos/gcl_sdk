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

"""Paths, names and binaries the EVPN driver is parameterised by.

Always read through this module (``base.BR_INT``, never a copy taken at
import time): the test tiers repoint them at a sandbox.
"""

import logging
import os

from gcl_sdk.agents.universal import constants
from gcl_sdk.agents.universal.drivers import evpn_proxy

LOG = logging.getLogger(__name__)

EVPN_PORT_KIND = "evpn_port"
EVPN_HOST_KIND = "evpn_host"
EVPN_NF_KIND = "evpn_nf"
BGP_RR_KIND = "bgp_rr"

# `ip route get` target for the source IP toward the default route when this
# host is the RR itself. Never actually reached.
DEFAULT_ROUTE_PROBE_IP = "1.1.1.1"

# evpn_connector's configs_dir: one client JSON per port.
CLIENT_CONF_DIR = "/opt/evpn/vm_conf"

NF_RECORDS_DIR = os.path.join(constants.WORK_DIR, "evpn_nf")

EGRESS_RECORDS_DIR = os.path.join(constants.WORK_DIR, "evpn_egress")

# evpn_connector owns the EVPN bridge, the agent owns br-int; one patch pair
# per guest joins them, and the connector's config points at the patch.
EVPN_BRIDGE = "evpn"

BR_INT = "br-int"

# br-int is one L2 domain, so VNIs (possibly with overlapping CIDRs) are
# separated by a per-host local access VLAN; this is the VNI -> VLAN map.
VLAN_MAP_PATH = os.path.join(constants.WORK_DIR, "evpn_vlan_map.json")

# Scratch file for flow bundles: overwritten each time, never read back.
FLOW_BUNDLE_FILE = os.path.join(constants.WORK_DIR, "evpn_flow_bundle.tmp")

# Published addresses belong to the host but the rules are per VNI, so they
# are kept on disk and survive an agent restart.
PUBLISHED_FILE = os.path.join(constants.WORK_DIR, "evpn_published.json")

# Each port's last installed allow-list: a revocation must flush the sessions
# the withdrawn rule held open, which needs knowing which rules left.
SG_RULES_PATH = os.path.join(constants.WORK_DIR, "evpn_sg_rules.json")

GOBGP_CONF_FILE = "/etc/gobgp.conf"

# A node can hold both fabric roles; each writes a fragment here and the one
# config gobgpd reads is assembled from all of them.
GOBGP_PARTS_DIR = "/etc/gobgp.d"

GOBGP_CONF_MARKER = "# managed by the exordos evpn agent"

GOBGP_SERVICE = "gobgpd"

# The connector ships in the image; the agent renders only its config.
CONNECTOR_CONF_FILE = "/opt/evpn/evpn_connector.cfg"

CONNECTOR_SERVICE = "evpn-connector"

# The forwarder's own files (the last synced flows it replays under
# fail-static, anycast statuses): root's alone, never a shared /tmp.
CONNECTOR_RUN_DIR = "/run/evpn"

VXLAN_UDP_PORT = 4789

# The NF responder: one process per hypervisor, one thread per VNI.
NF_SERVICE = "exordos-evpn-nf"

NF_UNIT_PATH = "/etc/systemd/system/%s.service" % NF_SERVICE

# Shared with the proxy, not restated: a port the bridge claims that the
# proxy doesn't listen on is a silent black hole.
METADATA_IP = evpn_proxy.METADATA_IP

METADATA_MAC = "02:00:00:00:00:03"  # fixed => the intercept action is constant

BOOT_CHAIN_PORT = evpn_proxy.BOOT_CHAIN_PORT

PLATFORM_PORTS = evpn_proxy.PLATFORM_PORTS

# High band so gateway legs never collide with guests' auto-assigned patch
# ofports (evpn_connector rejects duplicates).
EGRESS_OFPORT_BASE = 30000

# First two octets of the per-VNI transfer /30 range; same on every host.
EGRESS_TRANSFER_BASE = (10, 199)

OVSVSCTL_BIN = "ovs-vsctl"

OVSOFCTL_BIN = "ovs-ofctl"

# Matches evpn_connector's bridge, and has meters and group tables.
OPENFLOW_VERSION = "OpenFlow13"

SYSTEMCTL_BIN = "systemctl"

IP_BIN = "ip"

IPTABLES_BIN = "iptables"

IPTABLES_SAVE_BIN = "iptables-save"
