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
from unittest import mock

from gcl_sdk.agents.universal import constants as c
from gcl_sdk.agents.universal.clients.orch import db as orch_db
from gcl_sdk.agents.universal.dm import models


class TestDatabaseOrchClientAgentsUpdate:
    def test_agents_update_activates_the_agent(self):
        # A re-registering agent (uuid already exists) goes through this
        # path - it must always end up ACTIVE, since `status` is read-only
        # (see UniversalAgent.status) and this client is the trusted
        # in-process caller responsible for activating it directly.
        client = orch_db.DatabaseOrchClient()
        agent_uuid = sys_uuid.uuid4()

        incoming = models.UniversalAgent(
            uuid=agent_uuid,
            name="new-name",
            node=sys_uuid.uuid4(),
            capabilities={"capabilities": ["pool"]},
            facts={"facts": []},
        )
        origin_agent = models.UniversalAgent(
            uuid=agent_uuid,
            name="old-name",
            node=sys_uuid.uuid4(),
            status=c.AgentStatus.NEW.value,
        )

        with (
            mock.patch.object(
                models.UniversalAgent._ObjectCollection,
                "get_one",
                return_value=origin_agent,
            ),
            mock.patch.object(origin_agent, "save"),
        ):
            result = client.agents_update(incoming, session=mock.MagicMock())

        assert result.status == c.AgentStatus.ACTIVE.value
