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

from restalchemy.api import constants as ra_c
from restalchemy.api import contexts
from webob.request import Request

from gcl_sdk.agents.universal import constants as c
from gcl_sdk.agents.universal.dm import models
from gcl_sdk.agents.universal.orch_api import controllers


def _req_for_method(method):
    req = Request(environ={})
    req.api_context = contexts.RequestContext(req)
    req.api_context.set_active_method(method)
    return req


class TestUniversalAgentsControllerFieldPermissions:
    def test_status_is_read_only_for_update_only(self):
        # CREATE stays RW: older clients that still send their own initial
        # status (e.g. exordos_seed) must not get their request rejected -
        # the controller overrides the value instead (see
        # TestUniversalAgentsControllerCreate).
        perms = controllers.UniversalAgentsController.__resource__._fields_permissions

        assert perms.is_readonly("status", _req_for_method(ra_c.CREATE)) is False
        assert perms.is_readonly("status", _req_for_method(ra_c.UPDATE)) is True


class TestUniversalAgentsControllerCreate:
    def test_create_always_starts_the_agent_active(self):
        # A client-supplied status (e.g. from an older client that still
        # sends one) must not be rejected - it's simply overridden, same
        # end result as on update.
        controller = controllers.UniversalAgentsController(mock.MagicMock())

        with mock.patch.object(models.UniversalAgent, "insert"):
            result = controller.create(
                uuid=sys_uuid.uuid4(),
                name="agent",
                node=sys_uuid.uuid4(),
                status=c.AgentStatus.DISABLED.value,
            )

        assert result.status == c.AgentStatus.ACTIVE.value


class TestUniversalAgentsControllerUpdate:
    def test_update_always_activates_the_agent(self):
        # A re-registering agent (e.g. after its capabilities changed) goes
        # through this path - it must always end up ACTIVE, regardless of
        # what status it was in before or what the client sends, since
        # `status` is read-only over the API (see the controller's
        # fields_permissions) even though the underlying model field itself
        # stays freely writable for other in-process code (builders, etc).
        controller = controllers.UniversalAgentsController(mock.MagicMock())
        agent = mock.MagicMock()
        uuid = sys_uuid.uuid4()

        with mock.patch.object(controller, "get", return_value=agent) as mock_get:
            result = controller.update(uuid, name="new-name")

        mock_get.assert_called_once_with(uuid=uuid)
        agent.update_dm.assert_called_once_with(
            values={"name": "new-name", "status": c.AgentStatus.ACTIVE.value}
        )
        agent.update.assert_called_once_with()
        assert result is agent
