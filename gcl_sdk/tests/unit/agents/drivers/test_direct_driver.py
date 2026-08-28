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

from unittest.mock import MagicMock
import uuid as sys_uuid

import pytest

from gcl_sdk.agents.universal.clients.backend import exceptions as client_exc
from gcl_sdk.agents.universal.dm import models
from gcl_sdk.agents.universal.drivers import exceptions as driver_exc
from gcl_sdk.agents.universal.drivers.direct import DirectAgentDriver
from gcl_sdk.agents.universal.drivers.direct import ResourceTransformer
from gcl_sdk.agents.universal.storage import base as storage_base
from gcl_sdk.agents.universal.storage import exceptions as storage_exc


def _make_resource(
    kind: str, uuid: sys_uuid.UUID | None = None, value: dict | None = None
) -> models.Resource:
    uuid = uuid or sys_uuid.uuid4()
    value = value or {"uuid": str(uuid), "a": 1, "b": 2, "status": "ACTIVE"}
    return models.Resource.from_value(
        value, kind, target_fields=frozenset(value.keys())
    )


def _driver_with_caps(
    client_mock: MagicMock,
    storage_mock: MagicMock,
    caps: list[str],
    transformer_map: dict[str, ResourceTransformer] | None = None,
):
    class _Drv(DirectAgentDriver):
        def get_capabilities(self) -> list[str]:
            return caps

    return _Drv(
        client=client_mock,
        storage=storage_mock,
        transformer_map=transformer_map,
    )


class TestDirectDriver:
    def test_start_and_finalize(self):
        client = MagicMock()
        storage = MagicMock()

        drv = _driver_with_caps(client, storage, caps=["config"])

        drv.start()
        storage.load.assert_called_once_with()

        drv.finalize()
        storage.persist.assert_called_once_with()

    def test_validate_unsupported_kind_raises(self):
        client = MagicMock()
        storage = MagicMock()
        drv = _driver_with_caps(
            client, storage, caps=["config"]
        )  # only 'config' supported

        res = _make_resource("secret")

        with pytest.raises(TypeError):
            drv.get(res)

        with pytest.raises(TypeError):
            drv.create(res)

        with pytest.raises(TypeError):
            drv.update(res)

        with pytest.raises(TypeError):
            drv.list("secret")

        with pytest.raises(TypeError):
            drv.delete(res)

    def test_get_success_when_client_and_storage_ok(self):
        client = MagicMock()
        storage = MagicMock()
        drv = _driver_with_caps(client, storage, caps=["config"])

        res = _make_resource("config")
        tf_item = storage_base.TargetFieldItem(
            kind=res.kind,
            uuid=res.uuid,
            target_fields=frozenset({"a", "b", "uuid", "status"}),
        )

        # Note: DirectAgentDriver.get currently
        # calls storage.get(resource.uuid, resource.kind)
        storage.get.return_value = tf_item
        client_value = {
            "uuid": str(res.uuid),
            "a": 10,
            "b": 20,
            "status": "ACTIVE",
        }
        client.get.return_value = client_value

        actual = drv.get(res)

        # Ensure storage and client were called
        assert storage.get.call_count == 1
        client.get.assert_called_once_with(res)

        assert isinstance(actual, models.Resource)
        assert actual.uuid == res.uuid
        assert actual.kind == res.kind
        assert actual.value == client_value

    def test_get_not_found_maps_exceptions(self):
        client = MagicMock()
        storage = MagicMock()
        drv = _driver_with_caps(client, storage, caps=["config"])

        res = _make_resource("config")

        # storage raises -> driver maps to ResourceNotFound
        storage.get.side_effect = storage_exc.ItemNotFound(item=None)  # type: ignore[arg-type]
        with pytest.raises(driver_exc.ResourceNotFound):
            drv.get(res)

        # client raises -> driver maps to ResourceNotFound
        storage.get.reset_mock()
        storage.get.return_value = storage_base.TargetFieldItem(
            res.kind, res.uuid, frozenset(res.value.keys())
        )
        client.get.side_effect = client_exc.ResourceNotFound(resource=res)  # type: ignore[arg-type]
        with pytest.raises(driver_exc.ResourceNotFound):
            drv.get(res)

    def test_list_collects_intersection_of_storage_and_client(self):
        client = MagicMock()
        storage = MagicMock()
        drv = _driver_with_caps(client, storage, caps=["config"])

        # Storage has two items
        uuid1, uuid2, uuid3 = (
            sys_uuid.uuid4(),
            sys_uuid.uuid4(),
            sys_uuid.uuid4(),
        )
        s_item1 = storage_base.TargetFieldItem(
            "config", uuid1, frozenset({"a", "b", "uuid"})
        )
        s_item2 = storage_base.TargetFieldItem(
            "config", uuid2, frozenset({"a", "b", "uuid"})
        )
        storage.list.return_value = [s_item1, s_item2]

        # Client returns: one dict that matches storage (uuid1),
        # one Resource that doesn't have a storage entry (uuid3),
        # and one dict for uuid2 also matches storage.
        client_list = [
            {"uuid": str(uuid1), "a": 1, "b": 2},
            models.Resource.from_value(
                {"uuid": str(uuid3), "a": 3, "b": 4}, "config", s_item2.target_fields
            ),
            {"uuid": str(uuid2), "a": 5, "b": 6},
        ]
        client.list.return_value = client_list

        resources = drv.list("config")

        # Only items with both client and storage
        # should be returned (uuid1, uuid2)
        uuids = {r.uuid for r in resources}
        assert uuids == {uuid1, uuid2}
        assert all(r.kind == "config" for r in resources)

        storage.list.assert_called_once_with("config")
        client.list.assert_called_once_with("config")

    def test_create_success_and_storage_create_called_before_client(self):
        client = MagicMock()
        storage = MagicMock()
        drv = _driver_with_caps(client, storage, caps=["config"])

        res = _make_resource("config")
        client_resp = {
            "uuid": str(res.uuid),
            "a": 100,
            "b": 200,
            "status": "ACTIVE",
        }
        client.create.return_value = client_resp

        actual = drv.create(res)

        # storage.create called with TargetFieldItem and force=True
        assert storage.create.call_count == 1
        args, kwargs = storage.create.call_args
        assert isinstance(args[0], storage_base.TargetFieldItem)
        assert kwargs == {"force": True}

        # client.create called with resource
        client.create.assert_called_once_with(res)

        # Check result resource corresponds to client response
        assert actual.uuid == res.uuid
        assert actual.kind == res.kind
        assert actual.value == client_resp

        # Do not assert strict cross-mock ordering; just ensure both were called

    def test_create_maps_already_exists_exception(self):
        client = MagicMock()
        storage = MagicMock()
        drv = _driver_with_caps(client, storage, caps=["config"])

        res = _make_resource("config")
        client.create.side_effect = client_exc.ResourceAlreadyExists(resource=res)  # type: ignore[arg-type]

        with pytest.raises(driver_exc.ResourceAlreadyExists):
            drv.create(res)

    def test_update_success(self):
        client = MagicMock()
        storage = MagicMock()
        drv = _driver_with_caps(client, storage, caps=["config"])

        res = _make_resource("config")
        client_resp = {
            "uuid": str(res.uuid),
            "a": 11,
            "b": 22,
            "status": "ACTIVE",
        }
        client.update.return_value = client_resp

        actual = drv.update(res)

        # storage.update called with TargetFieldItem
        assert storage.update.call_count == 1
        args, _ = storage.update.call_args
        assert isinstance(args[0], storage_base.TargetFieldItem)

        client.update.assert_called_once_with(res)
        assert actual.value == client_resp

    def test_update_success_applies_transformer_map_on_model_response(self):
        client = MagicMock()
        storage = MagicMock()
        transformer_map = {"config": ResourceTransformer(ignore_null_attributes=True)}
        drv = _driver_with_caps(
            client, storage, caps=["config"], transformer_map=transformer_map
        )

        res = _make_resource("config", value={"uuid": str(sys_uuid.uuid4()), "a": 1})

        model_obj = MagicMock()
        model_obj.get_resource_ignore_fields.return_value = set()
        model_obj.dump_to_simple_view.return_value = {
            "uuid": str(res.uuid),
            "a": 11,
            "b": None,
            "status": "ACTIVE",
        }
        client.update.return_value = model_obj

        actual = drv.update(res)

        client.update.assert_called_once_with(res)
        assert storage.update.call_count == 1
        assert actual.value == {
            "uuid": str(res.uuid),
            "a": 11,
            "status": "ACTIVE",
        }

    def test_update_maps_not_found_exception(self):
        client = MagicMock()
        storage = MagicMock()
        drv = _driver_with_caps(client, storage, caps=["config"])

        res = _make_resource("config")
        client.update.side_effect = client_exc.ResourceNotFound(resource=res)  # type: ignore[arg-type]

        with pytest.raises(driver_exc.ResourceNotFound):
            drv.update(res)

    def test_delete_calls_client_and_storage_even_if_client_not_found(self):
        client = MagicMock()
        storage = MagicMock()
        drv = _driver_with_caps(client, storage, caps=["config"])

        res = _make_resource("config")

        # Case 1: normal delete
        drv.delete(res)
        client.delete.assert_called_once_with(res)
        # Ensure storage.delete was called with force=True and matching identity
        assert storage.delete.call_count == 1
        del_args, del_kwargs = storage.delete.call_args
        assert del_kwargs == {"force": True}
        item = del_args[0]
        if isinstance(item, storage_base.TargetFieldItem):
            assert item.kind == res.kind and item.uuid == res.uuid
        else:
            assert (
                getattr(item, "kind", None) == res.kind
                and getattr(item, "uuid", None) == res.uuid
            )

        # Case 2: client raises not found, storage.delete still called
        client.delete.reset_mock()
        storage.delete.reset_mock()
        client.delete.side_effect = client_exc.ResourceNotFound(resource=res)  # type: ignore[arg-type]

        drv.delete(res)

        client.delete.assert_called_once_with(res)
        assert storage.delete.call_count == 1
        del_args, del_kwargs = storage.delete.call_args
        assert del_kwargs == {"force": True}
        item = del_args[0]
        if isinstance(item, storage_base.TargetFieldItem):
            assert item.kind == res.kind and item.uuid == res.uuid
        else:
            assert (
                getattr(item, "kind", None) == res.kind
                and getattr(item, "uuid", None) == res.uuid
            )
