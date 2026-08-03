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

"""What the lease carries besides the address.

An address is the part of a lease everyone checks, and the part that fails
loudly. The rest of it is what decides whether the guest is actually usable:
the size it may send, who resolves its names, and how long any of it is good
for. Each of those is served by the responder today and none of them was
ever read back by a test, so a lease that quietly stopped carrying one would
look exactly like a lease that worked.

The MTU is the one that matters most, and it is the reason this file sits
next to `test_path_mtu`. Those cases prove the fabric *recovers* when a
guest sends something too big -- the guest is told, and adjusts. This one
proves the guest is told the size up front, so that recovery is a backstop
rather than the mechanism. An overlay that leaks its full 1500 into a lease
works for every small packet and stalls on the first large one, which is the
single most expensive way for this to be wrong.

OVN reads the whole option set back in `dhcp_options` tests, and Neutron
asserts the MTU option specifically because it had this bug.
"""

import re
import shutil
import uuid as sys_uuid

import pytest


MTU = 1450
VNI = 16041
CIDR = "10.42.4.0/24"
GATEWAY = "10.42.4.254"
ADDRESS = "10.42.4.11"
MAC = "52:54:00:4a:00:11"

needs_dhclient = pytest.mark.skipif(
    not shutil.which("dhclient"), reason="dhclient not installed"
)


def _lease(guest):
    """The lease as the guest's own client recorded it.

    Read from the client's lease file rather than from the interface: what
    is under test is what the responder *offered*, and an option the guest's
    script chooses not to apply is still an option that was served.
    """
    with open("/tmp/dhclient-%s.lease" % guest.ns) as fl:
        return fl.read()


def _option(lease, name):
    found = re.findall(r"option %s (.+?);" % re.escape(name), lease)
    return found[-1] if found else None


@pytest.fixture
def leased(fabric):
    """One guest with a record, and its lease taken."""
    fabric.write_record(
        str(sys_uuid.uuid4()),
        MAC,
        ADDRESS,
        vni=VNI,
        cidr=CIDR,
        mtu=MTU,
        routers=[{"to": "0.0.0.0/0", "via": GATEWAY}],
    )
    fabric.start_nf()
    guest = fabric.add_guest("do", vlan=41, mac=MAC, nfs=("dhcp", "dns"))
    out = guest.dhclient()
    assert "bound to %s" % ADDRESS in out, out
    return guest


@needs_dhclient
def test_the_lease_tells_the_guest_the_size_it_may_send(leased):
    """The overlay's MTU, handed over before the guest can get it wrong.

    Not the same claim as path MTU discovery next door: this is the guest
    never generating an oversized packet in the first place. Both have to
    hold -- the first because it is cheap and silent, the second because
    nothing guarantees every guest honours the first.
    """
    assert _option(_lease(leased), "interface-mtu") == str(MTU), (
        "the lease did not carry the overlay's MTU, so a guest that trusts "
        "its lease will send packets the overlay cannot carry: %s"
        % _lease(leased).strip()
    )


@needs_dhclient
def test_the_lease_hands_over_a_resolver_and_a_way_out(leased):
    """The two addresses a guest is useless without.

    The resolver is the gateway itself -- guests send DNS to the address
    they already route through, and a br-int flow punts port 53 to the
    responder -- so this also states that arrangement, which is invisible
    from the guest's side and easy to break from the host's.
    """
    lease = _lease(leased)

    assert _option(lease, "routers") == GATEWAY, lease
    assert _option(lease, "domain-name-servers") == GATEWAY, (
        "the guest was given no resolver, or one that is not the gateway it "
        "routes through: %s" % lease
    )


@needs_dhclient
def test_the_lease_expires_and_says_when(leased):
    """A lease with no lifetime is a static address with extra steps.

    What the time is worth: it is the interval at which a guest comes back
    and finds out its address changed. Serving no lease-time at all, or one
    long enough to outlive the installation, means a control plane that
    moves an address has no way to reach the guest holding the old one.
    """
    lease_time = _option(_lease(leased), "dhcp-lease-time")

    assert lease_time is not None, "the lease has no lifetime: %s" % _lease(leased)
    assert 0 < int(lease_time) <= 86400, (
        "a lease that lasts %s seconds outlives the changes it must carry" % lease_time
    )


@needs_dhclient
def test_a_guest_holding_the_wrong_address_is_corrected(fabric):
    """The guest that comes back after its address changed.

    A client with a lease on disk does not ask "what is my address" -- it
    asks for the one it already has, and a server that stays silent leaves
    it retrying forever with an address the fabric will not route. This is
    the ordinary consequence of a port being re-addressed on the control
    plane, so the guest must end up on the record's address rather than on
    the one it asked for.

    Measured rather than assumed, because the responder ignores the
    requested-address option outright: what makes this pass is that it
    answers every REQUEST with the record's address, which real clients
    accept. If a client were ever to insist on what it asked for, this is
    the case that would say so.
    """
    fabric.write_record(
        str(sys_uuid.uuid4()),
        MAC,
        ADDRESS,
        vni=VNI,
        cidr=CIDR,
        mtu=MTU,
        routers=[{"to": "0.0.0.0/0", "via": GATEWAY}],
    )
    fabric.start_nf()
    guest = fabric.add_guest("st", vlan=41, mac=MAC, nfs=("dhcp", "dns"))

    # The lease a guest would be holding from before the change.
    with open("/tmp/dhclient-%s.lease" % guest.ns, "w") as fl:
        fl.write(
            "lease {\n"
            '  interface "%s";\n'
            "  fixed-address 10.42.4.99;\n"
            "  option subnet-mask 255.255.255.0;\n"
            "  option routers %s;\n"
            "  renew 1 2030/01/01 00:00:01;\n"
            "  rebind 1 2030/01/01 00:00:01;\n"
            "  expire 1 2030/01/01 00:00:01;\n"
            "}\n" % (guest.dev, GATEWAY)
        )

    out = guest.dhclient()

    assert "bound to %s" % ADDRESS in out, (
        "a guest asking for the address it used to hold was not moved to the "
        "one the control plane now says is its: %s" % out
    )
