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
from __future__ import annotations

from unittest import mock
import uuid as sys_uuid

from gcl_sdk.agents.universal.clients.orch import http as orch_http
from gcl_sdk.agents.universal.dm import models


class TestHttpOrchClientAgentsUpdate:
    """Tests for HttpOrchClient.agents_update.

    `status` is read-only over the API (see UniversalAgent.status) - the
    client must not send its own self-reported value. The server activates
    a re-registering agent itself (see UniversalAgentsController.update).
    """

    def setup_method(self):
        self.client = orch_http.HttpOrchClient(
            orch_endpoint="http://localhost:11011",
            status_endpoint="http://localhost:11012",
            http_client=mock.MagicMock(),
        )
        self.client._status_api = mock.MagicMock()

    def test_agents_update_does_not_send_status(self):
        agent = models.UniversalAgent.from_system_uuid(
            capabilities=["pool", "local_pool"],
            facts=[],
            agent_uuid=sys_uuid.uuid4(),
            system_uuid=sys_uuid.uuid4(),
        )
        self.client._status_api.agents.update.return_value = agent

        self.client.agents_update(agent)

        self.client._status_api.agents.update.assert_called_once_with(
            agent.uuid,
            capabilities=agent.capabilities,
            facts=agent.facts,
            name=agent.name,
        )
