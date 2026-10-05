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
import uuid

import pytest

from gcl_sdk.agents.universal.drivers import storage_capacity as capacity

GIB = 1 << 30


def node(host, free):
    return {
        "uuid": host,
        "failure_domain_path": f"dc1/row1/rack1/{host}",
        "available": free * GIB,
    }


def policy(mirrors):
    return {"mirrors": mirrors, "failure_domain": "server", "chunk_size": GIB}


def test_two_pools_share_one_physical_budget():
    nodes = [node("host1", 100), node("host2", 100)]
    assert capacity.available_by_policy(nodes, policy(1)) == 200 * GIB
    assert capacity.available_by_policy(nodes, policy(2)) == 100 * GIB
    # A pending disk from either pool reduces both admission figures.
    assert capacity.available_by_policy(nodes, policy(1), [20 * GIB]) == 160 * GIB
    assert capacity.available_by_policy(nodes, policy(2), [20 * GIB]) == 80 * GIB


def test_replication_is_limited_by_distinct_domain_capacity():
    nodes = [node("host1", 180), node("host2", 20)]
    assert capacity.available_by_policy(nodes, policy(2)) == 20 * GIB


def test_multiple_osts_on_one_server_do_not_satisfy_two_mirrors():
    nodes = [node("host1", 100), node("host1", 100)]
    assert capacity.available_by_policy(nodes, policy(2)) == 0


def test_rack_domains_use_outermost_first_paths():
    nodes = [node("host1", 100), node("host2", 100)]
    p = {**policy(2), "failure_domain": "rack"}
    assert capacity.available_by_policy(nodes, p) == 0
    nodes[1]["failure_domain_path"] = "dc1/row1/rack2/host2"
    assert capacity.available_by_policy(nodes, p) == 100 * GIB


def test_short_server_paths_share_omitted_outer_domains():
    n = {"uuid": "ost1", "failure_domain_path": "host1", "available": GIB}
    assert capacity.domain(n, "server") == "///host1"
    assert capacity.domain(n, "dc") == ""


def test_pending_does_not_make_capacity_negative():
    assert capacity.available_by_policy([node("host1", 10)], policy(1), [20 * GIB]) == 0


def test_default_policies_have_stable_identity_and_distinct_replication():
    cluster = uuid.uuid4()
    policies = capacity.default_policies(cluster)
    assert policies == capacity.default_policies(cluster)
    assert {(p["ephemeral"], p["mirrors"]) for p in policies.values()} == {
        (True, 1),
        (False, 2),
    }


@pytest.mark.parametrize("chunk", [0, 3 * GIB, 2 * GIB])
def test_policy_rejects_unsupported_chunk_sizes(chunk):
    p = next(iter(capacity.default_policies(uuid.uuid4()).values()))
    with pytest.raises(ValueError):
        capacity.validate_policy({**p, "chunk_size": chunk})
