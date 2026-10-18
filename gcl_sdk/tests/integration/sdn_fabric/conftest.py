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

import os

import pytest

from gcl_sdk.tests.integration.sdn import harness as nf_h
from gcl_sdk.tests.integration.sdn_fabric import fabric as f


def _unavailable(reason):
    # Under CI (SDN_REQUIRE_ENV) a missing prerequisite is a broken runner,
    # not a reason to let the tier vanish from a green build.
    if os.environ.get("SDN_REQUIRE_ENV"):
        pytest.fail(reason, pytrace=False)
    pytest.skip(reason, allow_module_level=True)


@pytest.fixture(scope="session", autouse=True)
def requires_fabric_env():
    if not nf_h.env_ready():
        _unavailable("SDN fabric tier needs root + ovs-vsctl/ovs-ofctl/ip/conntrack")
    if not f.cache_ready(f.default_cache_dir()):
        _unavailable(
            "SDN fabric cache missing (gobgpd + evpn-connector venv); "
            "run gcl_sdk/tests/integration/sdn_fabric/prepare.sh first"
        )


@pytest.fixture
def evpn_fabric(tmp_path):
    """The full single-host EVPN fabric: RR + host gobgpd + evpn_connector."""
    harness = f.FabricHarness(str(tmp_path), f.default_cache_dir())
    # The cleanup covers the *setup* too, not only the test: bringing a
    # fabric up is where this tier is most likely to fail, and a failure
    # halfway through it used to leave the daemons and the underlay device
    # behind -- which the next run then collided with, failing for a reason
    # that had nothing to do with itself.
    try:
        harness.setup()
        harness.start_fabric()
        yield harness
    finally:
        harness.cleanup()


@pytest.fixture
def two_host_fabric(tmp_path):
    """Two hosts + one RR: real cross-host EVPN forwarding and fail-static."""
    from gcl_sdk.tests.integration.sdn_fabric import two_host as th

    if not th.daemons_ready():
        # Through `_unavailable`, not a bare skip: these cases are the only
        # cross-host coverage there is -- forwarding over VXLAN, fail-static,
        # a withdrawal reaching the far side -- and on a runner missing the
        # private-OVS tools they would step aside without a word while the
        # job reported the fabric tier green.
        _unavailable(
            "two-host tier needs ovsdb-server/ovsdb-tool/ovs-vswitchd + the "
            "OVS schema to run a private OVS instance per host"
        )
    harness = th.TwoHostFabric(str(tmp_path), f.default_cache_dir())
    try:
        harness.setup()
        harness.start_fabric()
        yield harness
    finally:
        harness.cleanup()
