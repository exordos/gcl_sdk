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

from unittest import mock
import uuid as sys_uuid

import pytest
from restalchemy.storage import exceptions as storage_exc

from restalchemy.api import constants as ra_c
from restalchemy.api import contexts
from webob.request import Request

from gcl_sdk.agents.universal import constants as c
from gcl_sdk.agents.universal.status_api import controllers
from gcl_sdk.agents.universal.status_api.dm import models as status_models


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
        request = mock.MagicMock()
        node = sys_uuid.uuid4()
        request.context.encryption_information.node_uuid = node
        controller = controllers.UniversalAgentsController(request)

        with mock.patch.object(status_models.UniversalAgent, "insert"):
            result = controller.create(
                uuid=sys_uuid.uuid4(),
                name="agent",
                node=sys_uuid.uuid4(),
                status=c.AgentStatus.DISABLED.value,
            )

        assert result.status == c.AgentStatus.ACTIVE.value
        assert result.node == node


class TestUniversalAgentsControllerUpdate:
    def test_update_always_activates_the_agent(self):
        # A re-registering agent (e.g. after its capabilities changed) goes
        # through this path - it must always end up ACTIVE, regardless of
        # what status it was in before or what the client sends, since
        # `status` is read-only over the API (see the controller's
        # fields_permissions) even though the underlying model field itself
        # stays freely writable for other in-process code (builders, etc).
        request = mock.MagicMock()
        node = sys_uuid.uuid4()
        request.context.encryption_information.node_uuid = node
        controller = controllers.UniversalAgentsController(request)
        agent = mock.MagicMock()
        uuid = sys_uuid.uuid4()

        with mock.patch.object(controller, "get", return_value=agent) as mock_get:
            result = controller.update(uuid, name="new-name")

        mock_get.assert_called_once_with(uuid=uuid)
        agent.update_dm.assert_called_once_with(
            values={
                "name": "new-name",
                "status": c.AgentStatus.ACTIVE.value,
                "node": node,
            }
        )
        agent.update.assert_called_once_with()
        assert result is agent


class TestResourceOwnership:
    def setup_method(self):
        self.node = sys_uuid.uuid4()
        self.foreign = sys_uuid.uuid4()
        request = mock.MagicMock()
        request.context.encryption_information.node_uuid = self.node
        self.controller = controllers.ResourcesController(request)
        self.kind = status_models.KindModel(kind="config")
        self.uuid = sys_uuid.uuid4()

    @pytest.mark.parametrize("operation", ["get", "update", "delete"])
    def test_foreign_resource_is_not_found(self, operation):
        resource = mock.MagicMock(node=self.foreign)

        def lookup(filters):
            if filters.get("node") and filters["node"].value == self.node:
                raise storage_exc.RecordNotFound(
                    model=status_models.Resource, filters=filters
                )
            return resource

        with mock.patch.object(status_models.Resource, "objects") as objects:
            objects.get_one.side_effect = lookup
            with pytest.raises(storage_exc.RecordNotFound):
                getattr(self.controller, operation)(
                    uuid=self.uuid, parent_resource=self.kind
                )
        resource.update.assert_not_called()
        resource.delete.assert_not_called()

    def test_create_stamps_the_authenticated_node(self):
        with (
            mock.patch.object(
                controllers.ua_models.TargetResource, "objects"
            ) as targets,
            mock.patch.object(status_models.Resource, "objects") as actual,
            mock.patch.object(status_models.Resource, "insert"),
        ):
            targets.get_one_or_none.return_value = None
            actual.get_all.return_value = []
            result = self.controller.create(
                parent_resource=self.kind,
                uuid=self.uuid,
                node=self.foreign,
                kind="foreign-kind",
                value={},
            )
        assert result.node == self.node
        assert result.kind.kind == self.kind.kind

    def test_create_cannot_shadow_another_nodes_fact(self):
        with (
            mock.patch.object(
                controllers.ua_models.TargetResource, "objects"
            ) as targets,
            mock.patch.object(status_models.Resource, "objects") as actual,
            mock.patch.object(status_models.Resource, "insert") as insert,
        ):
            targets.get_one_or_none.return_value = None
            actual.get_all.return_value = [mock.MagicMock(node=self.foreign)]
            with pytest.raises(controllers.ra_exc.NotFoundError):
                self.controller.create(
                    parent_resource=self.kind, uuid=self.uuid, value={}
                )
        insert.assert_not_called()

    def test_create_requires_the_targets_assigned_agent(self):
        agent_uuid = sys_uuid.uuid4()
        with (
            mock.patch.object(
                controllers.ua_models.TargetResource, "objects"
            ) as targets,
            mock.patch.object(
                controllers.ua_models.UniversalAgent, "objects"
            ) as agents,
            mock.patch.object(status_models.Resource, "insert") as insert,
        ):
            targets.get_one_or_none.return_value = mock.MagicMock(agent=agent_uuid)
            agents.get_one.side_effect = storage_exc.RecordNotFound(
                model=controllers.ua_models.UniversalAgent, filters={}
            )
            with pytest.raises(storage_exc.RecordNotFound):
                self.controller.create(
                    parent_resource=self.kind, uuid=self.uuid, value={}
                )
        filters = agents.get_one.call_args.kwargs["filters"]
        assert filters["uuid"].value == agent_uuid
        assert filters["node"].value == self.node
        insert.assert_not_called()

    def test_update_cannot_transfer_ownership(self):
        resource = mock.MagicMock()
        with mock.patch.object(self.controller, "get", return_value=resource):
            self.controller.update(
                parent_resource=self.kind,
                uuid=self.uuid,
                node=self.foreign,
                kind="foreign-kind",
                value={},
            )
        values = resource.update_dm.call_args.kwargs["values"]
        assert values["node"] == self.node
        assert values["kind"] == self.kind
