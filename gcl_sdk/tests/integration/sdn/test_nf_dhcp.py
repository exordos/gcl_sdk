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

"""Integration: the real per-host NF responder answers DHCP over br-int."""

import shutil
import uuid as sys_uuid

import pytest


@pytest.mark.skipif(not shutil.which("dhclient"), reason="dhclient not installed")
def test_static_dhcp_lease_from_record(fabric):
    port = str(sys_uuid.uuid4())
    mac = "52:54:00:11:22:33"
    fabric.write_record(
        port,
        mac,
        "10.42.0.7",
        vni=16005,
        cidr="10.42.0.0/24",
        mtu=1450,
        routers=[{"to": "0.0.0.0/0", "via": "10.42.0.254"}],
    )
    fabric.start_nf()
    guest = fabric.add_guest("g1", vlan=5, mac=mac, nfs=("dhcp", "dns"))

    out = guest.dhclient()
    assert "bound to 10.42.0.7" in out, out

    addr = guest.exec("ip", "-4", "addr", "show", guest.dev)
    assert "inet 10.42.0.7/24" in addr, addr
    routes = guest.exec("ip", "route")
    assert "default via 10.42.0.254" in routes, routes


@pytest.mark.skipif(not shutil.which("dhclient"), reason="dhclient not installed")
def test_no_record_no_lease(fabric):
    # a guest with no CP record must NOT get an address (static-only responder)
    fabric.start_nf()
    guest = fabric.add_guest("g2", vlan=6, mac="52:54:00:99:99:99", nfs=("dhcp", "dns"))
    out = guest.dhclient(wait=5)
    assert "bound to" not in out
    addr = guest.exec("ip", "-4", "addr", "show", guest.dev)
    assert "inet " not in addr, addr


@pytest.mark.skipif(not shutil.which("dhclient"), reason="dhclient not installed")
def test_port_without_the_dhcp_function_gets_no_lease(fabric):
    """The `simple` port kind's dhcp=false reaches the host as a record
    without the function, and the responder must stay silent for it — the
    guest manages its own addressing."""
    port = str(sys_uuid.uuid4())
    mac = "52:54:00:44:55:66"
    fabric.write_record(
        port,
        mac,
        "10.42.0.8",
        vni=16007,
        cidr="10.42.0.0/24",
        dhcp_enabled=False,
        dns_enabled=True,
    )
    fabric.start_nf()
    guest = fabric.add_guest("g3", vlan=7, mac=mac, nfs=("dhcp", "dns"))

    out = guest.dhclient(wait=5)
    assert "bound to" not in out, out
    addr = guest.exec("ip", "-4", "addr", "show", guest.dev)
    assert "inet " not in addr, addr


@pytest.mark.skipif(not shutil.which("dhclient"), reason="dhclient not installed")
def test_netboot_filename_is_served_from_the_dhcp_function(fabric):
    """`filename` is what points a guest's iPXE ROM at the host-local proxy,
    so installing a VM over the overlay needs no flat boot NIC."""
    port = str(sys_uuid.uuid4())
    mac = "52:54:00:77:88:99"
    fabric.write_record(
        port,
        mac,
        "10.42.0.9",
        vni=16008,
        cidr="10.42.0.0/24",
        routers=[{"to": "0.0.0.0/0", "via": "10.42.0.254"}],
    )
    # the netboot target is the dhcp *function's* setting, delivered as its
    # own slice beside the guests it serves
    fabric.apply_nf_config(
        16008, "dhcp", filename="http://169.254.169.254/boot/boot.ipxe"
    )
    fabric.start_nf()
    guest = fabric.add_guest("g4", vlan=8, mac=mac, nfs=("dhcp", "dns"))

    out = guest.dhclient()
    assert "bound to 10.42.0.9" in out, out
    # dhclient records the BOOTP `file` field / option 67 it was handed —
    # the same value a guest's iPXE ROM chain-loads from.
    with open(guest.lease_path) as fl:
        lease = fl.read()
    assert "http://169.254.169.254/boot/boot.ipxe" in lease, lease


@pytest.mark.skipif(not shutil.which("dhclient"), reason="dhclient not installed")
def test_dhcp_survives_a_default_deny_security_group(fabric):
    """The `dhcp` function intercepts the guest's DHCP before the splitter
    sees it, so a default-deny group cannot cost the guest its own lease."""
    port = str(sys_uuid.uuid4())
    mac = "52:54:00:de:ad:01"
    fabric.write_record(
        port,
        mac,
        "10.42.0.12",
        vni=16012,
        cidr="10.42.0.0/24",
        mtu=1450,
        routers=[{"to": "0.0.0.0/0", "via": "10.42.0.254"}],
    )
    fabric.start_nf()
    guest = fabric.add_guest("gsg", vlan=12, mac=mac, nfs=("dhcp", "dns"))
    # a group that permits nothing at all
    fabric.apply_security_group(guest, [{"direction": "ingress", "proto": "icmp"}])

    out = guest.dhclient()
    assert "bound to 10.42.0.12" in out, out
