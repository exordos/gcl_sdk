from __future__ import annotations

from urllib.parse import urljoin
import uuid as sys_uuid

from gcl_iam.drivers import DummyDriver
from gcl_iam.enforcers import Enforcer
from mock import mock
from oslo_config import cfg
import pytest
import requests
from restalchemy.common import contexts
from restalchemy.common.contexts import Context

from gcl_sdk.agents.universal.dm import models
from gcl_sdk.audit.dm.models import AuditLogSQLStorableMixin
from gcl_sdk.audit.dm.models import AuditRecord
from gcl_sdk.tests.functional import conftest
from gcl_sdk.tests.functional import utils as test_utils

CONF = cfg.CONF


class UniversalAgentAuditMixin(AuditLogSQLStorableMixin, models.UniversalAgent): ...


introspection_info = lambda x: DummyDriver().get_introspection_info(None)  # noqa


class DummyDriverIam(DummyDriver):
    introspection_info = introspection_info
    enforcer = Enforcer(["audit_log.audit_record.read"])


class DummyContext(Context):
    iam_context = DummyDriverIam()
    introspection_info = introspection_info


class TestAuditApi:
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
        contexts.get_context = mock.MagicMock(return_value=DummyContext)
        response = requests.get(url)
        assert response.json() == []
        assert response.status_code == 200

    def test_audit_get(self, audit_api: test_utils.RestServiceTestCase):
        uuid_a = sys_uuid.uuid4()
        agent_a = UniversalAgentAuditMixin(name="Agent A", uuid=uuid_a, node=uuid_a)
        agent_a.insert()
        audit = AuditRecord.objects.get_one(filters={"object_uuid": agent_a.uuid})
        url = urljoin(audit_api.base_url, f"audit/{audit.uuid}")
        contexts.get_context = mock.MagicMock(return_value=DummyContext)
        response = requests.get(url)
        output = response.json()
        expected = {
            "action": "create",
            "object_type": "ua_agents",
            "object_uuid": str(agent_a.uuid),
            "uuid": str(audit.uuid),
        }
        assert response.status_code == 200
        for key in expected.keys():
            assert output[key] == expected[key]

    def test_audit_list(self, audit_api: test_utils.RestServiceTestCase):
        uuid_a = sys_uuid.uuid4()
        agent_a = UniversalAgentAuditMixin(name="Agent A", uuid=uuid_a, node=uuid_a)
        uuid_b = sys_uuid.uuid4()
        agent_b = UniversalAgentAuditMixin(name="Agent B", uuid=uuid_b, node=uuid_b)
        agent_a.insert()
        agent_b.insert()
        audits = AuditRecord.objects.get_all()
        url = urljoin(audit_api.base_url, "audit/")
        contexts.get_context = mock.MagicMock(return_value=DummyContext)
        response = requests.get(url)
        output = response.json()
        assert response.status_code == 200
        assert len(output) == 2
        assert {a["uuid"] for a in output} == {
            str(audits[0].uuid),
            str(audits[1].uuid),
        }
