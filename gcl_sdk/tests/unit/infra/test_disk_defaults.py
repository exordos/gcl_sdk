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

from types import SimpleNamespace
import uuid

import pytest

from gcl_sdk.infra import constants as c
from gcl_sdk.infra.dm import models


@pytest.mark.parametrize(
    "spec",
    [
        models.RootDiskSpec(image="image", size=10),
        models.SetRootDiskSpec(image="image", size=10).node_spec(None, uuid.uuid4()),
        models.DisksSpec(
            disks=[{"size": 10, "image": "image"}, {"size": 20, "label": "data"}]
        ),
    ],
)
def test_generated_volumes_default_to_warm_ephemeral(spec):
    node = SimpleNamespace(
        uuid=uuid.uuid4(), node_type=c.NodeType.VM, project_id=uuid.uuid4()
    )
    for volume in spec.volumes(node):
        assert volume.tier == c.DiskTier.WARM.value
        assert volume.ephemeral is True


def test_explicit_persistent_warm_policy_is_preserved():
    node = SimpleNamespace(
        uuid=uuid.uuid4(), node_type=c.NodeType.VM, project_id=uuid.uuid4()
    )
    spec = models.SetRootDiskSpec(
        image="image", size=10, tier=c.DiskTier.WARM.value, ephemeral=False
    )
    volume = spec.node_spec(None, node.uuid).volumes(node)[0]
    assert volume.tier == c.DiskTier.WARM.value
    assert volume.ephemeral is False
