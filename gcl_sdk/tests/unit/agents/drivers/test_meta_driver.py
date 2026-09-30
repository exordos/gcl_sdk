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

import json
import logging
import os
import uuid as sys_uuid

import pytest
from restalchemy.dm import properties
from restalchemy.dm import types

from gcl_sdk.agents.universal.dm import models
from gcl_sdk.agents.universal.drivers import exceptions as driver_exc
from gcl_sdk.agents.universal.drivers import meta


def _make_resource(
    kind: str, uuid: sys_uuid.UUID | None = None, value: dict | None = None
) -> models.Resource:
    uuid = uuid or sys_uuid.uuid4()
    value = value or {"uuid": str(uuid), "foo": 1}
    return models.Resource.from_value(
        value, kind, target_fields=frozenset(value.keys())
    )


class DummyModel(meta.MetaDataPlaneModel):
    """A lightweight DP model for testing meta driver flows.

    It records calls to DP-like methods in a class-level log.
    """

    call_log: dict[str, list[str]] = {}

    # Custom fields that may appear in resource.value
    foo = properties.property(types.Integer(), default=0)
    raise_on_restore = properties.property(types.Boolean(), default=False)
    invalid_dp = properties.property(types.Boolean(), default=False)

    def get_meta_model_fields(self) -> set[str] | None:
        # Save all fields into meta file (including any service flags)
        return None

    def _log(self, action: str) -> None:
        self.call_log.setdefault(str(self.uuid), []).append(action)

    def dump_to_dp(self, coordinator=None) -> None:  # create
        self._log("dump_to_dp")

    def restore_from_dp(self, coordinator=None) -> None:  # get/list
        # Optional flag to simulate not-found on DP for this object
        if getattr(self, "raise_on_restore", False):
            raise driver_exc.ResourceNotFound(
                resource=models.Resource.from_value(
                    {"uuid": str(self.uuid)}, "dummy", frozenset({"uuid"})
                )
            )
        # Optional flag to simulate invalid DP object
        if getattr(self, "invalid_dp", False):
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})
        self._log("restore_from_dp")

    def delete_from_dp(self, coordinator=None) -> None:
        self._log("delete_from_dp")

    def update_on_dp(self, coordinator=None) -> None:
        self._log("update_on_dp")


class _Driver(meta.MetaFileStorageAgentDriver):
    __model_map__ = {"dummy": DummyModel}


class TestMetaDriver:
    def test_finalize_persists_meta_file(self, tmp_path):
        meta_file = tmp_path / "meta.json"
        drv = _Driver(meta_file=str(meta_file))

        # Initially empty storage
        assert drv._storage == {"dummy": {"resources": {}}}

        # Create one resource to populate meta storage
        res = _make_resource("dummy")
        drv.create(res)

        # Persist to file
        drv.finalize()

        assert os.path.exists(meta_file)
        with open(meta_file) as f:
            data = json.load(f)
        assert "dummy" in data and "resources" in data["dummy"]
        assert str(res.uuid) in data["dummy"]["resources"]

    def test_get_unsupported_kind_raises(self, tmp_path):
        meta_file = tmp_path / "meta.json"
        drv = _Driver(meta_file=str(meta_file))

        res = _make_resource("other")
        with pytest.raises(TypeError):
            drv.get(res)

    def test_get_not_found_raises(self, tmp_path):
        meta_file = tmp_path / "meta.json"
        drv = _Driver(meta_file=str(meta_file))

        res = _make_resource("dummy")
        with pytest.raises(driver_exc.ResourceNotFound):
            drv.get(res)

    def test_create_and_get_success(self, tmp_path):
        meta_file = tmp_path / "meta.json"
        drv = _Driver(meta_file=str(meta_file))

        res = _make_resource("dummy")
        created = drv.create(res)

        assert created.uuid == res.uuid
        assert created.kind == res.kind

        fetched = drv.get(res)
        assert fetched.uuid == res.uuid
        assert fetched.kind == res.kind

        # Ensure DP methods were called
        log = DummyModel.call_log[str(res.uuid)]
        assert "dump_to_dp" in log
        assert "restore_from_dp" in log

    def test_create_already_exists_maps_exception(self, tmp_path):
        meta_file = tmp_path / "meta.json"
        drv = _Driver(meta_file=str(meta_file))

        res = _make_resource("dummy")
        drv.create(res)

        with pytest.raises(driver_exc.ResourceAlreadyExists):
            drv.create(res)

    def test_create_recreates_on_invalid_dp_object(self, tmp_path):
        meta_file = tmp_path / "meta.json"
        drv = _Driver(meta_file=str(meta_file))

        res = _make_resource("dummy")
        drv.create(res)

        # Mark the existing meta entry as invalid so that get() will raise
        drv._storage["dummy"]["resources"][str(res.uuid)]["invalid_dp"] = True

        # Should not raise ResourceAlreadyExists; should proceed to recreate
        created_again = drv.create(res)

        assert created_again.uuid == res.uuid
        assert created_again.kind == res.kind

        # Ensure DP create called twice
        log = DummyModel.call_log[str(res.uuid)]
        assert log.count("dump_to_dp") >= 2

    def test_list_skips_objects_not_on_dp(self, tmp_path):
        meta_file = tmp_path / "meta.json"
        drv = _Driver(meta_file=str(meta_file))

        # Create two resources, one will be marked to raise on restore
        uuid1, uuid2 = sys_uuid.uuid4(), sys_uuid.uuid4()
        res1 = _make_resource("dummy", uuid=uuid1, value={"uuid": str(uuid1), "foo": 1})
        res2 = _make_resource(
            "dummy",
            uuid=uuid2,
            value={"uuid": str(uuid2), "foo": 2, "raise_on_restore": True},
        )

        drv.create(res1)
        drv.create(res2)

        resources = drv.list("dummy")

        # Only the one that could be restored from DP should be returned
        assert len(resources) == 1
        assert resources[0].uuid == uuid1
        assert resources[0].kind == "dummy"

    def test_list_skips_invalid_dp_objects(self, tmp_path):
        meta_file = tmp_path / "meta.json"
        drv = _Driver(meta_file=str(meta_file))

        # Create two resources, one will be marked invalid on DP
        uuid1, uuid2 = sys_uuid.uuid4(), sys_uuid.uuid4()
        res1 = _make_resource("dummy", uuid=uuid1, value={"uuid": str(uuid1), "foo": 1})
        res2 = _make_resource(
            "dummy",
            uuid=uuid2,
            value={"uuid": str(uuid2), "foo": 2, "invalid_dp": True},
        )

        drv.create(res1)
        drv.create(res2)

        resources = drv.list("dummy")

        # Only the valid object should be returned
        assert len(resources) == 1
        assert resources[0].uuid == uuid1
        assert resources[0].kind == "dummy"

    def test_update_success(self, tmp_path):
        meta_file = tmp_path / "meta.json"
        drv = _Driver(meta_file=str(meta_file))

        res = _make_resource("dummy")

        drv.create(res)

        # Update with new value
        new_value = {"uuid": str(res.uuid), "foo": 42}
        new_res = models.Resource.from_value(
            new_value, "dummy", target_fields=frozenset(new_value.keys())
        )

        updated = drv.update(new_res)

        assert updated.uuid == res.uuid
        assert updated.value["foo"] == 42

        # Ensure update_on_dp called and meta was refreshed
        log = DummyModel.call_log[str(res.uuid)]
        assert "update_on_dp" in log

        # The meta storage should contain updated value
        meta_obj = drv._storage["dummy"]["resources"][str(res.uuid)]
        assert meta_obj.get("foo") == 42

    def test_update_not_found_maps_exception(self, tmp_path):
        meta_file = tmp_path / "meta.json"
        drv = _Driver(meta_file=str(meta_file))

        # Not created in meta -> should raise
        res = _make_resource("dummy")
        with pytest.raises(driver_exc.ResourceNotFound):
            drv.update(res)

    def test_delete_success(self, tmp_path):
        meta_file = tmp_path / "meta.json"
        drv = _Driver(meta_file=str(meta_file))

        res = _make_resource("dummy")
        drv.create(res)

        # Ensure present in meta
        assert str(res.uuid) in drv._storage["dummy"]["resources"]

        drv.delete(res)

        # Ensure DP delete was called and meta entry removed
        log = DummyModel.call_log[str(res.uuid)]
        assert "delete_from_dp" in log
        assert str(res.uuid) not in drv._storage["dummy"]["resources"]

    def test_delete_unsupported_kind_raises(self, tmp_path):
        meta_file = tmp_path / "meta.json"
        drv = _Driver(meta_file=str(meta_file))

        res = _make_resource("other")
        with pytest.raises(TypeError):
            drv.delete(res)

    def test_delete_never_created_does_not_raise_or_crash(self, tmp_path):
        meta_file = tmp_path / "meta.json"
        drv = _Driver(meta_file=str(meta_file))

        # The resource was never created. Deleting it should be a safe
        # no-op (self-healing behavior) instead of crashing with a
        # KeyError.
        res = _make_resource("dummy")
        drv.delete(res)

        assert str(res.uuid) not in drv._storage["dummy"]["resources"]

    def test_delete_twice_does_not_raise_a_key_error(self, tmp_path):
        meta_file = tmp_path / "meta.json"
        drv = _Driver(meta_file=str(meta_file))

        res = _make_resource("dummy")
        drv.create(res)

        drv.delete(res)
        assert str(res.uuid) not in drv._storage["dummy"]["resources"]

        # Deleting an already deleted resource must not raise a KeyError
        drv.delete(res)
        assert str(res.uuid) not in drv._storage["dummy"]["resources"]

    def test_delete_invalid_dp_object_is_still_removed_from_meta(self, tmp_path):
        meta_file = tmp_path / "meta.json"
        drv = _Driver(meta_file=str(meta_file))

        uuid = sys_uuid.uuid4()
        res = _make_resource(
            "dummy",
            uuid=uuid,
            value={"uuid": str(uuid), "foo": 1, "invalid_dp": True},
        )
        drv.create(res)
        assert str(uuid) in drv._storage["dummy"]["resources"]

        # Should not raise even though restore_from_dp() reports the
        # object as invalid; the meta entry must still be cleaned up.
        drv.delete(res)

        assert str(uuid) not in drv._storage["dummy"]["resources"]


def test_a_field_this_agent_does_not_have_is_skipped_not_fatal(caplog):
    """An agent older than the core it talks to is an ordinary state during
    an upgrade, and it used to take the whole capability down: the model is
    built from the resource's keys, so one name it did not have failed every
    resource of that kind -- with a traceback naming
    `get_custom_property_type` rather than the field."""
    uuid = sys_uuid.uuid4()
    res = _make_resource(
        "dummy", uuid, {"uuid": str(uuid), "foo": 7, "routes": [{"to": "10.0.0.0/8"}]}
    )

    with caplog.at_level(logging.WARNING):
        obj = DummyModel.from_ua_resource(res)

    assert obj.foo == 7, "what the agent does understand is applied"
    assert not hasattr(obj, "routes")
    # ... and the field it could not read is named, because it is in the
    # target hash the control plane computed and nothing here can put it
    # back: the resource will be applied for ever without ever matching.
    assert "routes" in caplog.text
    assert "routes" not in obj.get_resource_target_fields()


class NewerModel(DummyModel):
    """The same agent one release later: it has learned a field."""

    driver_spec = properties.property(types.String(), default="")


def test_an_agent_newer_than_its_core_still_settles(caplog):
    """The other half of a rolling upgrade, and the half that goes quiet.

    Agents are updated before or after the core, never with it, so for a
    while an agent knows a field the core does not send yet. Nothing fails
    then -- the model just takes its default -- which is exactly what makes
    it dangerous: if the agent counted that default into the hash it
    reports, it would disagree with a target that never mentioned the field,
    and the resource would be re-applied for ever while every apply
    succeeded. That is a live installation burning an iteration per pass on
    a port nobody touched, and it shows up as load rather than as an error.

    So the hash the agent computes must be over the fields the *core* sent,
    which is what `target_fields` carries from one side to the other.
    """
    uuid = sys_uuid.uuid4()
    from_the_core = _make_resource("dummy", uuid, {"uuid": str(uuid), "foo": 7})

    with caplog.at_level(logging.WARNING):
        obj = NewerModel.from_ua_resource(from_the_core)
    reported = obj.to_ua_resource("dummy")

    assert obj.driver_spec == "", "a field the core never sent took a default"
    assert reported.hash == from_the_core.hash, (
        "the agent reports a hash the core cannot match, so this resource is "
        "re-applied on every pass for as long as the two versions differ"
    )
    # And it says nothing: an agent ahead of its core is the ordinary
    # direction of an upgrade and has nothing to report.
    assert not caplog.text.strip()


def test_a_field_the_agent_lacks_is_named_because_it_will_not_settle(caplog):
    """The asymmetry, stated as a measurement rather than a comment.

    The opposite skew cannot be repaired here: the missing field is part of
    the hash the core computed, and an agent that never knew the field
    cannot reproduce it. The resource is applied as far as it is understood
    and then applied again for ever. That is why that path warns and this
    one does not -- and it is worth a case of its own, so that a change
    which makes it settle silently (by hashing over the agent's own view,
    say) fails here instead of hiding a divergence.
    """
    uuid = sys_uuid.uuid4()
    from_the_core = _make_resource(
        "dummy", uuid, {"uuid": str(uuid), "foo": 7, "routes": []}
    )

    with caplog.at_level(logging.WARNING):
        reported = DummyModel.from_ua_resource(from_the_core).to_ua_resource("dummy")

    assert reported.hash != from_the_core.hash, (
        "the agent claims to match a target containing a field it cannot "
        "read; a resource that is not being applied in full must not look "
        "settled"
    )
    assert "routes" in caplog.text, "the field that blocks convergence is not named"
