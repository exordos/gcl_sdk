#    Copyright 2026-2026 Genesis Corporation.
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
from __future__ import annotations
from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch
import uuid as sys_uuid

import pytest

from gcl_sdk.agents.universal.drivers import core


@pytest.mark.parametrize(
    "driver_cls, spec",
    [
        (core.RestCoreCapabilityDriver, "/v1/compute/sets/"),
        (core.SecretCapabilityDriver, "/v1/iam/users/, password"),
    ],
)
def test_driver_uses_given_http_client(tmp_path, driver_cls, spec):
    http = MagicMock()

    with (
        patch.object(core.base, "CoreIamAuthenticator") as auth,
        patch.object(core.base, "CollectionBaseClient") as client,
    ):
        driver_cls(
            username="admin",
            password="secret",
            user_api_base_url="http://core.test/api/core",
            project_id=sys_uuid.uuid4(),
            agent_work_dir=str(tmp_path),
            http_client=http,
            em_kind=spec,
        )

    assert auth.call_args.kwargs["http_client"] is http
    assert client.call_args.kwargs["http_client"] is http
