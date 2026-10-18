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

"""Integration: the whole SDN stack, one host, all real components.

Real EvpnPort/EvpnHost/BgpRr code + real gobgpd (RR with passive dynamic
neighbors + host with GR/LLGR) + the real external ``evpn_connector``
consuming the driver's client configs: BGP comes up, Type-5 routes are
announced, guests forward through the connector's L3 pipeline (ARP proxy,
per-guest patches), and the egress gateway leg is deliverable.

Scope note: this is a single host with a single connector, so every
prefix is local — cross-host EVPN forwarding and the fail-static
guarantee (which only bites on *remote*, reflected routes) need the
two-host stand and are validated there, not here.
"""

VNI = 16500
CIDR = "10.150.0.0/24"
GW = "10.150.0.1"
DHCP = {
    "cidr": CIDR,
    "routers": [{"to": "0.0.0.0/0", "via": GW}],
    "dns_servers": [GW],
    "mtu": 1400,
}


def test_full_datapath(evpn_fabric):
    fab = evpn_fabric

    g1, _ = fab.add_fabric_guest("fg1", "10.150.0.5", VNI, DHCP)
    g2, _ = fab.add_fabric_guest("fg2", "10.150.0.6", VNI, DHCP)

    # Control plane: the host session with the RR establishes (dynamic
    # passive peering on the RR side)...
    fab.wait_bgp_established()
    # ...and the connector compiles the clients into flows on the EVPN
    # bridge (2 guests + the egress gateway leg).
    fab.wait_flows(min_flows=6)

    # Both guests are announced to the RR as EVPN Type-5 /32s.
    fab.wait_rib(("10.150.0.5", "10.150.0.6"))

    # Data plane through the connector's L3 pipeline (ARP proxy answers
    # with the router MAC, per-guest patches carry the traffic).
    assert g1.ping("10.150.0.6", count=3), "g1 -> g2 through the fabric"
    assert g2.ping("10.150.0.5", count=3), "g2 -> g1 through the fabric"
    # The egress gateway (a namespace leg that is itself a connector
    # client) is reachable from the overlay.
    assert g1.ping(GW), "g1 -> egress gateway"
