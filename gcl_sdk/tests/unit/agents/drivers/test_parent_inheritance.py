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
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest import mock
import uuid as sys_uuid

import pytest

from gcl_sdk.agents.universal.cmd import universal_agent_db_back
from gcl_sdk.agents.universal.clients.backend.db import DatabaseBackendClient
from gcl_sdk.agents.universal.clients.backend.db import ModelSpec
from gcl_sdk.agents.universal.dm import models
from gcl_sdk.agents.universal.drivers.core import DatabaseCapabilityDriver


def _resource(
    kind: str,
    uuid: sys_uuid.UUID,
    master: sys_uuid.UUID | None = None,
) -> models.Resource:
    value = {"uuid": str(uuid), "name": "route"}
    resource = models.Resource.from_value(value, kind, frozenset(value))
    if master is not None:
        resource.master = master
    return resource


def test_payload_preserves_master_and_hashes_reparenting(tmp_path):
    resource_uuid = sys_uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    parent_uuid = sys_uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    moved_parent_uuid = sys_uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

    payload = models.Payload.empty()
    payload.add_caps_resource(_resource("route", resource_uuid, parent_uuid))
    payload.calculate_hash()
    payload_path = tmp_path / "payload.json"
    payload.save(str(payload_path), recalculate_hash=False)

    loaded = models.Payload.load(str(payload_path))
    assert loaded.caps_resources()[0].master == str(parent_uuid)

    moved_payload = models.Payload.empty()
    moved_payload.add_caps_resource(
        _resource("route", resource_uuid, moved_parent_uuid)
    )
    moved_payload.calculate_hash()
    assert loaded.hash != moved_payload.hash


def test_database_driver_sets_master_from_model_parent():
    parent_uuid = sys_uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    driver = object.__new__(DatabaseCapabilityDriver)
    driver._parent_fields = {"route": "parent"}
    driver._transformer_map = {}
    model = MagicMock()
    model.dump_to_simple_view.return_value = {
        "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "name": "route",
    }
    model.get_resource_ignore_fields.return_value = ()
    model.parent = SimpleNamespace(uuid=parent_uuid)

    resource = driver._model_to_resource("route", model)

    assert resource.master == str(parent_uuid)


def test_database_driver_detects_reparenting_with_same_resource_uuid():
    resource_uuid = sys_uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    first_parent = sys_uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    second_parent = sys_uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
    driver = object.__new__(DatabaseCapabilityDriver)
    driver._parent_fields = {"route": "parent"}

    target = _resource("route", resource_uuid, first_parent)
    actual = _resource("route", resource_uuid, second_parent)

    assert target.hash == actual.hash
    assert not driver.resources_equal(target, actual)

    actual.master = first_parent
    assert driver.resources_equal(target, actual)


def test_database_driver_orders_parent_kind_before_child(tmp_path):
    parent_model = MagicMock()
    child_model = MagicMock()
    child_model.properties.properties = {"parent": object()}

    driver = DatabaseCapabilityDriver(
        model_specs=[
            ModelSpec(
                model=child_model,
                kind="route",
                filters={},
                parent_kind="vhost",
            ),
            ModelSpec(model=parent_model, kind="vhost", filters={}),
        ],
        target_fields_storage_path=str(tmp_path / "target-fields.json"),
    )

    assert driver.get_capabilities() == ["vhost", "route"]


def test_load_model_parents_parses_field_and_parent_kind():
    with (
        mock.patch.object(
            universal_agent_db_back,
            "CONF",
            MagicMock(config_file="agent.conf"),
        ),
        mock.patch.object(
            universal_agent_db_back.ua_utils,
            "cfg_load_section_map",
            return_value={"route": "owner,vhost"},
        ),
    ):
        assert universal_agent_db_back.load_model_parents() == {
            "route": ("owner", "vhost")
        }


def test_load_model_parents_rejects_malformed_mapping():
    with (
        mock.patch.object(
            universal_agent_db_back,
            "CONF",
            MagicMock(config_file="agent.conf"),
        ),
        mock.patch.object(
            universal_agent_db_back.ua_utils,
            "cfg_load_section_map",
            return_value={"route": "owner"},
        ),
        pytest.raises(ValueError, match="expected '<field>,<parent_kind>'"),
    ):
        universal_agent_db_back.load_model_parents()


def test_database_create_uses_master_when_parent_is_omitted_from_value():
    parent_uuid = sys_uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    parent = MagicMock(uuid=parent_uuid)
    parent_model = MagicMock()
    parent_model.objects.get_one.return_value = parent

    child_model = MagicMock()
    child_model.properties.properties = {"parent": object()}
    child_model.objects.get_one_or_none.return_value = None
    child_instance = MagicMock()
    child_model.from_ua_resource.return_value = child_instance

    client = DatabaseBackendClient(
        model_specs=[
            ModelSpec(model=parent_model, kind="parent", filters={}),
            ModelSpec(
                model=child_model,
                kind="route",
                filters={},
                parent_kind="parent",
                parent_field="parent",
            ),
        ],
        tf_storage=MagicMock(),
    )
    session = object()
    client.set_session(session)
    resource = _resource(
        "route",
        sys_uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        parent_uuid,
    )

    created = client.create(resource)

    assert "parent" not in resource.value
    assert created is child_instance
    assert child_instance.parent is parent
    parent_model.objects.get_one.assert_called_once()


def test_database_update_reparents_same_uuid_and_touches_old_parent():
    resource_uuid = sys_uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    old_parent = MagicMock(
        uuid=sys_uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    )
    new_parent = MagicMock(
        uuid=sys_uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
    )
    parent_model = MagicMock()
    parent_model.objects.get_one.return_value = new_parent

    existing = MagicMock()
    existing.uuid = resource_uuid
    existing.parent = old_parent
    existing.properties = {}
    existing.is_dirty.return_value = False
    child_model = MagicMock()
    child_model.properties.properties = {"parent": object()}
    child_model.objects.get_one_or_none.return_value = existing
    child_model.from_ua_resource.return_value = MagicMock()

    client = DatabaseBackendClient(
        model_specs=[
            ModelSpec(model=parent_model, kind="parent", filters={}),
            ModelSpec(
                model=child_model,
                kind="route",
                filters={},
                parent_kind="parent",
                parent_field="parent",
            ),
        ],
        tf_storage=MagicMock(),
    )
    session = object()
    client.set_session(session)
    resource = _resource("route", resource_uuid, new_parent.uuid)

    updated = client.update(resource)

    assert resource.uuid == existing.uuid
    assert "parent" not in resource.value
    assert updated is existing
    assert existing.parent is new_parent
    existing.update.assert_called_once_with(session=session, force=True)
    old_parent.update.assert_called_once_with(session=session, force=True)
