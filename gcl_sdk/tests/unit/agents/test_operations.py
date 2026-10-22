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

import dataclasses
import datetime
import typing as tp
from unittest import mock
import uuid as sys_uuid

import pytest

from gcl_sdk.agents.universal import operations

NOW = datetime.datetime(2026, 10, 15, 12, 0, tzinfo=datetime.timezone.utc)


class _Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime.datetime:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += datetime.timedelta(seconds=seconds)


@pytest.fixture
def clock():
    clock = _Clock()
    with mock.patch.object(operations, "now", clock):
        yield clock


@dataclasses.dataclass
class _StepsOperation(operations.Operation):
    """Finishes after the given number of steps."""

    group: tp.ClassVar[str | None] = "power"

    steps: int = 2
    calls: int = 0

    def step(self, **ctx):
        self.calls += 1
        self.log("step", ctx=ctx)
        if self.calls >= self.steps:
            return operations.OpStatus.DONE
        return operations.OpStatus.IN_PROGRESS


def _op(op_key: str, **attrs: tp.Any) -> type[_StepsOperation]:
    return type(op_key, (_StepsOperation,), {"op_key": op_key, **attrs})


PowerOff, PowerOn, Reboot = _op("power_off"), _op("power_on"), _op("reboot")


@dataclasses.dataclass
class _FailingOperation(operations.Operation):
    op_key: tp.ClassVar[str] = "failing"

    def step(self, **ctx):
        raise RuntimeError("boom")


class TestOperation:
    def test_default_containers_are_not_shared(self):
        first = PowerOff()
        second = PowerOff()

        first.log("started")

        assert second.journal == []

    def test_last_and_since(self, clock):
        op = PowerOff()
        op.log("send")
        clock.tick(5)
        op.log("other")
        clock.tick(5)

        assert op.last("send")["dt"] == NOW
        assert op.since("send") == datetime.timedelta(seconds=10)
        assert op.last("missing") is None
        assert op.since("missing") is None


class TestOperationCatalog:
    def test_run_creates_then_proceeds(self, clock):
        catalog = operations.OperationCatalog()
        entity = sys_uuid.uuid4()
        first = catalog.run(entity, Reboot(steps=2), driver="drv")
        # A new instance is dropped, the existing operation proceeds
        second = catalog.run(entity, Reboot(steps=100))

        assert first is second
        assert first.steps == 2
        assert first.status == operations.OpStatus.DONE
        assert [r["action"] for r in first.journal] == [
            "started",
            "step",
            "step",
            "done",
        ]
        assert first.journal[1]["ctx"] == {"driver": "drv"}

    def test_finished_operation_is_not_run_again(self, clock):
        catalog = operations.OperationCatalog()
        entity = sys_uuid.uuid4()
        first = catalog.run(entity, Reboot(steps=1))

        second = catalog.run(entity, Reboot(steps=1))

        assert second is first
        assert first.calls == 1

    def test_operation_of_the_same_group_replaces_the_other(self, clock):
        catalog = operations.OperationCatalog()
        entity = sys_uuid.uuid4()
        catalog.run(entity, PowerOff(steps=5))
        catalog.run(entity, PowerOn(steps=5))

        assert catalog.get(entity, PowerOff) is None
        assert catalog.get(entity, PowerOn) is not None

    def test_operation_of_the_same_group_replaces_a_finished_one(self, clock):
        catalog = operations.OperationCatalog()
        entity = sys_uuid.uuid4()
        catalog.run(entity, Reboot(steps=1))

        catalog.run(entity, PowerOn(steps=5))

        assert catalog.get(entity, Reboot) is None

    def test_operations_of_other_entities_are_kept(self, clock):
        catalog = operations.OperationCatalog()
        first, second = sys_uuid.uuid4(), sys_uuid.uuid4()

        catalog.run(first, PowerOff(steps=5))
        catalog.run(second, PowerOn(steps=5))

        assert catalog.get(first, PowerOff) is not None
        assert catalog.get(second, PowerOn) is not None

    def test_failed_step_is_journaled_and_retried(self, clock):
        catalog = operations.OperationCatalog()
        entity = sys_uuid.uuid4()

        with pytest.raises(RuntimeError):
            catalog.run(entity, _FailingOperation())

        op = catalog.get(entity, _FailingOperation)
        assert op.status == operations.OpStatus.IN_PROGRESS
        assert op.last("error")["error"] == "boom"

    def test_sweep_drops_operation_not_requested_since_last_sweep(self, clock):
        catalog = operations.OperationCatalog()
        entity = sys_uuid.uuid4()
        catalog.run(entity, PowerOff(steps=5))

        # Requested in the previous iteration, kept
        catalog.sweep(entity)
        assert catalog.get(entity, PowerOff) is not None

        # Not requested since then, dropped
        catalog.sweep(entity)
        assert catalog.get(entity, PowerOff) is None

    def test_sweep_keeps_requested_operations(self, clock):
        catalog = operations.OperationCatalog()
        entity = sys_uuid.uuid4()
        op = catalog.run(entity, Reboot(steps=1))

        for _ in range(3):
            catalog.sweep(entity)
            catalog.run(entity, Reboot(steps=1))

        assert catalog.get(entity, Reboot) is op
        assert op.calls == 1

    def test_sweep_of_entity_leaves_other_entities(self, clock):
        catalog = operations.OperationCatalog()
        swept, other = sys_uuid.uuid4(), sys_uuid.uuid4()
        catalog.run(swept, PowerOff(steps=5))
        catalog.run(other, PowerOff(steps=5))

        catalog.sweep(swept)
        catalog.sweep(swept)

        assert catalog.get(swept, PowerOff) is None
        assert catalog.get(other, PowerOff) is not None

    def test_sweep_of_whole_catalog(self, clock):
        catalog = operations.OperationCatalog()
        touched, untouched = sys_uuid.uuid4(), sys_uuid.uuid4()
        catalog.run(touched, PowerOff(steps=5))
        catalog.run(untouched, PowerOff(steps=5))
        catalog.sweep()

        catalog.run(touched, PowerOff(steps=5))
        catalog.sweep()

        assert catalog.get(touched, PowerOff) is not None
        assert catalog.get(untouched, PowerOff) is None

    def test_clear_forgets_the_entity(self, clock):
        catalog = operations.OperationCatalog()
        entity = sys_uuid.uuid4()
        catalog.run(entity, Reboot(steps=1))

        catalog.clear(entity)

        assert catalog.get(entity, Reboot) is None
