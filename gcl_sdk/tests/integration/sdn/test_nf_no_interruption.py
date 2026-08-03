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

"""Integration: wiring a new function must not disturb a working one.

Guests arrive one at a time and networks are created while others are
running, so installing a function has to be additive from the point of
view of every guest that already has one. The properties this pins down:

* a guest's flows are its own — a new guest, or a whole new VNI, must not
  reinstall or renumber them (a reinstall shows up as a flow whose
  duration went backwards);
* the responder is not restarted for a newcomer, so nobody loses a lease
  or a resolver while a neighbour is being wired;
* what was answered before is still answered afterwards.
"""

import shutil
import subprocess
import uuid as sys_uuid

import pytest

from gcl_sdk.agents.universal.drivers.evpn import base
from gcl_sdk.agents.universal.drivers.evpn import flows

GW = "10.42.0.254"
GW_MAC = "12:34:56:78:90:ab"


def _flow_durations(bridge, cookie):
    """{flow match: duration} for one cookie, as OVS reports it."""
    out = subprocess.run(
        [
            "ovs-ofctl",
            "-O",
            base.OPENFLOW_VERSION,
            "dump-flows",
            bridge,
            "cookie=%s/-1" % cookie,
        ],
        stdout=subprocess.PIPE,
        text=True,
    ).stdout
    durations = {}
    for line in out.splitlines():
        if "duration=" not in line:
            continue
        parts = dict(
            kv.split("=", 1)
            for kv in line.strip().split(" ")[0].split(",")
            if "=" in kv
        )
        match = line.split("priority=", 1)[-1]
        durations[match] = float(parts.get("duration", "0").rstrip("s"))
    return durations


def _pid_of_responder(harness):
    return harness._procs[0].pid if harness._procs else None


@pytest.mark.skipif(not shutil.which("dhclient"), reason="dhclient not installed")
def test_a_new_vni_does_not_disturb_a_working_guest(fabric):
    """The first guest keeps its lease, its flows and its responder while a
    guest of another VNI is wired next to it."""
    mac_a = "52:54:00:c0:01:01"
    fabric.write_record(
        str(sys_uuid.uuid4()),
        mac_a,
        "10.42.0.21",
        vni=16021,
        cidr="10.42.0.0/24",
        mtu=1450,
        routers=[{"to": "0.0.0.0/0", "via": GW}],
    )
    fabric.start_nf()
    guest_a = fabric.add_guest("ia", vlan=21, mac=mac_a, nfs=("dhcp", "dns"))
    assert "bound to 10.42.0.21" in guest_a.dhclient(wait=8)

    cookie_a = flows.port_cookie(guest_a.name, "nf")
    before = _flow_durations(fabric.br, cookie_a)
    assert before, "the first guest must have its own flows"
    responder = _pid_of_responder(fabric)

    # A whole new VNI arrives: its own namespace, port, records and flows.
    mac_b = "52:54:00:c0:02:02"
    fabric.write_record(
        str(sys_uuid.uuid4()),
        mac_b,
        "10.42.0.22",
        vni=16022,
        cidr="10.42.0.0/24",
        mtu=1450,
        routers=[{"to": "0.0.0.0/0", "via": GW}],
    )
    guest_b = fabric.add_guest("ib", vlan=22, mac=mac_b, nfs=("dhcp", "dns"))
    assert "bound to 10.42.0.22" in guest_b.dhclient(wait=10)

    after = _flow_durations(fabric.br, cookie_a)
    assert set(after) == set(before), "the first guest's flow set changed"
    for match, duration in after.items():
        assert duration >= before[match], (
            "flow %r was reinstalled while another VNI was wired" % match
        )
    assert _pid_of_responder(fabric) == responder, "the responder was restarted"

    # ... and the first guest is still served: it can renew.
    assert "bound to 10.42.0.21" in guest_a.dhclient(wait=8)


@pytest.mark.skipif(not shutil.which("dig"), reason="dig not installed")
def test_a_second_guest_on_the_same_vni_does_not_disturb_the_first(fabric):
    """Same namespace, same port, same daemon thread: the newcomer's record
    and flows are added, the neighbour's are not touched, and the zone it
    resolves keeps answering."""
    mac_a = "52:54:00:c0:03:03"
    fabric.write_record(
        str(sys_uuid.uuid4()),
        mac_a,
        "10.42.0.31",
        vni=16031,
        cidr="10.42.0.0/24",
        name="first",
    )
    fabric.start_nf()
    guest_a = fabric.add_guest(
        "sa", vlan=31, ip="10.42.0.31/24", mac=mac_a, nfs=("dhcp", "dns")
    )
    guest_a.exec("ip", "neigh", "replace", GW, "lladdr", GW_MAC, "dev", guest_a.dev)

    def _resolve():
        return guest_a.exec(
            "dig",
            "+time=2",
            "+tries=1",
            "+short",
            "@" + GW,
            "first.internal",
            check=False,
            timeout=12,
        ).strip()

    assert _resolve() == "10.42.0.31"
    cookie_a = flows.port_cookie(guest_a.name, "nf")
    before = _flow_durations(fabric.br, cookie_a)

    # A neighbour joins the same VNI — same records directory, so the
    # responder reloads it; that must not cost the first guest anything.
    mac_b = "52:54:00:c0:04:04"
    fabric.write_record(
        str(sys_uuid.uuid4()),
        mac_b,
        "10.42.0.32",
        vni=16031,
        cidr="10.42.0.0/24",
        name="second",
    )
    guest_b = fabric.add_guest(
        "sb", vlan=32, ip="10.42.0.32/24", mac=mac_b, nfs=("dhcp", "dns"), vni=16031
    )
    guest_b.exec("ip", "neigh", "replace", GW, "lladdr", GW_MAC, "dev", guest_b.dev)

    after = _flow_durations(fabric.br, cookie_a)
    assert set(after) == set(before)
    for match, duration in after.items():
        assert duration >= before[match], (
            "flow %r was reinstalled while a neighbour joined" % match
        )
    # both names now resolve, and the first guest never stopped resolving
    assert _resolve() == "10.42.0.31"
    assert (
        guest_a.exec(
            "dig",
            "+time=2",
            "+tries=1",
            "+short",
            "@" + GW,
            "second.internal",
            check=False,
            timeout=12,
        ).strip()
        == "10.42.0.32"
    )
