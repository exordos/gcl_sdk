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


def _node_set(**kw):
    return models.NodeSet(
        uuid=sys_uuid.uuid4(),
        name="workers",
        project_id=sys_uuid.uuid4(),
        cores=2,
        ram=2048,
        disk_spec=models.SetRootDiskSpec(image="ubuntu_24.04"),
        **kw,
    )


def test_nodes_and_nodesets_have_no_interfaces_declaration():
    assert "interfaces" not in models.Node.properties
    assert "interfaces" not in models.NodeSet.properties


def test_default_network_template_preserves_omission_and_empty_object():
    _, omitted = _node_set().get_ua_all_and_target_values()
    _, empty = _node_set(default_network={}).get_ua_all_and_target_values()
    assert "default_network" not in omitted
    assert empty["default_network"] == {}


def test_nodeset_carries_explicit_floating_network_for_new_replicas():
    request = {"auto_floating_ip": True, "floating_network": str(sys_uuid.uuid4())}
    _, target = _node_set(default_network=request).get_ua_all_and_target_values()
    assert target["default_network"] == request


def test_node_default_network_is_an_ordinary_hashed_field():
    identifier = sys_uuid.uuid4()
    values = dict(
        uuid=identifier,
        project_id=sys_uuid.uuid4(),
        cores=1,
        ram=1024,
        disk_spec=models.RootDiskSpec(image="ubuntu_24.04"),
    )
    omitted = models.Node(**values).to_ua_resource()
    empty = models.Node(**values, default_network={}).to_ua_resource()
    fip = models.Node(
        **values, default_network={"auto_floating_ip": True}
    ).to_ua_resource()
    assert "default_network" not in omitted.value
    assert empty.value["default_network"] == {}
    assert fip.value["default_network"] == {"auto_floating_ip": True}
    assert len({omitted.hash, empty.hash, fip.hash}) == 3


def test_node_and_nodeset_have_no_networking_field():
    for model in (models.Node, models.NodeSet):
        assert "networking" not in model.properties.properties
    assert "networking" not in _node_set().get_resource_target_fields()
