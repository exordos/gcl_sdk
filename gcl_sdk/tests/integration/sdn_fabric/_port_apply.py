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

"""Wire one guest port with the *real* EVPN driver, inside a host's netns.

The two-host fabric runs each "host" in its own network namespace with its
own OVS instance, so the driver's OVS/ip commands must execute there. This
tiny entrypoint is invoked as ``ip netns exec <host-ns> env OVS_RUNDIR=...
python -m ...sdn_fabric._port_apply '<json>'`` so ``EvpnPort.dump_to_dp``
runs against that host's namespace and OVS with zero mocking.
"""

import json
import os
import sys
import uuid as sys_uuid

from gcl_sdk.agents.universal.drivers import evpn
from gcl_sdk.agents.universal.drivers.evpn import base
from gcl_sdk.tests.integration.sdn import harness


def main() -> int:
    spec = json.loads(sys.argv[1])
    # The evpn module globals this host owns (`harness.host_paths`).
    for key, value in json.loads(os.environ["EVPN_HOST_GLOBALS"]).items():
        setattr(base, key, value)
    harness.assert_isolated()
    for d in (base.CLIENT_CONF_DIR, base.NF_RECORDS_DIR):
        os.makedirs(d, exist_ok=True)
    port = evpn.EvpnPort(
        uuid=sys_uuid.UUID(spec["uuid"]),
        mac=spec["mac"],
        ipv4=spec["ipv4"],
        vni=spec["vni"],
        imp_rt=spec["imp_rt"],
        exp_rt=spec["exp_rt"],
        dhcp=spec["dhcp"],
        identity=spec.get("identity", 0),
        security_rules=spec.get("security_rules", []),
    )
    # The second argument is the verb, so a test can put a port on a host
    # and later take it away through the same real driver that wired it --
    # a delete is a data-plane operation like any other, and reading its
    # effect (a route withdrawn, a guest that stops answering) needs it to
    # go through the driver rather than around it.
    if len(sys.argv) > 2 and sys.argv[2] == "delete":
        port.delete_from_dp()
    else:
        port.dump_to_dp()
    return 0


if __name__ == "__main__":
    sys.exit(main())
