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

"""Integration: the punted DNS path of the real NF responder.

Guests send DNS to their gateway; the br-int punt flows steal port 53 to
the NF trunk and the responder answers the internal zone (scoped by the
guest's recorded VLAN) or forwards upstream. In production the gateway's
ARP is answered by evpn_connector's ARP proxy; the harness has no
connector, so guests get a static neighbour entry for the gateway —
exactly the boundary this tier does not cover.
"""

import shutil
import time
import uuid as sys_uuid

import pytest

from gcl_sdk.tests.integration.sdn.harness import METADATA_IP

GW = "10.42.0.254"
GW_MAC = "12:34:56:78:90:ab"  # any resolvable MAC; the punt matches on port

pytestmark = pytest.mark.skipif(not shutil.which("dig"), reason="dig not installed")


def _dns_guest(fabric, name, vlan, ip, mac):
    guest = fabric.add_guest(
        name, vlan=vlan, ip=ip + "/24", mac=mac, nfs=("dhcp", "dns")
    )
    guest.exec("ip", "neigh", "replace", GW, "lladdr", GW_MAC, "dev", guest.dev)
    return guest


def _dig(guest, qname, *args):
    return guest.exec(
        "dig",
        "+time=2",
        "+tries=1",
        "+short",
        "@" + GW,
        qname,
        *args,
        check=False,
        timeout=12,
    ).strip()


def test_internal_zone_a_and_ptr(fabric):
    mac = "52:54:00:aa:00:01"
    fabric.write_record(
        str(sys_uuid.uuid4()),
        mac,
        "10.42.0.9",
        vni=16007,
        cidr="10.42.0.0/24",
        name="web1",
    )
    fabric.start_nf()
    guest = _dns_guest(fabric, "d1", 7, "10.42.0.9", mac)

    assert _dig(guest, "web1.internal") == "10.42.0.9"
    assert _dig(guest, "-x", "10.42.0.9").rstrip(".") == "web1.internal"


def test_unknown_guest_gets_no_answer(fabric):
    # a guest without a CP record cannot be scoped -> the responder stays
    # silent (fail-closed), the query times out
    fabric.start_nf()
    guest = _dns_guest(fabric, "d2", 8, "10.42.0.66", "52:54:00:aa:00:66")
    out = _dig(guest, "web1.internal")
    # dig +short prints answers on bare lines; ";;" lines are diagnostics
    answers = [ln for ln in out.splitlines() if ln and not ln.startswith(";")]
    assert answers == [], out


def test_zone_is_scoped_per_vlan(fabric):
    # same name in two realms resolves to each realm's own address
    mac_a, mac_b = "52:54:00:aa:00:0a", "52:54:00:aa:00:0b"
    fabric.write_record(
        str(sys_uuid.uuid4()),
        mac_a,
        "10.42.0.10",
        vni=16007,
        cidr="10.42.0.0/24",
        name="app",
    )
    fabric.write_record(
        str(sys_uuid.uuid4()),
        mac_b,
        "10.42.0.20",
        vni=16009,
        cidr="10.42.0.0/24",
        name="app",
    )
    fabric.start_nf()
    ga = _dns_guest(fabric, "da", 7, "10.42.0.10", mac_a)
    gb = _dns_guest(fabric, "db", 9, "10.42.0.20", mac_b)

    assert _dig(ga, "app.internal") == "10.42.0.10"
    assert _dig(gb, "app.internal") == "10.42.0.20"


def test_port_without_the_dns_function_is_not_published(fabric):
    """dns=false on the port keeps the guest out of the internal zone."""
    mac = "52:54:00:ab:cd:ef"
    fabric.write_record(
        str(sys_uuid.uuid4()),
        mac,
        "10.42.0.30",
        vni=16009,
        cidr="10.42.0.0/24",
        name="hidden",
        dns_enabled=False,
    )
    fabric.start_nf()
    guest = _dns_guest(fabric, "doff", 9, "10.42.0.30", mac)

    assert _dig(guest, "hidden.internal") != "10.42.0.30"


def test_a_port_without_the_dns_function_keeps_its_own_resolver(fabric):
    """Switching the function off must leave the guest's port 53 alone.

    While the intercept was host-scope this could not hold: the flow took
    every guest's DNS to a responder that had no reason to answer this one,
    so "manage your own addressing" meant "have no resolver at all". Here a
    peer plays the guest's chosen server and must receive the query itself.
    """
    server_mac, client_mac = "52:54:00:ab:00:01", "52:54:00:ab:00:02"
    server = fabric.add_guest("dsrv", vlan=17, ip="10.42.0.40/24", mac=server_mac)
    # the client's port carries dhcp but *not* dns
    client = fabric.add_guest(
        "dcli", vlan=17, ip="10.42.0.41/24", mac=client_mac, nfs=("dhcp",)
    )
    fabric.start_nf()
    server.popen(
        "python3",
        "-c",
        "import socket\n"
        "s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        's.bind(("10.42.0.40", 53))\n'
        "while True:\n"
        "    data, peer = s.recvfrom(2048)\n"
        "    s.sendto(data[:2] + b'\\x81\\x83' + data[4:], peer)\n",
    )
    time.sleep(0.5)

    out = client.exec(
        "dig",
        "+time=2",
        "+tries=1",
        "@10.42.0.40",
        "anything.example",
        check=False,
        timeout=12,
    )
    # the guest's own server answered (its canned rcode), not the responder
    assert "status: NXDOMAIN" in out, out


def test_dns_survives_a_default_deny_security_group(fabric):
    """Same for the resolver: the `dns` function takes port 53 above the
    filter, and its answers come back by the intercept's return leg."""
    mac = "52:54:00:de:ad:02"
    fabric.write_record(
        str(sys_uuid.uuid4()),
        mac,
        "10.42.0.13",
        vni=16013,
        cidr="10.42.0.0/24",
        name="behind-sg",
    )
    fabric.start_nf()
    guest = _dns_guest(fabric, "dsg", 13, "10.42.0.13", mac)
    fabric.apply_security_group(guest, [{"direction": "ingress", "proto": "icmp"}])

    assert _dig(guest, "behind-sg.internal") == "10.42.0.13"


def test_the_platform_resolves_to_the_hypervisor(fabric):
    """A guest asks for the installation by name and must get an address it
    can reach — its own hypervisor's, where every platform port is served.

    Worth having on real packets rather than in a unit: the first version of
    this answered with the right address in the wrong shape, which made every
    such query die inside the response builder and be dropped as an
    unparseable frame. The unit test asserted the shape I had written, so it
    agreed with the bug.
    """
    mac = "52:54:00:aa:00:07"
    fabric.write_record(
        str(sys_uuid.uuid4()),
        mac,
        "10.42.0.7",
        vni=16017,
        cidr="10.42.0.0/24",
        name="p1",
    )
    fabric.apply_nf_config(
        16017,
        "dns",
        forwarders=[],
        zone_suffix="internal",
        platform_names=["core.local.genesis-core.tech"],
    )
    fabric.start_nf()
    guest = _dns_guest(fabric, "d7", 17, "10.42.0.7", mac)

    assert _dig(guest, "core.local.genesis-core.tech") == METADATA_IP
    # ... and a name it was not told about is not the platform's to answer:
    # with no forwarder configured here, the query simply goes unanswered
    # rather than being given the hypervisor's address.
    assert "timed out" in _dig(guest, "elsewhere.example.com")
