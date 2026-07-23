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
import uuid as sys_uuid

import pytest

from gcl_sdk.agents.universal.dm import models as ua_models
from gcl_sdk.tests.functional import conftest
from gcl_sdk.tests.functional import utils as test_utils


class TestNodeEncryptionKeyGetOrCreate:
    # Need only to apply DB migrations
    @pytest.fixture(scope="class", autouse=True)
    def api_service(self, orch_api_wsgi_app):
        class ApiRestService(test_utils.RestServiceTestCase):
            __FIRST_MIGRATION__ = conftest.FIRST_MIGRATION
            __APP__ = orch_api_wsgi_app

        rest_service = ApiRestService()
        rest_service.setup_class()

        yield rest_service

        rest_service.teardown_class()

    @pytest.fixture(autouse=True)
    def db_migrations(self, api_service: test_utils.RestServiceTestCase):
        api_service.setup_method()
        yield api_service
        api_service.teardown_method()

    def test_creates_a_key_when_missing(self):
        node = sys_uuid.uuid4()

        key = ua_models.NodeEncryptionKey.get_or_create(node)

        assert key.uuid == node
        assert key.private_key

    def test_creates_a_key_with_the_given_private_key(self):
        node = sys_uuid.uuid4()

        key = ua_models.NodeEncryptionKey.get_or_create(node, private_key="c2VjcmV0")

        assert key.private_key == "c2VjcmV0"

    def test_returns_the_existing_key_when_none_explicitly_given(self):
        node = sys_uuid.uuid4()
        first = ua_models.NodeEncryptionKey.get_or_create(node)

        second = ua_models.NodeEncryptionKey.get_or_create(node)

        assert second.private_key == first.private_key

    def test_syncs_the_existing_key_to_match_the_given_one(self):
        # A caller that already generated and deployed a specific key
        # (e.g. a trusted bootstrap flow) must end up in sync with the
        # DB - an existing key from unrelated prior provisioning would
        # otherwise silently win, leaving the agent unable to
        # authenticate with the key it was actually given.
        node = sys_uuid.uuid4()
        ua_models.NodeEncryptionKey.get_or_create(node)

        key = ua_models.NodeEncryptionKey.get_or_create(
            node, private_key="ZnJlc2gta2V5"
        )

        assert key.private_key == "ZnJlc2gta2V5"
