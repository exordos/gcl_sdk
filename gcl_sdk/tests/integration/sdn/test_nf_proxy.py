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

"""Integration: the netboot/metadata proxy path (proxy NF).

A guest fetches its boot script from the cloud-init well-known address;
the per-guest punt rewrites the dst MAC into the per-VLAN ``md<vlan>``
port and the co-located proxy thread answers — overlapping realm
addressing included: two guests in different VLANs with the SAME IP are
served independently.
"""

import pytest


@pytest.fixture
def proxy_fabric(fabric):
    """The proxy binds the well-known port — inside the VNI's namespace.

    It used to need a free high port when the host itself held :80: the
    listener lived in the root namespace and collided with whatever was
    there. One namespace per VNI removes the conflict entirely, which is
    why this fixture no longer overrides anything.
    """
    return fabric


def _url(fabric, path):
    return "http://169.254.169.254%s" % path


def test_guest_fetches_its_boot_script(proxy_fabric):
    fab = proxy_fabric
    fab.start_nf()
    guest = fab.add_guest("p1", vlan=21, ip="10.50.0.4/24", mac="52:54:00:15:00:01")
    fab.apply_metadata(guest, "10.50.0.0/24")

    status, body = guest.http_get(_url(fab, "/boot/boot.ipxe"))
    assert status == 200, (status, body)
    assert body.startswith("#!ipxe")


def test_proxy_is_an_allowlist_not_a_relay(proxy_fabric):
    fab = proxy_fabric
    fab.start_nf()
    guest = fab.add_guest("p2", vlan=22, ip="10.50.0.4/24", mac="52:54:00:15:00:02")
    fab.apply_metadata(guest, "10.50.0.0/24")

    status, _ = guest.http_get(_url(fab, "/etc/passwd"))
    assert status == 404
    # The boot prefix is one composed answer, not a directory: nothing under
    # it is read off the host, so there is nothing to traverse out of.
    status, body = guest.http_get(_url(fab, "/boot/../../etc/passwd"))
    assert status in (403, 404), (status, body)
    assert "root:" not in body


def test_overlapping_realm_ips_served_independently(proxy_fabric):
    fab = proxy_fabric
    fab.start_nf()
    # the SAME guest IP in two different VLANs, one proxy process
    a = fab.add_guest("pa", vlan=23, ip="10.60.0.5/24", mac="52:54:00:15:00:0a")
    b = fab.add_guest("pb", vlan=24, ip="10.60.0.5/24", mac="52:54:00:15:00:0b")
    fab.apply_metadata(a, "10.60.0.0/24")
    fab.apply_metadata(b, "10.60.0.0/24")

    sa, ba = a.http_get(_url(fab, "/boot/boot.ipxe"))
    sb, bb = b.http_get(_url(fab, "/boot/boot.ipxe"))
    assert sa == 200 and ba.startswith("#!ipxe"), (sa, ba)
    assert sb == 200 and bb.startswith("#!ipxe"), (sb, bb)
