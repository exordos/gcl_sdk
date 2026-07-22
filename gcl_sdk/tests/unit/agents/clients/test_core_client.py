#    Copyright 2025 Genesis Corporation.
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

from unittest import mock
import uuid as sys_uuid

from gcl_sdk.agents.universal.clients.http import core as core_client

AGENT_UUID = sys_uuid.UUID("55c6968c-d26e-58a3-cfe2-11afde248319")


class TestCoreAgentsClient:
    """Tests for CoreAgentsClient."""

    def setup_method(self):
        self.client = core_client.CoreAgentsClient(
            "http://localhost:8080",
            http_client=mock.MagicMock(),
        )

    def test_issue_key(self):
        self.client.do_action = mock.MagicMock(return_value={"key": "s3cr3t"})

        result = self.client.issue_key(AGENT_UUID)

        assert result == "s3cr3t"
        self.client.do_action.assert_called_once_with(
            "issue_key", AGENT_UUID, invoke=True
        )
