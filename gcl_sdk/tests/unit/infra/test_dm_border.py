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


def _border(**kw):
    return models.Border(
        uuid=sys_uuid.uuid4(),
        name="realm-border",
        project_id=sys_uuid.uuid4(),
        **kw,
    )


def test_border_resource_kind():
    assert _border().get_resource_kind() == "border"


def test_border_target_fields_include_node_and_rules():
    fields = set(_border().get_resource_target_fields())
    assert {
        "uuid",
        "name",
        "project_id",
        "node",
        "snat_rules",
        "forwards",
    } <= fields


def test_border_defaults():
    border = _border()
    assert border.node is None
    assert border.snat_rules == []
    assert border.forwards == []


def test_border_carries_inline_rules_and_node():
    node = sys_uuid.uuid4()
    border = _border(
        node=node,
        snat_rules=[
            {"source_cidr": "192.168.100.0/24", "mode": "masquerade", "snat_to": None}
        ],
        forwards=[
            {
                "proto": "tcp",
                "public_ip": None,
                "listen_port": 443,
                "to_host": "192.168.100.2",
                "to_port": 443,
            }
        ],
    )
    assert border.node == node
    assert border.snat_rules[0]["source_cidr"] == "192.168.100.0/24"
    assert border.forwards[0]["to_port"] == 443
