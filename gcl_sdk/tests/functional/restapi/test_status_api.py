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

import pytest
import requests
from oslo_config import cfg

from gcl_sdk.agents.universal.dm import models
from gcl_sdk.tests.functional import utils as test_utils
from gcl_sdk.tests.functional import conftest

CONF = cfg.CONF


class TestUAStatusApi:
    @pytest.fixture(scope="class")
    def status_api_service(self, status_api_wsgi_app):
        class ApiRestService(test_utils.RestServiceTestCase):
            __FIRST_MIGRATION__ = conftest.FIRST_MIGRATION
            __APP__ = status_api_wsgi_app

        rest_service = ApiRestService()
        rest_service.setup_class()

        yield rest_service

        rest_service.teardown_class()

    @pytest.fixture()
    def status_api(self, status_api_service: test_utils.RestServiceTestCase):
        status_api_service.setup_method()

        yield status_api_service

        status_api_service.teardown_method()

    def test_agent_list_not_allowed(
        self,
        status_api: test_utils.RestServiceTestCase,
    ):
        url = urljoin(status_api.base_url, "agents/")

        response = requests.get(url)

        assert response.status_code == 405

    def test_agent_register(
        self,
        status_api: test_utils.RestServiceTestCase,
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

        url = urljoin(status_api.base_url, "agents/")

        response = requests.post(url, json=view)
        output = response.json()

        assert response.status_code == 201
        assert output["uuid"] == str(uuid)

    def test_resources_no_kind(
        self,
        status_api: test_utils.RestServiceTestCase,
    ):
        url = urljoin(status_api.base_url, "kind/")

        response = requests.get(url)
        output = response.json()

        assert response.status_code == 200
        assert output == []

    def test_resources_kind_list_not_allowed(
        self,
        status_api: test_utils.RestServiceTestCase,
    ):
        url = urljoin(status_api.base_url, "kind/foo/resources/")

        response = requests.get(url)

        assert response.status_code == 405

    def test_resources_add_resource(
        self,
        status_api: test_utils.RestServiceTestCase,
    ):
        uuid = sys_uuid.uuid4()
        resource = conftest.FooResource(
            uuid=uuid,
            name="foo-name",
            project_id=uuid,
        ).to_ua_resource(kind="/v1/kind/foo")
        view = resource.dump_to_simple_view()

        url = urljoin(status_api.base_url, "kind/foo/resources/")

        response = requests.post(url, json=view)
        output = response.json()

        assert response.status_code == 201
        assert output["uuid"] == str(uuid)

    def test_resources_update_resource(
        self,
        status_api: test_utils.RestServiceTestCase,
    ):
        uuid = sys_uuid.uuid4()
        resource = conftest.FooResource(
            uuid=uuid,
            name="foo-name",
            project_id=uuid,
        ).to_ua_resource(kind="/v1/kind/foo")
        view = resource.dump_to_simple_view()

        url = urljoin(status_api.base_url, "kind/foo/resources/")

        response = requests.post(url, json=view)
        output = response.json()

        assert response.status_code == 201
        assert output["uuid"] == str(uuid)

        view["value"]["name"] = "foo-name-updated"
        view.pop("uuid")
        view.pop("res_uuid")
        view.pop("created_at")
        view.pop("updated_at")

        url = urljoin(status_api.base_url, f"kind/foo/resources/{uuid}")

        response = requests.put(url, json=view)
        output = response.json()

        assert response.status_code == 200
        assert output["value"]["name"] == "foo-name-updated"

    def test_resources_delete_resource(
        self,
        status_api: test_utils.RestServiceTestCase,
    ):
        uuid = sys_uuid.uuid4()
        resource = conftest.FooResource(
            uuid=uuid,
            name="foo-name",
            project_id=uuid,
        ).to_ua_resource(kind="/v1/kind/foo")
        view = resource.dump_to_simple_view()

        url = urljoin(status_api.base_url, "kind/foo/resources/")

        response = requests.post(url, json=view)
        output = response.json()

        assert response.status_code == 201
        assert output["uuid"] == str(uuid)

        url = urljoin(status_api.base_url, f"kind/foo/resources/{uuid}")

        response = requests.delete(url)

        assert response.status_code == 204

    def test_resources_add_resource_dynamic_kind(
        self,
        status_api: test_utils.RestServiceTestCase,
    ):
        uuid = sys_uuid.uuid4()
        resource = conftest.FooResource(
            uuid=uuid,
            name="foo-name",
            project_id=uuid,
        ).to_ua_resource(kind="/v1/kind/foo")
        view = resource.dump_to_simple_view()

        url = urljoin(status_api.base_url, "kind/foo/resources/")

        response = requests.post(url, json=view)
        assert response.status_code == 201

        url = urljoin(status_api.base_url, "kind/")

        response = requests.get(url)
        output = response.json()

        assert response.status_code == 200
        assert output == ["foo"]

    def test_node_verifier_verify_exists_true(
        self,
        status_api: test_utils.RestServiceTestCase,
    ):
        """Test verify returns valid=True when node encryption key exists."""
        node_uuid = sys_uuid.uuid4()

        # Create NodeEncryptionKey
        node_key = models.NodeEncryptionKey(
            uuid=node_uuid,
            private_key="test-key",
        )
        node_key.save()

        url = urljoin(status_api.base_url, f"node_verifiers/{node_uuid}/actions/verify")
        response = requests.get(url)
        output = response.json()

        assert response.status_code == 200
        assert output == {"valid": True}

    def test_node_verifier_verify_not_found(
        self,
        status_api: test_utils.RestServiceTestCase,
    ):
        """Test verify returns 404 when node encryption key doesn't exist."""
        node_uuid = sys_uuid.uuid4()

        url = urljoin(status_api.base_url, f"node_verifiers/{node_uuid}/actions/verify")
        response = requests.get(url)

        # The framework returns 404 when the resource is not found
        assert response.status_code == 404
