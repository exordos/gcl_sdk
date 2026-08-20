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
USERS_COLLECTION = "/v1/iam/users/"
USER_SECRET_FIELD = "password"

CLIENT_KIND = "em_core_iam_clients"
CLIENTS_COLLECTION = "/v1/iam/clients/"
CLIENT_SECRET_FIELD = "secret"

_USER_SPEC = core_back.SecretModelSpec(
    kind=USER_KIND,
    collection=USERS_COLLECTION,
    secret_field=USER_SECRET_FIELD,
)
_CLIENT_SPEC = core_back.SecretModelSpec(
    kind=CLIENT_KIND,
    collection=CLIENTS_COLLECTION,
    secret_field=CLIENT_SECRET_FIELD,
)
_CLIENT_SPEC_WITH_FILTER = core_back.SecretModelSpec(
    kind=CLIENT_KIND,
    collection=CLIENTS_COLLECTION,
    secret_field=CLIENT_SECRET_FIELD,
    filters={"project_id": "12345678-c625-4fee-81d5-f691897b8142"},
)
_MODEL_SPECS = [_USER_SPEC, _CLIENT_SPEC]


def _make_resource(
    uuid: sys_uuid.UUID | None = None,
    value: dict | None = None,
    kind: str = USER_KIND,
    secret_field: str = USER_SECRET_FIELD,
) -> models.Resource:
    uuid = uuid or sys_uuid.uuid4()
    value = value or {
        "uuid": str(uuid),
        "name": "test-user",
        secret_field: "s3cr3t",
        "status": "ACTIVE",
    }
    return models.Resource.from_value(
        value, kind, target_fields=frozenset(value.keys())
    )


def _make_client(
    tf_storage: MagicMock | None = None,
    model_specs: list[core_back.SecretModelSpec] | None = None,
    project_id: sys_uuid.UUID | None = None,
) -> core_back.GCSecretRestApiBackendClient:
    http_client = MagicMock()
    return core_back.GCSecretRestApiBackendClient(
        http_client=http_client,
        model_specs=model_specs or _MODEL_SPECS,
        project_id=project_id,
        tf_storage=tf_storage,
    )


def _make_db_resource(
    uuid: sys_uuid.UUID,
    secret: str,
    kind: str = USER_KIND,
    secret_field: str = USER_SECRET_FIELD,
) -> models.Resource:
    """Return a Resource that simulates DB-stored record with secret."""
    value = {"uuid": str(uuid), "name": "test-user", secret_field: secret}
    return models.Resource.from_value(
        value, kind, target_fields=frozenset(value.keys())
    )


class TestGCSecretRestApiBackendClientInit:
    def test_collection_map_built_from_specs(self):
        client = _make_client()
        assert client._collection_map == {
            USER_KIND: USERS_COLLECTION,
            CLIENT_KIND: CLIENTS_COLLECTION,
        }

    def test_spec_map_is_stored(self):
        client = _make_client()
        assert client._spec_map[USER_KIND] == _USER_SPEC
        assert client._spec_map[CLIENT_KIND] == _CLIENT_SPEC

    def test_custom_model_specs(self):
        http_client = MagicMock()
        spec = core_back.SecretModelSpec(
            kind="custom_kind",
            collection="/v1/custom/",
            secret_field="token",
        )
        client = core_back.GCSecretRestApiBackendClient(
            http_client=http_client,
            model_specs=[spec],
        )
        assert "custom_kind" in client._collection_map
        assert client._spec_map["custom_kind"].secret_field == "token"


class TestGCSecretRestApiBackendClientGetFilters:
    def test_returns_empty_when_no_tf_storage_and_no_spec_filters(self):
        client = _make_client(tf_storage=None)
        assert client._get_filters(USER_KIND) == {}

    def test_returns_spec_filters_when_spec_has_filters(self):
        client = _make_client(model_specs=[_CLIENT_SPEC_WITH_FILTER])
        assert client._get_filters(CLIENT_KIND) == {
            "project_id": "12345678-c625-4fee-81d5-f691897b8142"
        }

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

    def test_spec_filters_take_precedence_over_tf_storage(self):
        uuid1 = sys_uuid.uuid4()
        tf_storage = MagicMock()
        tf_storage.storage.return_value = {CLIENT_KIND: {uuid1: MagicMock()}}
        client = _make_client(
            tf_storage=tf_storage,
            model_specs=[_CLIENT_SPEC_WITH_FILTER],
        )
        assert client._get_filters(CLIENT_KIND) == {
            "project_id": "12345678-c625-4fee-81d5-f691897b8142"
        }


class TestGCSecretRestApiBackendClientEnrichResources:
    def test_enriches_users_with_password_from_db(self):
        uuid = sys_uuid.uuid4()
        db_res = _make_db_resource(uuid, "s3cr3t")

        client = _make_client()
        users = [{"uuid": str(uuid), "name": "alice"}]

        with patch(_CORE_MODELS) as mr:
            mr.objects.get_all.return_value = [db_res]
            enriched = client._enrich_resources(USER_KIND, users)

        assert enriched[0]["password"] == "s3cr3t"

    def test_enriches_clients_with_secret_from_db(self):
        uuid = sys_uuid.uuid4()
        db_res = _make_db_resource(
            uuid, "client-secret", kind=CLIENT_KIND, secret_field=CLIENT_SECRET_FIELD
        )

        client = _make_client()
        clients = [{"uuid": str(uuid), "name": "my-client"}]

        with patch(_CORE_MODELS) as mr:
            mr.objects.get_all.return_value = [db_res]
            enriched = client._enrich_resources(CLIENT_KIND, clients)

        assert enriched[0]["secret"] == "client-secret"

    def test_skips_resource_when_db_resource_not_found(self):
        uuid = sys_uuid.uuid4()
        client = _make_client()
        users = [{"uuid": str(uuid), "name": "ghost"}]

        with patch(_CORE_MODELS) as mr:
            mr.objects.get_all.return_value = []
            enriched = client._enrich_resources(USER_KIND, users)

        assert enriched == []

    def test_returns_resources_unchanged_when_kind_unknown(self):
        uuid = sys_uuid.uuid4()
        client = _make_client()
        users = [{"uuid": str(uuid), "name": "alice"}]
        enriched = client._enrich_resources("unknown_kind", users)
        assert enriched == users


class TestGCSecretRestApiBackendClientGet:
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

    def test_get_returns_enriched_client(self):
        uuid = sys_uuid.uuid4()
        res = _make_resource(
            uuid=uuid, kind=CLIENT_KIND, secret_field=CLIENT_SECRET_FIELD
        )
        db_res = _make_db_resource(
            uuid, "client-pass", kind=CLIENT_KIND, secret_field=CLIENT_SECRET_FIELD
        )

        client = _make_client()
        client._client.get.return_value = {"uuid": str(uuid), "name": "my-client"}

        with patch(_CORE_MODELS) as mr:
            mr.objects.get_all.return_value = [db_res]
            result = client.get(res)

        client._client.get.assert_called_once_with(CLIENTS_COLLECTION, uuid)
        assert result["uuid"] == str(uuid)
        assert result["secret"] == "client-pass"

    def test_get_raises_resource_not_found_on_404(self):
        uuid = sys_uuid.uuid4()
        res = _make_resource(uuid=uuid)

        client = _make_client()
        client._client.get.side_effect = bazooka_exc.NotFoundError(MagicMock())

        with pytest.raises(client_exc.ResourceNotFound):
            client.get(res)

    def test_get_falls_back_to_target_secret_when_not_in_data_plane(self):
        uuid = sys_uuid.uuid4()
        res = _make_resource(uuid=uuid)

        client = _make_client()
        client._client.get.return_value = {"uuid": str(uuid), "name": "alice"}

        with patch(_CORE_MODELS) as mr:
            mr.objects.get_all.return_value = []
            result = client.get(res)

        assert result["password"] == "s3cr3t"
        assert result["uuid"] == str(uuid)

    def test_get_falls_back_to_client_secret_when_not_in_data_plane(self):
        uuid = sys_uuid.uuid4()
        res = _make_resource(
            uuid=uuid, kind=CLIENT_KIND, secret_field=CLIENT_SECRET_FIELD
        )

        client = _make_client()
        client._client.get.return_value = {"uuid": str(uuid), "name": "my-client"}

        with patch(_CORE_MODELS) as mr:
            mr.objects.get_all.return_value = []
            result = client.get(res)

        assert result["secret"] == "s3cr3t"
        assert result["uuid"] == str(uuid)


class TestGCSecretRestApiBackendClientCreate:
    def test_create_injects_uuid_and_preserves_secret(self):
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

        assert res.value["uuid"] == str(uuid)
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

    def test_create_preserves_secret_for_client_kind(self):
        uuid = sys_uuid.uuid4()
        value = {
            "uuid": str(uuid),
            "name": "my-client",
            "secret": "client-secret",
            "status": "ACTIVE",
        }
        res = _make_resource(
            uuid=uuid, value=value, kind=CLIENT_KIND, secret_field=CLIENT_SECRET_FIELD
        )

        client = _make_client()
        client._client.create.return_value = {
            "uuid": str(uuid),
            "name": "my-client",
            "status": "ACTIVE",
        }

        result = client.create(res)

        assert result["secret"] == "client-secret"

    def test_create_raises_on_project_mismatch(self):
        pid = sys_uuid.uuid4()
        other_pid = sys_uuid.uuid4()
        value = {
            "uuid": str(sys_uuid.uuid4()),
            "name": "bob",
            "password": "p",
            "project_id": str(other_pid),
            "status": "ACTIVE",
        }
        res = _make_resource(value=value)

        client = _make_client(project_id=pid)

        with pytest.raises(core_back.ResourceProjectMismatch):
            client.create(res)

        client._client.create.assert_not_called()

    def test_create_allows_matching_project_id(self):
        pid = sys_uuid.uuid4()
        uuid = sys_uuid.uuid4()
        value = {
            "uuid": str(uuid),
            "name": "bob",
            "password": "bobpass",
            "project_id": str(pid),
            "status": "ACTIVE",
        }
        res = _make_resource(uuid=uuid, value=value)

        client = _make_client(project_id=pid)
        client._client.create.return_value = {
            "uuid": str(uuid),
            "name": "bob",
            "status": "ACTIVE",
        }

        result = client.create(res)

        assert result["password"] == "bobpass"

    def test_create_allows_missing_project_id_when_scoped(self):
        pid = sys_uuid.uuid4()
        uuid = sys_uuid.uuid4()
        value = {
            "uuid": str(uuid),
            "name": "bob",
            "password": "bobpass",
            "status": "ACTIVE",
        }
        res = _make_resource(uuid=uuid, value=value)

        client = _make_client(project_id=pid)
        client._client.create.return_value = {
            "uuid": str(uuid),
            "name": "bob",
            "status": "ACTIVE",
        }

        result = client.create(res)

        assert result["password"] == "bobpass"


class TestGCSecretRestApiBackendClientUpdate:
    def test_update_strips_ro_fields_and_restores_secret(self):
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

        _, kwargs = client._client.update.call_args
        sent = kwargs
        assert "created_at" not in sent
        assert "updated_at" not in sent
        assert "project_id" not in sent
        assert "uuid" not in sent

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

        value_before = value.copy()

        client = _make_client()
        client._client.get.return_value = {"uuid": str(uuid), "name": "dave"}
        client._client.update.side_effect = bazooka_exc.NotFoundError(MagicMock())

        with patch(_CORE_MODELS) as mr:
            mr.objects.get_all.return_value = [db_res]
            with pytest.raises(Exception):
                client.update(res)

        assert res.value == value_before

    def test_update_falls_back_to_target_secret_when_not_in_data_plane(self):
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

        assert result["password"] == "newpass"


class TestGCSecretRestApiBackendClientList:
    def test_list_returns_empty_when_no_filters(self):
        client = _make_client(tf_storage=None)
        result = client.list(USER_KIND)
        assert result == []
        client._client.filter.assert_not_called()

    def test_list_returns_enriched_resources_when_filters_present(self):
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

    def test_list_uses_spec_filters_when_set(self):
        pid = "12345678-c625-4fee-81d5-f691897b8142"
        uuid = sys_uuid.uuid4()
        db_res = _make_db_resource(
            uuid, "p", kind=CLIENT_KIND, secret_field=CLIENT_SECRET_FIELD
        )

        client = _make_client(model_specs=[_CLIENT_SPEC_WITH_FILTER])
        client._client.filter.return_value = [{"uuid": str(uuid), "name": "frank"}]

        with patch(_CORE_MODELS) as mr:
            mr.objects.get_all.return_value = [db_res]
            client.list(CLIENT_KIND)

        args, kwargs = client._client.filter.call_args
        assert args[0] == CLIENTS_COLLECTION
        assert kwargs["project_id"] == pid


class TestUserCapabilityDriverWrapper:
    """Tests for the deprecated UserCapabilityDriver wrapper."""

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

    def test_is_subclass_of_secret_driver(self):
        from gcl_sdk.agents.universal.drivers import core as drv_core

        assert issubclass(
            drv_core.UserCapabilityDriver, drv_core.SecretCapabilityDriver
        )


class TestSecretCapabilityDriver:
    """Tests for the generic SecretCapabilityDriver."""

    def test_capabilities_returns_all_kinds(self):
        from gcl_sdk.agents.universal.drivers import core as drv_core

        with (
            patch("gcl_sdk.agents.universal.drivers.core.bazooka.Client"),
            patch("gcl_sdk.agents.universal.drivers.core.base.CoreIamAuthenticator"),
            patch("gcl_sdk.agents.universal.drivers.core.base.CollectionBaseClient"),
            patch("gcl_sdk.agents.universal.storage.fs.TargetFieldsFileStorage"),
        ):
            driver = drv_core.SecretCapabilityDriver(
                username="admin",
                password="pass",
                user_api_base_url="http://localhost",
                agent_work_dir="/tmp",
                **{
                    USER_KIND: f"{USERS_COLLECTION}, {USER_SECRET_FIELD}",
                    CLIENT_KIND: f"{CLIENTS_COLLECTION}, {CLIENT_SECRET_FIELD}",
                },
            )
        assert set(driver.get_capabilities()) == {USER_KIND, CLIENT_KIND}

    def test_raises_on_invalid_spec_format(self):
        from gcl_sdk.agents.universal.drivers import core as drv_core

        with (
            patch("gcl_sdk.agents.universal.drivers.core.bazooka.Client"),
            patch("gcl_sdk.agents.universal.drivers.core.base.CoreIamAuthenticator"),
            patch("gcl_sdk.agents.universal.drivers.core.base.CollectionBaseClient"),
            patch("gcl_sdk.agents.universal.storage.fs.TargetFieldsFileStorage"),
        ):
            with pytest.raises(ValueError, match="Invalid model spec"):
                drv_core.SecretCapabilityDriver(
                    username="admin",
                    password="pass",
                    user_api_base_url="http://localhost",
                    agent_work_dir="/tmp",
                    **{USER_KIND: "no_comma_here"},
                )

    def test_parses_filter_from_spec_string(self):
        from gcl_sdk.agents.universal.drivers import core as drv_core

        with (
            patch("gcl_sdk.agents.universal.drivers.core.bazooka.Client"),
            patch("gcl_sdk.agents.universal.drivers.core.base.CoreIamAuthenticator"),
            patch("gcl_sdk.agents.universal.drivers.core.base.CollectionBaseClient"),
            patch("gcl_sdk.agents.universal.storage.fs.TargetFieldsFileStorage"),
        ):
            driver = drv_core.SecretCapabilityDriver(
                username="admin",
                password="pass",
                user_api_base_url="http://localhost",
                agent_work_dir="/tmp",
                **{
                    CLIENT_KIND: (
                        f"{CLIENTS_COLLECTION}, {CLIENT_SECRET_FIELD}, "
                        f"filter:project_id:12345678-c625-4fee-81d5-f691897b8142"
                    ),
                },
            )
        assert driver.get_capabilities() == [CLIENT_KIND]
        # Verify the spec was parsed with filters
        client = driver._client
        assert client._spec_map[CLIENT_KIND].filters == {
            "project_id": "12345678-c625-4fee-81d5-f691897b8142"
        }

    def test_raises_on_invalid_filter_format(self):
        from gcl_sdk.agents.universal.drivers import core as drv_core

        with (
            patch("gcl_sdk.agents.universal.drivers.core.bazooka.Client"),
            patch("gcl_sdk.agents.universal.drivers.core.base.CoreIamAuthenticator"),
            patch("gcl_sdk.agents.universal.drivers.core.base.CollectionBaseClient"),
            patch("gcl_sdk.agents.universal.storage.fs.TargetFieldsFileStorage"),
        ):
            with pytest.raises(ValueError, match="Invalid filter spec"):
                drv_core.SecretCapabilityDriver(
                    username="admin",
                    password="pass",
                    user_api_base_url="http://localhost",
                    agent_work_dir="/tmp",
                    **{
                        USER_KIND: (
                            f"{USERS_COLLECTION}, {USER_SECRET_FIELD}, "
                            f"bad_filter_format"
                        ),
                    },
                )

    def test_project_scope_auth_when_use_project_scope(self):
        from gcl_sdk.agents.universal.drivers import core as drv_core

        pid = sys_uuid.uuid4()
        with (
            patch("gcl_sdk.agents.universal.drivers.core.bazooka.Client"),
            patch(
                "gcl_sdk.agents.universal.drivers.core.base.CoreIamAuthenticator"
            ) as mock_auth,
            patch("gcl_sdk.agents.universal.drivers.core.base.CollectionBaseClient"),
            patch("gcl_sdk.agents.universal.storage.fs.TargetFieldsFileStorage"),
        ):
            drv_core.SecretCapabilityDriver(
                username="admin",
                password="pass",
                user_api_base_url="http://localhost",
                project_id=pid,
                use_project_scope=True,
                agent_work_dir="/tmp",
                **{USER_KIND: f"{USERS_COLLECTION}, {USER_SECRET_FIELD}"},
            )

        _, kwargs = mock_auth.call_args
        assert "scope" in kwargs

    def test_no_project_scope_auth_by_default(self):
        from gcl_sdk.agents.universal.drivers import core as drv_core

        with (
            patch("gcl_sdk.agents.universal.drivers.core.bazooka.Client"),
            patch(
                "gcl_sdk.agents.universal.drivers.core.base.CoreIamAuthenticator"
            ) as mock_auth,
            patch("gcl_sdk.agents.universal.drivers.core.base.CollectionBaseClient"),
            patch("gcl_sdk.agents.universal.storage.fs.TargetFieldsFileStorage"),
        ):
            drv_core.SecretCapabilityDriver(
                username="admin",
                password="pass",
                user_api_base_url="http://localhost",
                agent_work_dir="/tmp",
                **{USER_KIND: f"{USERS_COLLECTION}, {USER_SECRET_FIELD}"},
            )

        _, kwargs = mock_auth.call_args
        assert "scope" not in kwargs
