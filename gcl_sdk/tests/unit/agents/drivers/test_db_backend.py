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

import typing as tp
from unittest.mock import MagicMock
import uuid as sys_uuid

import pytest
from restalchemy.dm import filters as dm_filters
from restalchemy.storage import exceptions as ra_exc

from gcl_sdk.agents.universal.clients.backend import exceptions as client_exc
from gcl_sdk.agents.universal.clients.backend.db import DatabaseBackendClient
from gcl_sdk.agents.universal.clients.backend.db import ModelSpec
from gcl_sdk.agents.universal.dm import models


def _make_resource(
    kind: str, uuid: tp.Optional[sys_uuid.UUID] = None, value: tp.Optional[dict] = None
) -> models.Resource:
    uuid = uuid or sys_uuid.uuid4()
    value = value or {"uuid": str(uuid), "a": 1, "b": 2}
    return models.Resource.from_value(value, kind, frozenset(value.keys()))


class TestDatabaseBackendClient:
    def test_get_success_uses_filters_and_returns_obj(self):
        # Arrange
        kind = "config"
        resource = _make_resource(kind)

        # Model and manager mocks
        obj = MagicMock(name="ModelInstance")
        manager = MagicMock()
        manager.get_one.return_value = obj

        model = MagicMock()
        model.objects = manager

        # ModelSpec with extra filter
        ms = ModelSpec(model=model, kind=kind, filters={"project": dm_filters.EQ("p1")})

        storage = MagicMock()
        client = DatabaseBackendClient(model_specs=[ms], tf_storage=storage)
        client.set_session(object())

        # Act
        actual = client.get(resource)

        # Assert
        assert actual is obj
        # ensure filters include uuid and extra filter
        args, kwargs = manager.get_one.call_args
        assert "filters" in kwargs
        flt = kwargs["filters"]
        assert set(flt.keys()) == {"uuid", "project"}
        assert isinstance(flt["uuid"], dm_filters.EQ)
        assert flt["uuid"].value == str(resource.uuid)
        assert isinstance(flt["project"], dm_filters.EQ)
        assert flt["project"].value == "p1"

    def test_get_maps_not_found(self):
        kind = "config"
        resource = _make_resource(kind)

        manager = MagicMock()
        manager.get_one.side_effect = ra_exc.RecordNotFound(
            model="Model", filters={"uuid": str(resource.uuid)}
        )

        model = MagicMock()
        model.objects = manager
        ms = ModelSpec(model=model, kind=kind, filters={})

        client = DatabaseBackendClient(model_specs=[ms], tf_storage=MagicMock())
        client.set_session(object())

        with pytest.raises(client_exc.ResourceNotFound):
            client.get(resource)

    def test_list_returns_empty_if_no_filters_and_no_target_fields(self):
        kind = "config"
        # ModelSpec without predefined filters -> uses storage
        ms = ModelSpec(model=MagicMock(), kind=kind, filters=None)

        storage = MagicMock()
        storage.storage.return_value = {kind: {}}  # empty for this kind

        client = DatabaseBackendClient(model_specs=[ms], tf_storage=storage)
        client.set_session(object())

        res = client.list(kind)

        assert res == []
        # get_all should not be called when filters are empty
        assert not ms.model.objects.get_all.called

    def test_list_uses_storage_target_fields_to_build_in_filter(self):
        kind = "config"

        manager = MagicMock()
        expected_list = [MagicMock(name="Model1"), MagicMock(name="Model2")]
        manager.get_all.return_value = expected_list

        model = MagicMock()
        model.objects = manager

        ms = ModelSpec(model=model, kind=kind, filters=None)

        storage = MagicMock()
        storage.storage.return_value = {
            kind: {
                "uuid1": ["a", "b"],
                "uuid2": ["a"],
            }
        }

        client = DatabaseBackendClient(model_specs=[ms], tf_storage=storage)
        client.set_session(object())

        res = client.list(kind)

        assert res == expected_list
        args, kwargs = manager.get_all.call_args
        assert "filters" in kwargs and "uuid" in kwargs["filters"]
        uuid_filter = kwargs["filters"]["uuid"]
        assert isinstance(uuid_filter, dm_filters.In)
        # should include both keys from storage
        assert set(uuid_filter.value) == {"uuid1", "uuid2"}

    def test_create_injects_filter_fields_and_uses_restore_when_missing(self):
        kind = "config"
        uid = str(sys_uuid.uuid4())
        resource = _make_resource(kind, value={"uuid": uid, "a": 10})

        manager = MagicMock()
        manager.get_one_or_none.return_value = None  # not exists

        model_instance = MagicMock()
        model = MagicMock()
        model.objects = manager
        model.restore_from_simple_view.return_value = model_instance

        # Filters should be injected into value if missing
        ms = ModelSpec(
            model=model,
            kind=kind,
            filters={"project": dm_filters.EQ("p1")},
            inject_filter_fields=True,
        )

        storage = MagicMock()
        client = DatabaseBackendClient(model_specs=[ms], tf_storage=storage)
        client.set_session(object())

        actual = client.create(resource)

        # Should have called restore_from_simple_view with injected field
        kwargs = model.restore_from_simple_view.call_args.kwargs
        assert kwargs["uuid"] == uid
        assert kwargs["a"] == 10
        assert kwargs["project"] == "p1"

        # and then insert called
        model_instance.insert.assert_called_once()
        assert actual is model_instance

    def test_create_raises_already_exists(self):
        kind = "config"
        resource = _make_resource(kind)

        manager = MagicMock()
        manager.get_one_or_none.return_value = MagicMock()

        model = MagicMock()
        model.objects = manager

        ms = ModelSpec(model=model, kind=kind, filters={})

        client = DatabaseBackendClient(model_specs=[ms], tf_storage=MagicMock())
        client.set_session(object())

        with pytest.raises(client_exc.ResourceAlreadyExists):
            client.create(resource)

    def test_update_not_found_when_missing(self):
        kind = "config"
        resource = _make_resource(kind)

        manager = MagicMock()
        manager.get_one_or_none.return_value = None

        model = MagicMock()
        model.objects = manager

        ms = ModelSpec(model=model, kind=kind, filters={})
        client = DatabaseBackendClient(model_specs=[ms], tf_storage=MagicMock())
        client.set_session(object())

        with pytest.raises(client_exc.ResourceNotFound):
            client.update(resource)

    def test_update_updates_only_non_read_only_and_calls_update(self):
        kind = "config"
        # new value wants to change both a and b
        uid = str(sys_uuid.uuid4())
        resource = _make_resource(kind, value={"uuid": uid, "a": 10, "b": 20})

        # existing object with properties and current values
        prop_a = MagicMock()
        prop_a.is_read_only.return_value = True
        prop_b = MagicMock()
        prop_b.is_read_only.return_value = False

        existing_obj = MagicMock()
        existing_obj.properties = {"a": prop_a, "b": prop_b}
        existing_obj.a = 1
        existing_obj.b = 2

        manager = MagicMock()
        manager.get_one_or_none.return_value = existing_obj

        # model.from_ua_resource returns an object with desired values
        updated_obj = MagicMock()
        updated_obj.a = 10
        updated_obj.b = 20

        model = MagicMock()
        model.objects = manager
        model.from_ua_resource.return_value = updated_obj

        ms = ModelSpec(model=model, kind=kind, filters={})
        client = DatabaseBackendClient(model_specs=[ms], tf_storage=MagicMock())
        client.set_session(object())

        actual = client.update(resource)

        # a is read-only -> unchanged
        assert existing_obj.a == 1
        # b is writable -> updated
        assert existing_obj.b == 20
        existing_obj.update.assert_called_once()
        assert actual is existing_obj

    def test_delete_deletes_when_found_and_noop_when_missing(self):
        kind = "config"
        resource = _make_resource(kind)

        # Case 1: found and deleted
        obj = MagicMock()
        manager = MagicMock()
        manager.get_one.return_value = obj

        model = MagicMock()
        model.objects = manager
        ms = ModelSpec(model=model, kind=kind, filters={})

        client = DatabaseBackendClient(model_specs=[ms], tf_storage=MagicMock())
        client.set_session(object())
        client.delete(resource)
        obj.delete.assert_called_once()

        # Case 2: not found -> should not raise
        manager.get_one.side_effect = ra_exc.RecordNotFound(
            model="Model", filters={"uuid": str(resource.uuid)}
        )
        client.delete(resource)
        # No exception raised
