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

import typing as tp
import uuid as sys_uuid

from restalchemy.dm import properties
from restalchemy.dm import types

from gcl_sdk.agents.universal.dm import models
from gcl_sdk.agents.universal.drivers import exceptions as driver_exc
from gcl_sdk.agents.universal.drivers import meta


def _make_resource(
    kind: str, uuid: tp.Optional[sys_uuid.UUID] = None, value: tp.Optional[dict] = None
) -> models.Resource:
    uuid = uuid or sys_uuid.uuid4()
    value = value or {"uuid": str(uuid), "foo": 1}
    return models.Resource.from_value(
        value, kind, target_fields=frozenset(value.keys())
    )


class DummyCoordinatorModel(meta.MetaCoordinatorDataPlaneModel):
    """A lightweight DP model for testing coordinator driver flows."""

    call_log: tp.Dict[str, tp.List[str]] = {}

    foo = properties.property(types.Integer(), default=0)
    invalid_dp = properties.property(types.Boolean(), default=False)

    def get_meta_model_fields(self) -> tp.Optional[tp.Set[str]]:
        return None

    def _log(self, action: str) -> None:
        self.call_log.setdefault(str(self.uuid), []).append(action)

    def dump_to_dp(self, **kwargs) -> None:
        self._log("dump_to_dp")

    def restore_from_dp(self, **kwargs) -> None:
        if getattr(self, "invalid_dp", False):
            raise driver_exc.InvalidDataPlaneObjectError(obj={"uuid": str(self.uuid)})
        self._log("restore_from_dp")

    def delete_from_dp(self, **kwargs) -> None:
        self._log("delete_from_dp")

    def update_on_dp(self, **kwargs) -> None:
        self._log("update_on_dp")


class _CoordinatorDriver(meta.MetaCoordinatorAgentDriver):
    __model_map__ = {"dummy": DummyCoordinatorModel}
    __coordinator_map__ = {}


class TestMetaCoordinatorDriver:
    def test_delete_success(self, tmp_path):
        meta_file = tmp_path / "meta.json"
        drv = _CoordinatorDriver(meta_file=str(meta_file))
        drv.start()

        res = _make_resource("dummy")
        drv.create(res)
        assert str(res.uuid) in drv._storage["dummy"]["resources"]

        drv.delete(res)

        log = DummyCoordinatorModel.call_log[str(res.uuid)]
        assert "delete_from_dp" in log
        assert str(res.uuid) not in drv._storage["dummy"]["resources"]

    def test_delete_never_created_does_not_raise_or_crash(self, tmp_path):
        meta_file = tmp_path / "meta.json"
        drv = _CoordinatorDriver(meta_file=str(meta_file))
        drv.start()

        # The resource was never created. Deleting it should be a safe
        # no-op (self-healing behavior) instead of crashing with a
        # KeyError.
        res = _make_resource("dummy")
        drv.delete(res)

        assert str(res.uuid) not in drv._storage["dummy"]["resources"]

    def test_delete_twice_does_not_raise_a_key_error(self, tmp_path):
        meta_file = tmp_path / "meta.json"
        drv = _CoordinatorDriver(meta_file=str(meta_file))
        drv.start()

        res = _make_resource("dummy")
        drv.create(res)

        drv.delete(res)
        assert str(res.uuid) not in drv._storage["dummy"]["resources"]

        # Must not raise a KeyError on the second delete attempt
        drv.delete(res)
        assert str(res.uuid) not in drv._storage["dummy"]["resources"]

    def test_delete_invalid_dp_object_is_still_removed_from_meta(self, tmp_path):
        meta_file = tmp_path / "meta.json"
        drv = _CoordinatorDriver(meta_file=str(meta_file))
        drv.start()

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
