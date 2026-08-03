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


def test_a_set_that_declares_no_legs_sends_no_interfaces():
    """A target resource is restored field by field on the far side. An
    installation that predates `interfaces` has nothing to restore it into
    and raises, so the set is never built — and a set that says nothing has
    nothing to say here either. Not sending it also leaves the value, and
    the hash over it, the one every installation already agrees on."""
    node_set = _node_set()
    assert node_set.interfaces is None
    assert "interfaces" not in node_set.get_resource_target_fields()

    _all_values, target = node_set.get_ua_all_and_target_values()
    assert "interfaces" not in target


def test_a_set_that_declares_legs_sends_them():
    node_set = _node_set(interfaces=[{"kind": "subnet", "subnet": str(sys_uuid.uuid4())}])
    assert "interfaces" in node_set.get_resource_target_fields()

    _all_values, target = node_set.get_ua_all_and_target_values()
    assert target["interfaces"] == node_set.interfaces


def test_asking_for_no_networking_is_not_saying_nothing():
    """`[]` is a request and `None` is silence; the first has to travel."""
    node_set = _node_set(interfaces=[])
    assert "interfaces" in node_set.get_resource_target_fields()

    _all_values, target = node_set.get_ua_all_and_target_values()
    assert target["interfaces"] == []
