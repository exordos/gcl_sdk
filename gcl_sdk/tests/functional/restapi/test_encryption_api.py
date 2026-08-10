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

import base64
import datetime
from urllib import parse as urllib_parse
import uuid as sys_uuid

import pytest
import requests
from restalchemy.api import applications
from restalchemy.api import controllers
from restalchemy.api import middlewares
from restalchemy.api import routes
from restalchemy.api.middlewares import errors as errors_mw
from restalchemy.api.middlewares import logging as logging_mw

from gcl_sdk.agents.universal.api import crypto as sdk_crypto
from gcl_sdk.agents.universal.api import middlewares as sdk_middlewares
from gcl_sdk.agents.universal.api import packers
from gcl_sdk.agents.universal.dm import models
from gcl_sdk.agents.universal.orch_api import routes as orch_routes
from gcl_sdk.agents.universal.status_api import routes as status_routes
from gcl_sdk.tests.functional import conftest
from gcl_sdk.tests.functional import utils as test_utils


@pytest.fixture()
def encryption_key_factory():
    def _factory(disabled_until: datetime.datetime) -> sys_uuid.UUID:
        node_uuid = sys_uuid.uuid4()
        models.NodeEncryptionKey(
            uuid=node_uuid,
            encryption_disabled_until=disabled_until,
            private_key=sdk_crypto.generate_key_base64()[1],
        ).insert()
        return node_uuid

    return _factory


def build_encryption_headers(node_uuid: sys_uuid.UUID, content_type: str):
    nonce = sdk_crypto.generate_nonce()
    return {
        packers.EXORDOS_NODE_UUID_HEADER: str(node_uuid),
        packers.EXORDOS_NONCE_HEADER: base64.b64encode(nonce).decode(),
        "Accept": "application/json",
        "Content-Type": content_type,
    }


@pytest.fixture(scope="module")
def orch_api_encrypted_wsgi_app():
    class OrchApiApp(routes.RootRoute):
        pass

    class ApiEndpointController(controllers.RoutesListController):
        __TARGET_PATH__ = "/v1/"

    class ApiEndpointRoute(routes.Route):
        __controller__ = ApiEndpointController
        __allow_methods__ = [routes.FILTER]

        agents = routes.route(orch_routes.UniversalAgentsRoute)

    OrchApiApp.v1 = routes.route(ApiEndpointRoute)

    return middlewares.attach_middlewares(
        applications.OpenApiApplication(
            route_class=OrchApiApp,
            openapi_engine=conftest.get_openapi_engine(),
        ),
        [
            sdk_middlewares.SdkContextMiddleware,
            errors_mw.ErrorsHandlerMiddleware,
            logging_mw.LoggingMiddleware,
        ],
    )


@pytest.fixture(scope="module")
def status_api_encrypted_wsgi_app():
    class StatusApiApp(routes.RootRoute):
        pass

    class ApiEndpointController(controllers.RoutesListController):
        __TARGET_PATH__ = "/v1/"

    class ApiEndpointRoute(routes.Route):
        __controller__ = ApiEndpointController
        __allow_methods__ = [routes.FILTER]

        agents = routes.route(status_routes.UniversalAgentsRoute)
        kind = routes.route(status_routes.KindRoute)

    StatusApiApp.v1 = routes.route(ApiEndpointRoute)

    return middlewares.attach_middlewares(
        applications.OpenApiApplication(
            route_class=StatusApiApp,
            openapi_engine=conftest.get_openapi_engine(),
        ),
        [
            sdk_middlewares.SdkContextMiddleware,
            errors_mw.ErrorsHandlerMiddleware,
            logging_mw.LoggingMiddleware,
        ],
    )


class TestEncryptedOrchApi:
    @pytest.fixture(scope="class")
    def orch_api_service(self, orch_api_encrypted_wsgi_app):
        class ApiRestService(test_utils.RestServiceTestCase):
            __FIRST_MIGRATION__ = conftest.FIRST_MIGRATION
            __APP__ = orch_api_encrypted_wsgi_app

        rest_service = ApiRestService()
        rest_service.setup_class()

        yield rest_service

        rest_service.teardown_class()

    @pytest.fixture()
    def orch_api(self, orch_api_service: test_utils.RestServiceTestCase):
        orch_api_service.setup_method()

        yield orch_api_service

        orch_api_service.teardown_method()

    def test_encryption_disabled_allows_json(
        self,
        orch_api: test_utils.RestServiceTestCase,
        encryption_key_factory,
    ):
        disabled_until = datetime.datetime.now(
            datetime.timezone.utc
        ) + datetime.timedelta(hours=1)
        node_uuid = encryption_key_factory(disabled_until)

        agent_uuid = sys_uuid.uuid4()
        agent = models.UniversalAgent(
            name="Test Agent",
            uuid=agent_uuid,
            node=node_uuid,
        )
        agent.insert()

        headers = build_encryption_headers(node_uuid, "application/json")

        url = urllib_parse.urljoin(orch_api.base_url, f"agents/{agent_uuid}")

        response = requests.get(url, headers=headers)

        assert response.status_code == 200

    def test_encryption_required_rejects_plain_json(
        self,
        orch_api: test_utils.RestServiceTestCase,
        encryption_key_factory,
    ):
        disabled_until = datetime.datetime.now(
            datetime.timezone.utc
        ) - datetime.timedelta(hours=1)
        node_uuid = encryption_key_factory(disabled_until)
        headers = build_encryption_headers(node_uuid, "application/json")

        url = urllib_parse.urljoin(orch_api.base_url, "agents/")

        response = requests.get(url, headers=headers)

        assert response.status_code == 400

    def test_encryption_required_allows_encrypted_content_type(
        self,
        orch_api: test_utils.RestServiceTestCase,
        encryption_key_factory,
    ):
        disabled_until = datetime.datetime.now(
            datetime.timezone.utc
        ) - datetime.timedelta(hours=1)
        node_uuid = encryption_key_factory(disabled_until)
        headers = build_encryption_headers(
            node_uuid,
            packers.ENCRYPTED_JSON_CONTENT_TYPE,
        )

        agent_uuid = sys_uuid.uuid4()
        agent = models.UniversalAgent(
            name="Test Agent",
            uuid=agent_uuid,
            node=node_uuid,
        )
        agent.insert()

        url = urllib_parse.urljoin(orch_api.base_url, f"agents/{agent_uuid}")

        response = requests.get(url, headers=headers)

        assert response.status_code == 200


class TestEncryptedStatusApi:
    @pytest.fixture(scope="class")
    def status_api_service(self, status_api_encrypted_wsgi_app):
        class ApiRestService(test_utils.RestServiceTestCase):
            __FIRST_MIGRATION__ = conftest.FIRST_MIGRATION
            __APP__ = status_api_encrypted_wsgi_app

        rest_service = ApiRestService()
        rest_service.setup_class()

        yield rest_service

        rest_service.teardown_class()

    @pytest.fixture()
    def status_api(self, status_api_service: test_utils.RestServiceTestCase):
        status_api_service.setup_method()

        yield status_api_service

        status_api_service.teardown_method()

    def test_encryption_disabled_allows_json(
        self,
        status_api: test_utils.RestServiceTestCase,
        encryption_key_factory,
    ):
        disabled_until = datetime.datetime.now(
            datetime.timezone.utc
        ) + datetime.timedelta(hours=1)
        node_uuid = encryption_key_factory(disabled_until)
        headers = build_encryption_headers(node_uuid, "application/json")

        agent_uuid = sys_uuid.uuid4()
        agent = models.UniversalAgent(
            name="Test Agent",
            uuid=agent_uuid,
            node=node_uuid,
        )
        agent.insert()

        url = urllib_parse.urljoin(status_api.base_url, f"agents/{agent_uuid}")

        response = requests.get(url, headers=headers)

        assert response.status_code == 200

    def test_encryption_required_rejects_plain_json(
        self,
        status_api: test_utils.RestServiceTestCase,
        encryption_key_factory,
    ):
        disabled_until = datetime.datetime.now(
            datetime.timezone.utc
        ) - datetime.timedelta(hours=1)
        node_uuid = encryption_key_factory(disabled_until)
        headers = build_encryption_headers(node_uuid, "application/json")

        url = urllib_parse.urljoin(status_api.base_url, "agents/")

        response = requests.get(url, headers=headers)

        assert response.status_code == 400

    def test_encryption_required_allows_encrypted_content_type(
        self,
        status_api: test_utils.RestServiceTestCase,
        encryption_key_factory,
    ):
        disabled_until = datetime.datetime.now(
            datetime.timezone.utc
        ) - datetime.timedelta(hours=1)
        node_uuid = encryption_key_factory(disabled_until)
        headers = build_encryption_headers(
            node_uuid,
            packers.ENCRYPTED_JSON_CONTENT_TYPE,
        )

        agent_uuid = sys_uuid.uuid4()
        agent = models.UniversalAgent(
            name="Test Agent",
            uuid=agent_uuid,
            node=node_uuid,
        )
        agent.insert()

        url = urllib_parse.urljoin(status_api.base_url, f"agents/{agent_uuid}")

        response = requests.get(url, headers=headers)

        assert response.status_code == 200
        assert response.headers["Content-Type"] == (packers.ENCRYPTED_JSON_CONTENT_TYPE)
        assert packers.EXORDOS_NONCE_HEADER in response.headers
        assert packers.EXORDOS_NODE_UUID_HEADER in response.headers
