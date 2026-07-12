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

"""The nginx config files are one shared dataplane per node, while the agent
may carry several LB resources. Rendering must therefore be the uuid-sorted
union of all of them (identical no matter which resource renders), or the
resources wipe each other's vhosts and validation flip-flops forever."""

import uuid as sys_uuid

import pytest

from gcl_sdk.agents.universal.drivers import lb as lb_driver
from gcl_sdk.agents.universal.storage import common as storage_common

UUID_A = sys_uuid.UUID("00000000-0000-4000-8000-00000000000a")
UUID_B = sys_uuid.UUID("00000000-0000-4000-8000-00000000000b")


def _vhost(uuid, port, pool):
    return {
        "uuid": str(uuid),
        "proto": "http",
        "port": port,
        "domains": ["_"],
        "cert": None,
        "ext_sources": [],
        "proxy_proto_from": None,
        "routes": {
            str(sys_uuid.uuid5(uuid, "route")): {
                "cond": {
                    "kind": "prefix",
                    "value": "/",
                    "actions": [
                        {
                            "kind": "backend",
                            "pool": pool,
                            "protocol": {"kind": "http"},
                        }
                    ],
                    "modifiers": [],
                    "allowed_ips": ["0.0.0.0/0"],
                }
            }
        },
    }


def _pool(host, port):
    return {
        "balance": "roundrobin",
        "endpoints": [{"kind": "host", "host": host, "port": port, "weight": 1}],
    }


@pytest.fixture
def shared_meta(tmp_path, monkeypatch):
    """Two LB models sharing one meta file, both present in the storage."""
    meta_file = str(tmp_path / "lb_meta.json")
    monkeypatch.setattr(lb_driver.LB, "META_PATH", meta_file)
    # The singleton caches instances by path; a tmp path is always fresh.
    storage = storage_common.JsonFileStorageSingleton.get_instance(meta_file)

    pool_a, pool_b = str(sys_uuid.uuid4()), str(sys_uuid.uuid4())
    lb_a = lb_driver.LB(
        uuid=UUID_A,
        vhosts=[_vhost(UUID_A, 80, pool_a)],
        backend_pools={pool_a: _pool("127.0.0.1", 11010)},
    )
    lb_b = lb_driver.LB(
        uuid=UUID_B,
        vhosts=[_vhost(UUID_B, 8443, pool_b)],
        backend_pools={pool_b: _pool("10.0.0.5", 80)},
    )
    storage["paas_lb_agent"] = {
        "resources": {
            str(UUID_A): {
                "uuid": str(UUID_A),
                "vhosts": lb_a.vhosts,
                "backend_pools": lb_a.backend_pools,
            },
            str(UUID_B): {
                "uuid": str(UUID_B),
                "vhosts": lb_b.vhosts,
                "backend_pools": lb_b.backend_pools,
            },
        }
    }
    return lb_a, lb_b


def _render(lb):
    vhosts_l4, vhosts_l7, _ext = lb._gen_vhosts()
    return (
        lb._gen_file_content_l4(vhosts_l4),
        lb._gen_file_content_l7(vhosts_l7),
    )


def test_render_is_the_union_and_identical_from_either_resource(shared_meta):
    lb_a, lb_b = shared_meta

    l4_a, l7_a = _render(lb_a)
    l4_b, l7_b = _render(lb_b)

    # Byte-identical render regardless of which resource produces it --
    # this is what makes cross-validation stable (no flip-flop).
    assert (l4_a, l7_a) == (l4_b, l7_b)
    # And it is the union: both vhosts and both upstreams are present.
    assert "listen 0.0.0.0:80" in l7_a
    assert "listen 0.0.0.0:8443" in l7_a
    assert "127.0.0.1:11010" in l7_a
    assert "10.0.0.5:80" in l7_a


def test_self_is_authoritative_over_its_stale_meta_view(shared_meta):
    lb_a, _lb_b = shared_meta

    # Simulate an in-flight update: self carries the new state while the
    # meta file still holds the pre-update view.
    new_pool = str(sys_uuid.uuid4())
    lb_a.vhosts = [_vhost(UUID_A, 8081, new_pool)]
    lb_a.backend_pools = {new_pool: _pool("192.0.2.1", 9000)}

    _l4, l7 = _render(lb_a)
    assert "listen 0.0.0.0:8081" in l7
    assert "192.0.2.1:9000" in l7
    # The stale meta view of A itself must not leak into the render...
    assert "listen 0.0.0.0:80;" not in l7
    assert "127.0.0.1:11010" not in l7
    # ...while sibling B is untouched.
    assert "listen 0.0.0.0:8443" in l7


def test_catch_all_vhost_is_the_explicit_default_server(shared_meta):
    # "_" never matches a real Host: without an explicit default_server the
    # first (uuid-sorted) sibling's vhost would swallow every unmatched Host.
    lb_a, _lb_b = shared_meta
    lb_a.vhosts[0]["domains"] = ["_"]
    view_b = lb_a._common_storage["paas_lb_agent"]["resources"][str(UUID_B)]
    view_b["vhosts"][0]["domains"] = ["site.example"]

    _l4, l7 = _render(lb_a)
    assert "listen 0.0.0.0:80 default_server;" in l7
    # The named sibling vhost must not carry the flag.
    assert "listen 0.0.0.0:8443;" in l7


def test_only_one_default_server_per_port(shared_meta):
    # Two aggregated catch-alls on one port must not render two
    # default_server flags (nginx would refuse the whole config).
    lb_a, lb_b = shared_meta
    lb_a.vhosts[0]["domains"] = ["_"]
    view_b = lb_a._common_storage["paas_lb_agent"]["resources"][str(UUID_B)]
    view_b["vhosts"][0]["domains"] = ["_"]
    view_b["vhosts"][0]["port"] = 80

    _l4, l7 = _render(lb_a)
    assert l7.count("default_server") == 1


def test_emptied_self_renders_siblings_only(shared_meta):
    # delete_from_dp re-renders with self emptied: the shared files keep
    # the sibling's vhosts instead of being removed.
    lb_a, _lb_b = shared_meta
    lb_a.vhosts = []
    lb_a.backend_pools = {}

    _l4, l7 = _render(lb_a)
    assert "listen 0.0.0.0:80;" not in l7
    assert "listen 0.0.0.0:8443" in l7
