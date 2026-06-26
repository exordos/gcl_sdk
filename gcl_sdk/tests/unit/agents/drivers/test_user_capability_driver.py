#    Copyright 2025-2026 Genesis Corporation.
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

from unittest.mock import MagicMock
from unittest.mock import patch
import uuid as sys_uuid

from bazooka import exceptions as bazooka_exc
import pytest

from gcl_sdk.agents.universal.clients.backend import core as core_back
from gcl_sdk.agents.universal.clients.backend import exceptions as client_exc
from gcl_sdk.agents.universal.dm import models

_CORE_MODELS = "gcl_sdk.agents.universal.clients.backend.core.models.Resource"


USER_KIND = "em_core_iam_users"
USERS_COLLECTION = core_back.GCUsersRestApiBackendClient.USERS_COLLECTION


def _make_resource(
    uuid: sys_uuid.UUID | None = None,
    value: dict | None = None,
) -> models.Resource:
    uuid = uuid or sys_uuid.uuid4()
    value = value or {
        "uuid": str(uuid),
        "name": "test-user",
        "password": "s3cr3t",
        "status": "ACTIVE",
    }
    return models.Resource.from_value(
        value, USER_KIND, target_fields=frozenset(value.keys())
    )


def _make_client(
    tf_storage: MagicMock | None = None,
) -> core_back.GCUsersRestApiBackendClient:
    http_client = MagicMock()
    return core_back.GCUsersRestApiBackendClient(
        http_client=http_client,
        user_kind=USER_KIND,
        tf_storage=tf_storage,
    )


def _make_db_resource(uuid: sys_uuid.UUID, password: str) -> models.Resource:
    """Return a Resource that simulates DB-stored record with password."""
    value = {"uuid": str(uuid), "name": "test-user", "password": password}
    return models.Resource.from_value(
        value, USER_KIND, target_fields=frozenset(value.keys())
    )


class TestGCUsersRestApiBackendClientInit:
    def test_collection_map_contains_only_users_collection(self):
        client = _make_client()

        assert client._collection_map == {USER_KIND: USERS_COLLECTION}

    def test_custom_user_kind_is_stored(self):
        http_client = MagicMock()
        client = core_back.GCUsersRestApiBackendClient(
            http_client=http_client, user_kind="custom_kind"
        )
        assert client._user_kind == "custom_kind"
        assert "custom_kind" in client._collection_map

    def test_capabilities_returns_user_kind(self):
        from gcl_sdk.agents.universal.drivers import core as drv_core

        with (
            patch("gcl_sdk.agents.universal.drivers.core.bazooka.Client"),
            patch("gcl_sdk.agents.universal.drivers.core.base.CoreIamAuthenticator"),
            patch("gcl_sdk.agents.universal.drivers.core.base.CollectionBaseClient"),
            patch("gcl_sdk.agents.universal.storage.fs.TargetFieldsFileStorage"),
        ):
            driver = drv_core.UserCapabilityDriver(
                username="admin",
                password="pass",
                user_api_base_url="http://localhost",
                user_kind="my_users",
                agent_work_dir="/tmp",
            )
        assert driver.get_capabilities() == ["my_users"]

    def test_default_user_kind(self):
        from gcl_sdk.agents.universal.drivers import core as drv_core

        with (
            patch("gcl_sdk.agents.universal.drivers.core.bazooka.Client"),
            patch("gcl_sdk.agents.universal.drivers.core.base.CoreIamAuthenticator"),
            patch("gcl_sdk.agents.universal.drivers.core.base.CollectionBaseClient"),
            patch("gcl_sdk.agents.universal.storage.fs.TargetFieldsFileStorage"),
        ):
            driver = drv_core.UserCapabilityDriver(
                username="admin",
                password="pass",
                user_api_base_url="http://localhost",
                agent_work_dir="/tmp",
            )
        assert driver.get_capabilities() == ["em_core_iam_users"]


class TestGCUsersRestApiBackendClientGetFilters:
    def test_returns_empty_when_no_tf_storage(self):
        client = _make_client(tf_storage=None)
        assert client._get_filters(USER_KIND) == {}

    def test_returns_empty_when_kind_not_in_storage(self):
        tf_storage = MagicMock()
        tf_storage.storage.return_value = {}
        client = _make_client(tf_storage=tf_storage)
        assert client._get_filters(USER_KIND) == {}

    def test_returns_empty_when_kind_has_no_entries(self):
        tf_storage = MagicMock()
        tf_storage.storage.return_value = {USER_KIND: {}}
        client = _make_client(tf_storage=tf_storage)
        assert client._get_filters(USER_KIND) == {}

    def test_returns_uuid_filter_from_storage(self):
        uuid1, uuid2 = sys_uuid.uuid4(), sys_uuid.uuid4()
        tf_storage = MagicMock()
        tf_storage.storage.return_value = {
            USER_KIND: {uuid1: MagicMock(), uuid2: MagicMock()}
        }
        client = _make_client(tf_storage=tf_storage)
        filters = client._get_filters(USER_KIND)

        assert "uuid" in filters
        assert set(filters["uuid"]) == {str(uuid1), str(uuid2)}
        assert "project_id" not in filters


class TestGCUsersRestApiBackendClientEnrichUsers:
    def test_enriches_users_with_password_from_db(self):
        uuid = sys_uuid.uuid4()
        db_res = _make_db_resource(uuid, "s3cr3t")

        client = _make_client()
        users = [{"uuid": str(uuid), "name": "alice"}]

        with patch(_CORE_MODELS) as mr:
            mr.objects.get_all.return_value = [db_res]
            enriched = client._enrich_users(users)

        assert enriched[0]["password"] == "s3cr3t"

    def test_skips_user_when_db_resource_not_found(self):
        uuid = sys_uuid.uuid4()
        client = _make_client()
        users = [{"uuid": str(uuid), "name": "ghost"}]

        with patch(_CORE_MODELS) as mr:
            mr.objects.get_all.return_value = []
            enriched = client._enrich_users(users)

        # User should be skipped, not raise an error
        assert enriched == []


class TestGCUsersRestApiBackendClientGet:
    def test_get_returns_enriched_user(self):
        uuid = sys_uuid.uuid4()
        res = _make_resource(uuid=uuid)
        db_res = _make_db_resource(uuid, "mypass")

        client = _make_client()
        client._client.get.return_value = {"uuid": str(uuid), "name": "alice"}

        with patch(_CORE_MODELS) as mr:
            mr.objects.get_all.return_value = [db_res]
            result = client.get(res)

        client._client.get.assert_called_once_with(USERS_COLLECTION, uuid)
        assert result["uuid"] == str(uuid)
        assert result["password"] == "mypass"

    def test_get_raises_resource_not_found_on_404(self):
        uuid = sys_uuid.uuid4()
        res = _make_resource(uuid=uuid)

        client = _make_client()
        client._client.get.side_effect = bazooka_exc.NotFoundError(MagicMock())

        with pytest.raises(client_exc.ResourceNotFound):
            client.get(res)

    def test_get_falls_back_to_target_password_when_user_not_in_data_plane(self):
        uuid = sys_uuid.uuid4()
        res = _make_resource(uuid=uuid)

        client = _make_client()
        client._client.get.return_value = {"uuid": str(uuid), "name": "alice"}

        with patch(_CORE_MODELS) as mr:
            mr.objects.get_all.return_value = []
            result = client.get(res)

        # Should not raise - falls back to target resource password
        assert result["password"] == "s3cr3t"
        assert result["uuid"] == str(uuid)


class TestGCUsersRestApiBackendClientCreate:
    def test_create_injects_uuid_and_preserves_password(self):
        uuid = sys_uuid.uuid4()
        value = {
            "uuid": str(uuid),
            "name": "bob",
            "password": "bobpass",
            "status": "ACTIVE",
        }
        res = _make_resource(uuid=uuid, value=value)

        client = _make_client()
        client._client.create.return_value = {
            "uuid": str(uuid),
            "name": "bob",
            "status": "ACTIVE",
        }

        result = client.create(res)

        # uuid must be injected into the payload
        assert res.value["uuid"] == str(uuid)

        # password must be preserved in the result even if backend strips it
        assert result["password"] == "bobpass"

    def test_create_calls_correct_collection(self):
        uuid = sys_uuid.uuid4()
        value = {"uuid": str(uuid), "name": "bob", "password": "p", "status": "ACTIVE"}
        res = _make_resource(uuid=uuid, value=value)

        client = _make_client()
        client._client.create.return_value = {"uuid": str(uuid), "name": "bob"}

        client.create(res)

        args, _ = client._client.create.call_args
        assert args[0] == USERS_COLLECTION


class TestGCUsersRestApiBackendClientUpdate:
    def test_update_strips_ro_fields_and_restores_password(self):
        uuid = sys_uuid.uuid4()
        value = {
            "uuid": str(uuid),
            "name": "carol",
            "password": "newpass",
            "created_at": "2024-01-01",
            "updated_at": "2024-01-02",
            "project_id": "proj-123",
            "status": "ACTIVE",
        }
        res = _make_resource(uuid=uuid, value=value)
        db_res = _make_db_resource(uuid, "dbpass")

        client = _make_client()
        client._client.get.return_value = {"uuid": str(uuid), "name": "carol"}
        client._client.update.return_value = {
            "uuid": str(uuid),
            "name": "carol",
            "status": "ACTIVE",
        }

        with patch(_CORE_MODELS) as mr:
            mr.objects.get_all.return_value = [db_res]
            result = client.update(res)

        # RO fields must be stripped from the payload sent to backend
        _, kwargs = client._client.update.call_args
        sent = kwargs
        assert "created_at" not in sent
        assert "updated_at" not in sent
        assert "project_id" not in sent
        assert "uuid" not in sent

        # password must be restored from the enriched get() result
        assert result["password"] == "dbpass"

    def test_update_restores_resource_value_on_backend_failure(self):
        uuid = sys_uuid.uuid4()
        value = {
            "uuid": str(uuid),
            "name": "dave",
            "password": "dpass",
            "status": "ACTIVE",
        }
        res = _make_resource(uuid=uuid, value=value)
        db_res = _make_db_resource(uuid, "dpass")

        # Keep a snapshot before update mutates the dict in-place
        value_before = value.copy()

        client = _make_client()
        client._client.get.return_value = {"uuid": str(uuid), "name": "dave"}
        client._client.update.side_effect = bazooka_exc.NotFoundError(MagicMock())

        with patch(_CORE_MODELS) as mr:
            mr.objects.get_all.return_value = [db_res]
            with pytest.raises(Exception):
                client.update(res)

        # resource.value must be restored after exception (same keys as before update)
        assert res.value == value_before

    def test_update_falls_back_to_target_password_when_user_not_in_data_plane(self):
        uuid = sys_uuid.uuid4()
        value = {
            "uuid": str(uuid),
            "name": "eve",
            "password": "newpass",
            "status": "ACTIVE",
        }
        res = _make_resource(uuid=uuid, value=value)

        client = _make_client()
        client._client.get.return_value = {"uuid": str(uuid), "name": "eve"}
        client._client.update.return_value = {"uuid": str(uuid), "name": "eve"}

        with patch(_CORE_MODELS) as mr:
            mr.objects.get_all.return_value = []
            result = client.update(res)

        # Should not raise - falls back to target resource password
        assert result["password"] == "newpass"


class TestGCUsersRestApiBackendClientList:
    def test_list_returns_empty_when_no_filters(self):
        client = _make_client(tf_storage=None)
        result = client.list(USER_KIND)
        assert result == []
        client._client.filter.assert_not_called()

    def test_list_returns_enriched_users_when_filters_present(self):
        uuid1, uuid2 = sys_uuid.uuid4(), sys_uuid.uuid4()

        tf_storage = MagicMock()
        tf_storage.storage.return_value = {
            USER_KIND: {uuid1: MagicMock(), uuid2: MagicMock()}
        }

        db_res1 = _make_db_resource(uuid1, "pass1")
        db_res2 = _make_db_resource(uuid2, "pass2")

        client = _make_client(tf_storage=tf_storage)
        client._client.filter.return_value = [
            {"uuid": str(uuid1), "name": "alice"},
            {"uuid": str(uuid2), "name": "bob"},
        ]

        with patch(_CORE_MODELS) as mr:
            mr.objects.get_all.return_value = [db_res1, db_res2]
            result = client.list(USER_KIND)

        assert len(result) == 2
        passwords = {r["password"] for r in result}
        assert passwords == {"pass1", "pass2"}

    def test_list_calls_correct_collection_with_uuid_filter(self):
        uuid = sys_uuid.uuid4()
        db_res = _make_db_resource(uuid, "p")

        tf_storage = MagicMock()
        tf_storage.storage.return_value = {USER_KIND: {uuid: MagicMock()}}

        client = _make_client(tf_storage=tf_storage)
        client._client.filter.return_value = [{"uuid": str(uuid), "name": "eve"}]

        with patch(_CORE_MODELS) as mr:
            mr.objects.get_all.return_value = [db_res]
            client.list(USER_KIND)

        args, kwargs = client._client.filter.call_args
        assert args[0] == USERS_COLLECTION
        assert "uuid" in kwargs
