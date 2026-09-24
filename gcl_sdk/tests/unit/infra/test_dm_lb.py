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

import uuid as sys_uuid

from gcl_sdk.infra.dm import models

PROJECT = sys_uuid.uuid4()
LB = sys_uuid.uuid4()


def test_lb_on_node():
    node = sys_uuid.uuid4()
    lb = models.LB(
        uuid=LB,
        name="realm-lb",
        project_id=PROJECT,
        type=models.LBTypeNodeKind(node=node),
    )

    assert lb.get_resource_kind() == "lb"
    assert set(lb.get_resource_target_fields()) == {
        "uuid",
        "name",
        "type",
        "project_id",
    }
    assert lb.dump_to_simple_view()["type"] == {"kind": "node", "node": str(node)}


def test_lb_defaults_to_core_vm():
    lb = models.LB(uuid=LB, name="lb", project_id=PROJECT)

    assert lb.type.kind == "core"
    assert lb.ipsv4 == []


def test_vhost_targets_its_lb():
    vhost = models.LBVhost(
        uuid=sys_uuid.uuid4(),
        name="repo",
        project_id=PROJECT,
        lb=LB,
        port=8080,
    )

    assert vhost.get_resource_kind() == "lb_vhost"
    assert {"lb", "protocol", "port"} <= set(vhost.get_resource_target_fields())
    assert vhost.protocol == "http"
    assert vhost.external_sources == []


def test_vhost_declares_optional_fields_only_when_set():
    # Core leaves unset ones out of its answer; declared as None they would
    # never match and the vhost would be updated on every iteration.
    plain = models.LBVhost(
        uuid=sys_uuid.uuid4(), name="l4", project_id=PROJECT, lb=LB, protocol="tcp"
    )
    assert not {"domains", "cert", "proxy_protocol_from"} & set(
        plain.get_resource_target_fields()
    )

    tls = models.LBVhost(
        uuid=sys_uuid.uuid4(),
        name="tls",
        project_id=PROJECT,
        lb=LB,
        protocol="https",
        domains=["repo.example.com"],
        cert={"kind": "raw", "crt": "c", "key": "k"},
        proxy_protocol_from="10.0.0.0/8",
    )
    assert {"domains", "cert", "proxy_protocol_from"} <= set(
        tls.get_resource_target_fields()
    )


def test_route_targets_its_lb_and_vhost():
    vhost = sys_uuid.uuid4()
    route = models.LBVhostRoute(
        uuid=sys_uuid.uuid4(),
        name="repo",
        project_id=PROJECT,
        lb=LB,
        vhost=vhost,
        condition={
            "kind": "prefix",
            "value": "/repo/",
            "actions": [{"kind": "local_dir", "path": "/var/www/repo"}],
            "modifiers": [],
        },
    )

    assert route.get_resource_kind() == "lb_vhost_route"
    assert {"lb", "vhost", "condition"} <= set(route.get_resource_target_fields())
    assert route.vhost == vhost


def test_backend_pool_targets_its_lb():
    pool = models.LBBackendPool(
        uuid=sys_uuid.uuid4(),
        name="core-api",
        project_id=PROJECT,
        lb=LB,
        endpoints=[{"kind": "host", "host": "192.168.100.2", "port": 11010}],
    )

    assert pool.get_resource_kind() == "lb_backendpool"
    assert {"lb", "endpoints", "balance"} <= set(pool.get_resource_target_fields())
    assert pool.balance == "roundrobin"
