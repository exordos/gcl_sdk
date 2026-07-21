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

from urllib.parse import urljoin
import uuid as sys_uuid

from bazooka import exceptions as bzk_exceptions
from gcl_iam import exceptions as iam_exceptions
from gcl_iam.drivers import DummyDriver
from gcl_iam.enforcers import Enforcer
from mock import mock
from oslo_config import cfg
import pytest
import requests
from restalchemy.common import contexts

from gcl_sdk.agents.universal.dm import models
from gcl_sdk.audit.api import controllers as audit_controllers
from gcl_sdk.audit.dm.models import AuditDeliveryEvent
from gcl_sdk.audit.dm.models import AuditLogSQLStorableMixin
from gcl_sdk.audit.services import senders as audit_senders
from gcl_sdk.tests.functional import conftest
from gcl_sdk.tests.functional import utils as test_utils

CONF = cfg.CONF


PROJECT_ID = sys_uuid.UUID("00000000-0000-0000-0000-000000000001")
PROJECT_ID_2 = sys_uuid.UUID("00000000-0000-0000-0000-000000000002")


class UniversalAgentAuditMixin(AuditLogSQLStorableMixin, models.UniversalAgent):
    __audit_resource_type__ = "universal_agent"
    __audit_service_name__ = "gcl_sdk"


class DummyIamContext:
    def __init__(self, permissions, project_id):
        self.enforcer = Enforcer(permissions)
        self._introspection_info = DummyDriver().get_introspection_info(None)
        self._introspection_info["project_id"] = str(project_id) if project_id else None

    def introspection_info(self):
        return self._introspection_info


class DummyContext:
    def __init__(self, permissions=("audit.events.read",), project_id=PROJECT_ID):
        self.iam_context = DummyIamContext(permissions, project_id)
        self.project_id = project_id


class TestAuditApi:
    @staticmethod
    def _set_context(
        permissions=("audit.events.read",),
        project_id=PROJECT_ID,
    ):
        context = DummyContext(permissions=permissions, project_id=project_id)
        contexts.get_context = mock.MagicMock(return_value=context)
        return context

    @pytest.fixture(scope="class")
    def audit_api_service(self, audit_api_wsgi_app):
        class ApiRestService(test_utils.RestServiceTestCase):
            __FIRST_MIGRATION__ = conftest.FIRST_MIGRATION
            __APP__ = audit_api_wsgi_app

        rest_service = ApiRestService()
        rest_service.setup_class()
        yield rest_service
        rest_service.teardown_class()

    @pytest.fixture()
    def audit_api(self, audit_api_service: test_utils.RestServiceTestCase):
        audit_api_service.setup_method()
        yield audit_api_service
        audit_api_service.teardown_method()

    def test_no_audit(self, audit_api: test_utils.RestServiceTestCase):
        url = urljoin(audit_api.base_url, "audit/")
        self._set_context()
        response = requests.get(url)
        assert response.json() == []
        assert response.status_code == 200

    def test_audit_get(self, audit_api: test_utils.RestServiceTestCase):
        self._set_context()
        uuid_a = sys_uuid.uuid4()
        agent_a = UniversalAgentAuditMixin(name="Agent A", uuid=uuid_a, node=uuid_a)
        agent_a.insert()
        audit = AuditDeliveryEvent.objects.get_one(
            filters={"resource_uuid": agent_a.uuid}
        )
        url = urljoin(audit_api.base_url, f"audit/{audit.uuid}")
        response = requests.get(url)
        output = response.json()
        expected = {
            "action": "create",
            "service_name": "gcl_sdk",
            "resource_type": "universal_agent",
            "resource_uuid": str(agent_a.uuid),
            "project_id": str(PROJECT_ID),
            "uuid": str(audit.uuid),
        }
        assert response.status_code == 200
        for key in expected.keys():
            assert output[key] == expected[key]
        assert output["snapshot"]["uuid"] == str(agent_a.uuid)

    def test_audit_get_without_project(self, audit_api: test_utils.RestServiceTestCase):
        self._set_context(project_id=None)
        uuid_a = sys_uuid.uuid4()
        agent_a = UniversalAgentAuditMixin(name="Agent A", uuid=uuid_a, node=uuid_a)
        agent_a.insert()
        audit = AuditDeliveryEvent.objects.get_one(
            filters={"resource_uuid": agent_a.uuid}
        )
        assert audit.project_id is None

    def test_actor_without_iam_context_is_none(self):
        contexts.get_context = mock.MagicMock(return_value=object())
        agent = UniversalAgentAuditMixin(
            name="Agent without IAM",
            uuid=sys_uuid.uuid4(),
            node=sys_uuid.uuid4(),
        )

        assert agent.get_audit_actor_user_uuid() is None

    def test_audit_list(self, audit_api: test_utils.RestServiceTestCase):
        self._set_context()
        uuid_a = sys_uuid.uuid4()
        agent_a = UniversalAgentAuditMixin(name="Agent A", uuid=uuid_a, node=uuid_a)
        uuid_b = sys_uuid.uuid4()
        agent_b = UniversalAgentAuditMixin(name="Agent B", uuid=uuid_b, node=uuid_b)
        agent_a.insert()
        agent_b.insert()
        audits = AuditDeliveryEvent.objects.get_all()
        url = urljoin(audit_api.base_url, "audit/")
        response = requests.get(url)
        output = response.json()
        assert response.status_code == 200
        assert len(output) == 2
        assert {a["uuid"] for a in output} == {
            str(audits[0].uuid),
            str(audits[1].uuid),
        }

    def test_audit_list_is_project_scoped(
        self, audit_api: test_utils.RestServiceTestCase
    ):
        self._set_context(project_id=PROJECT_ID)
        uuid_a = sys_uuid.uuid4()
        UniversalAgentAuditMixin(name="Agent A", uuid=uuid_a, node=uuid_a).insert()

        self._set_context(project_id=PROJECT_ID_2)
        uuid_b = sys_uuid.uuid4()
        UniversalAgentAuditMixin(name="Agent B", uuid=uuid_b, node=uuid_b).insert()

        self._set_context(project_id=PROJECT_ID)
        response = requests.get(urljoin(audit_api.base_url, "audit/"))

        assert response.status_code == 200
        assert {event["resource_uuid"] for event in response.json()} == {str(uuid_a)}

    def test_audit_read_without_project_forbidden(
        self, audit_api: test_utils.RestServiceTestCase
    ):
        self._set_context(project_id=None)
        controller = audit_controllers.AuditController(request=mock.Mock())

        with pytest.raises(iam_exceptions.Forbidden):
            controller.filter(filters={})

    def test_audit_list_all(self, audit_api: test_utils.RestServiceTestCase):
        self._set_context(project_id=PROJECT_ID)
        uuid_a = sys_uuid.uuid4()
        UniversalAgentAuditMixin(name="Agent A", uuid=uuid_a, node=uuid_a).insert()

        self._set_context(project_id=None)
        uuid_b = sys_uuid.uuid4()
        UniversalAgentAuditMixin(name="Agent B", uuid=uuid_b, node=uuid_b).insert()

        self._set_context(
            permissions=("audit.events.read_all",),
            project_id=None,
        )
        response = requests.get(urljoin(audit_api.base_url, "audit/"))

        assert response.status_code == 200
        assert {event["resource_uuid"] for event in response.json()} == {
            str(uuid_a),
            str(uuid_b),
        }

    def test_audit_list_without_permission_forbidden(
        self, audit_api: test_utils.RestServiceTestCase
    ):
        self._set_context(permissions=tuple(), project_id=PROJECT_ID)
        controller = audit_controllers.AuditController(request=mock.Mock())

        with pytest.raises(iam_exceptions.PolicyNotAuthorized):
            controller.filter(filters={})

    def test_delivery_service_removes_persisted_event_after_acknowledgement(
        self, audit_api: test_utils.RestServiceTestCase
    ):
        self._set_context()
        resource_uuid = sys_uuid.uuid4()
        UniversalAgentAuditMixin(
            name="Agent delivery",
            uuid=resource_uuid,
            node=resource_uuid,
        ).insert()
        event = AuditDeliveryEvent.objects.get_one(
            filters={"resource_uuid": resource_uuid}
        )
        assert event.status == AuditDeliveryEvent.STATUS.NEW.value

        audit_client = mock.Mock()
        service = audit_senders.AuditSenderService(
            audit_client=audit_client,
            batch_size=1,
        )
        service._iteration()

        delivered = AuditDeliveryEvent.objects.get_one_or_none(
            filters={"uuid": event.uuid}
        )
        assert delivered is None
        audit_client.send_event.assert_called_once()
        assert audit_client.send_event.call_args.args[0].uuid == event.uuid

    def test_delivery_order_has_matching_database_index(
        self, audit_api: test_utils.RestServiceTestCase
    ):
        with audit_api.engine.session_manager() as session:
            rows = session.execute(
                """
                SELECT indexdef
                FROM pg_indexes
                WHERE tablename = 'gcl_sdk_audit_events'
                  AND indexname = 'gcl_sdk_audit_events_created_uuid_idx';
                """
            ).fetchall()

        assert len(rows) == 1
        assert "(created_at, uuid)" in rows[0]["indexdef"]

    def test_delivery_conflict_preserves_immutable_event_timestamps(
        self, audit_api: test_utils.RestServiceTestCase
    ):
        self._set_context()
        resource_uuid = sys_uuid.uuid4()
        UniversalAgentAuditMixin(
            name="Agent conflict",
            uuid=resource_uuid,
            node=resource_uuid,
        ).insert()
        event = AuditDeliveryEvent.objects.get_one(
            filters={"resource_uuid": resource_uuid}
        )
        original_updated_at = event.updated_at

        cause = mock.Mock()
        cause.response.status_code = 409
        audit_client = mock.Mock()
        audit_client.send_event.side_effect = bzk_exceptions.ConflictError(cause)
        service = audit_senders.AuditSenderService(
            audit_client=audit_client,
            batch_size=1,
        )
        service._iteration()

        persisted = AuditDeliveryEvent.objects.get_one(filters={"uuid": event.uuid})
        assert persisted.status == AuditDeliveryEvent.STATUS.ERROR.value
        assert persisted.created_at == event.created_at
        assert persisted.updated_at == original_updated_at
