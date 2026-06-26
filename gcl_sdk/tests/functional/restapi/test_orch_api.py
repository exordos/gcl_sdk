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

from oslo_config import cfg
import pytest
import requests

from gcl_sdk.agents.universal.dm import models
from gcl_sdk.tests.functional import conftest
from gcl_sdk.tests.functional import utils as test_utils

CONF = cfg.CONF


class TestUAOrchApi:
    @pytest.fixture(scope="class")
    def orch_api_service(self, orch_api_wsgi_app):
        class ApiRestService(test_utils.RestServiceTestCase):
            __FIRST_MIGRATION__ = conftest.FIRST_MIGRATION
            __APP__ = orch_api_wsgi_app

        rest_service = ApiRestService()
        rest_service.setup_class()

        yield rest_service

        rest_service.teardown_class()

    @pytest.fixture()
    def orch_api(self, orch_api_service: test_utils.RestServiceTestCase):
        orch_api_service.setup_method()

        yield orch_api_service

        orch_api_service.teardown_method()

    def test_agent_list_not_allowed(
        self,
        orch_api: test_utils.RestServiceTestCase,
    ):
        url = urljoin(orch_api.base_url, "agents/")

        response = requests.get(url)

        assert response.status_code == 405

    def test_agent_register_not_allowed(
        self,
        orch_api: test_utils.RestServiceTestCase,
    ):
        uuid = sys_uuid.uuid4()
        agent = models.UniversalAgent(
            name="Agent A",
            uuid=uuid,
            node=uuid,
            capabilities={"capabilities": ["foo"]},
            facts={"facts": ["bar"]},
        )

        view = agent.dump_to_simple_view()

        url = urljoin(orch_api.base_url, "agents/")

        response = requests.post(url, json=view)

        assert response.status_code == 405

    def test_agent_get_payload_no_payload(
        self,
        orch_api: test_utils.RestServiceTestCase,
    ):
        uuid = sys_uuid.uuid4()
        agent = models.UniversalAgent(
            name="Agent A",
            uuid=uuid,
            node=uuid,
            capabilities={"capabilities": ["foo"]},
            facts={"facts": ["bar"]},
        )
        agent.insert()

        url = urljoin(
            orch_api.base_url,
            f"agents/{agent.uuid}/actions/get_payload?version=0&hash=",
        )

        response = requests.get(url)
        output = response.json()

        assert response.status_code == 200
        assert output["capabilities"] == {"foo": {"resources": []}}
        assert output["facts"] == {
            "bar": {"resources": []},
            "foo": {"resources": []},
        }
        assert output["hash"] != ""

    def test_agent_get_payload(
        self,
        orch_api: test_utils.RestServiceTestCase,
    ):
        uuid = sys_uuid.uuid4()
        agent = models.UniversalAgent(
            name="Agent A",
            uuid=uuid,
            node=uuid,
            capabilities={"capabilities": ["foo"]},
            facts={"facts": ["bar"]},
        )
        agent.insert()

        uuid_r = sys_uuid.uuid4()
        resource = conftest.FooTargetResource(
            uuid=uuid_r,
            name="foo-name",
            project_id=uuid_r,
        ).to_ua_resource(kind="foo")
        resource.agent = agent.uuid
        resource.insert()

        url = urljoin(
            orch_api.base_url,
            f"agents/{agent.uuid}/actions/get_payload?version=0&hash=",
        )

        response = requests.get(url)
        output = response.json()

        assert response.status_code == 200

        view = resource.dump_to_simple_view()
        view.pop("agent", None)
        view.pop("master", None)
        view.pop("node", None)
        view.pop("tracked_at", None)
        view.pop("master_hash", None)
        view.pop("master_full_hash", None)
        assert output["capabilities"]["foo"]["resources"] == [view]
        assert output["facts"] == {
            "bar": {"resources": []},
            "foo": {"resources": []},
        }
        assert output["hash"] != ""

    def test_agent_get_payload_same_hash(
        self,
        orch_api: test_utils.RestServiceTestCase,
    ):
        uuid = sys_uuid.uuid4()
        agent = models.UniversalAgent(
            name="Agent A",
            uuid=uuid,
            node=uuid,
            capabilities={"capabilities": ["foo"]},
            facts={"facts": ["bar"]},
        )
        agent.insert()

        uuid_r = sys_uuid.uuid4()
        resource = conftest.FooTargetResource(
            uuid=uuid_r,
            name="foo-name",
            project_id=uuid_r,
        ).to_ua_resource(kind="foo")
        resource.agent = agent.uuid
        resource.insert()

        url = urljoin(
            orch_api.base_url,
            f"agents/{agent.uuid}/actions/get_payload?version=0&hash=",
        )

        response = requests.get(url)
        output = response.json()

        assert response.status_code == 200

        current_hash = output["hash"]

        url = urljoin(
            orch_api.base_url,
            (f"agents/{agent.uuid}/actions/get_payload?version=0&hash={current_hash}"),
        )

        response = requests.get(url)
        output = response.json()

        assert response.status_code == 200

        assert output["capabilities"] == {}
        assert output["facts"] == {}
        assert output["hash"] == current_hash
