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
from gcl_sdk.agents.universal.status_api import controllers


class TestUniversalAgentsControllerUpdate:
    def test_update_always_activates_the_agent(self):
        # A re-registering agent (e.g. after its capabilities changed) goes
        # through this path - it must always end up ACTIVE, regardless of
        # what status it was in before or what the client sends, since
        # `status` is read-only over the API (see UniversalAgent.status).
        controller = controllers.UniversalAgentsController(mock.MagicMock())
        agent = mock.MagicMock()
        uuid = sys_uuid.uuid4()

        with mock.patch.object(controller, "get", return_value=agent) as mock_get:
            result = controller.update(uuid, name="new-name")

        mock_get.assert_called_once_with(uuid=uuid)
        agent.update_dm.assert_called_once_with(values={"name": "new-name"})
        agent.properties["status"].set_value_force.assert_called_once_with(
            c.AgentStatus.ACTIVE.value
        )
        agent.update.assert_called_once_with()
        assert result is agent
