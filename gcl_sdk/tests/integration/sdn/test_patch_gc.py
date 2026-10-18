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

"""Integration: the real per-guest patch + metadata wiring and its GC.

Drives the actual driver helpers (``_local_vlan_for_port``,
``_ensure_guest_patch``, ``_ensure_vni_metadata``, ``_gc_guest``) against
real OVS/iproute2 and asserts the objects appear and are fully removed.
"""

import subprocess
import uuid as sys_uuid

from gcl_sdk.agents.universal.drivers import evpn
from gcl_sdk.agents.universal.drivers.evpn import hostns


def _exists(kind, name):
    r = subprocess.run(
        ["ovs-vsctl", "--if-exists", "get", kind, name, "name"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return name in r.stdout


def test_per_guest_patch_gc(fabric):
    """A port's own dataplane goes with it — and only its own. The VNI's
    namespace and port serve every guest on it, so they stay until the last
    one leaves (`_gc_vni`, covered in the egress tier)."""
    port = sys_uuid.uuid4()
    vlan = hostns.local_vlan_for_port(port)
    guest = fabric.add_guest(
        "gc1", vlan=vlan, ip="10.70.0.2/24", mac="52:54:00:70:00:01"
    )
    vni = fabric.vni_of(guest)

    # real driver dataplane: this guest's own patch pair, and the VNI's port
    pe_ofport = _EnsurePatch(fabric.br, fabric.evpn_br).ensure(vlan)
    nf_ofport = fabric.ensure_vni(vni)
    assert pe_ofport > 0 and nf_ofport > 0 and guest.ofport
    assert _exists("port", "pi%d" % vlan)
    assert _exists("interface", "pe%d" % vlan)
    assert _exists("port", hostns.nf_port(vni))

    # the real per-port GC removes the guest's own wiring and frees the VLAN
    hostns.gc_guest(port)
    assert not _exists("port", "pi%d" % vlan)
    assert not _exists("interface", "pe%d" % vlan)
    assert hostns.lookup_local_vlan(port) is None
    # ... and leaves the VNI's port alone: other guests are still on it
    assert _exists("port", hostns.nf_port(vni))


class _EnsurePatch:
    """Minimal shim to call ``EvpnPort._ensure_guest_patch`` (an instance
    method) without the full meta model."""

    def __init__(self, br, evpn_br):
        self.uuid = sys_uuid.uuid4()

    def ensure(self, vlan):
        return evpn.EvpnPort._ensure_guest_patch(self, vlan)
