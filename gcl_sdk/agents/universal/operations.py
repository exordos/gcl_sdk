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
"""Interruptible long-term operations.

Some data plane changes take longer than a single agent iteration, for
instance a graceful shutdown of a virtual machine. Waiting for them in the
agent's main loop would stall every other resource, so such changes are
split into short steps instead. Each step sends a signal to the data plane,
writes what it did into the operation's journal and returns. The next
iteration calls the operation again, if the resource still has not reached
its target, and the operation decides what to do next by its journal.

All steps run in the agent's main thread. This keeps the model simple:

- no locks, all data is accessed from one thread;
- operations follow the reconciliation loop, no `while True` inside;
- the latest target always wins, conflicting operations are just dropped
  from the catalog instead of joining threads.

The price is that a step must be fast.
"""

from __future__ import annotations

import abc
import dataclasses
import datetime
import enum
import logging
import typing as tp
import uuid as sys_uuid

LOG = logging.getLogger(__name__)

# How long to remember finished operations, for instance to enforce
# a cooldown between two reboots of the same machine.
DEFAULT_HISTORY_TTL = datetime.timedelta(hours=1)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class OpStatus(str, enum.Enum):
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"
    FAILED = "FAILED"


@dataclasses.dataclass
class Operation(abc.ABC):
    """An operation: the logic of its steps and the journal of them.

    Specific operations declare their arguments as dataclass fields.
    The data plane context of the current iteration, for instance a driver
    or a freshly fetched data plane object, is passed to `step` instead of
    being kept in the operation since it's valid only for one iteration.
    """

    # The unique key of the operation within an entity
    op_key: tp.ClassVar[str] = ""

    # Operations of the same group are mutually exclusive for an entity.
    # Starting an operation drops other operations of its group.
    group: tp.ClassVar[str | None] = None

    journal: list[dict[str, tp.Any]] = dataclasses.field(default_factory=list)
    status: OpStatus = OpStatus.IN_PROGRESS

    @property
    def finished(self) -> bool:
        return self.status != OpStatus.IN_PROGRESS

    def log(self, action: str, **extra: tp.Any) -> dict[str, tp.Any]:
        """Write the action into the journal."""
        record = {"action": action, "dt": _now(), **extra}
        self.journal.append(record)
        return record

    def last(self, action: str) -> dict[str, tp.Any] | None:
        """Return the latest journal record of the action if any."""
        for record in reversed(self.journal):
            if record["action"] == action:
                return record
        return None

    def since(self, action: str) -> datetime.timedelta | None:
        """Return the time passed since the latest record of the action."""
        record = self.last(action)
        if record is None:
            return None
        return _now() - record["dt"]

    @abc.abstractmethod
    def step(self, **ctx: tp.Any) -> OpStatus:
        """Perform the next short step of the operation.

        It's called on every agent iteration while the operation is in
        progress. It must not block, send a signal to the data plane,
        write it into the journal and return.
        """


OpT = tp.TypeVar("OpT", bound=Operation)


class OperationCatalog:
    """The catalog of operations per entity.

    Example of the catalog content:
    {
        UUID("138cb1ea-2ce5-445e-9da8-78fe8652f213"): {
            "reboot": RebootMachineOperation(
                not_before=None,
                journal=[
                    {"action": "started", "dt": ...},
                    {"action": "send_shutdown", "dt": ...},
                    {"action": "send_start", "dt": ...},
                ],
            ),
        },
    }

    The catalog follows the agent iteration: `start_iteration` and
    `finish_iteration` are called around it.

    Finished operations stay in the catalog until another operation of the
    same group replaces them or the entity is not touched for a whole
    iteration. It allows the owner to know an operation has been finished.

    Besides, the catalog remembers when operations were finished for
    `history_ttl`. A finished operation is often replaced by the next one
    quickly, for instance a finished reboot by the power on, but the time
    of the last reboot is still needed to keep a pause before the next one.
    """

    def __init__(self, history_ttl: datetime.timedelta = DEFAULT_HISTORY_TTL) -> None:
        self._catalog: dict[sys_uuid.UUID, dict[str, Operation]] = {}
        self._finished_at: dict[tuple[sys_uuid.UUID, str], datetime.datetime] = {}
        self._touched: set[sys_uuid.UUID] = set()
        self._history_ttl = history_ttl

    def get(self, entity: sys_uuid.UUID, op_class: type[OpT]) -> OpT | None:
        """Return the operation of the entity if any."""
        op = self._catalog.get(entity, {}).get(op_class.op_key)
        return tp.cast(OpT | None, op)

    def last_finished_at(
        self, entity: sys_uuid.UUID, op_class: type[Operation]
    ) -> datetime.datetime | None:
        """Return when the operation was finished for the entity last time."""
        return self._finished_at.get((entity, op_class.op_key))

    def clear(self, entity: sys_uuid.UUID) -> None:
        """Forget everything about the entity, for instance it's deleted."""
        self._catalog.pop(entity, None)
        for key in tuple(self._finished_at):
            if key[0] == entity:
                del self._finished_at[key]

    def run(self, entity: sys_uuid.UUID, op: OpT, **ctx: tp.Any) -> OpT:
        """Start the operation or proceed with the existing one.

        `op` is added to the catalog only if the entity has no operation
        of this kind yet, otherwise the existing one proceeds and `op` is
        dropped. Conflicting operations of the same group are replaced.
        A finished operation is not run again, it's returned as is.
        `ctx` is passed to the `step` of the operation.
        """
        self._touched.add(entity)
        self._drop_conflicts(entity, op)

        existing = self.get(entity, type(op))
        if existing is None:
            self._add(entity, op)
        else:
            op = existing

        if op.finished:
            return op

        try:
            status = op.step(**ctx)
        except Exception as e:
            op.log("error", error=str(e))
            raise

        if status != OpStatus.IN_PROGRESS:
            op.status = status
            record = op.log(status.value.lower())
            self._finished_at[(entity, op.op_key)] = record["dt"]
            LOG.info(
                "Operation %s for %s finished with %s, journal: %s",
                op.op_key,
                entity,
                status.value,
                op.journal,
            )

        return op

    def start_iteration(self) -> None:
        """Mark the beginning of an agent iteration."""
        self._touched = set()

    def finish_iteration(self) -> None:
        """Drop operations of entities that were not touched this iteration.

        An entity that reached its target is not updated anymore, so its
        operations are not needed.
        """
        for entity in self._catalog.keys() - self._touched:
            del self._catalog[entity]
            LOG.debug("Dropped operations of untouched entity %s", entity)

        expired = _now() - self._history_ttl
        for key, finished_at in tuple(self._finished_at.items()):
            if finished_at < expired:
                del self._finished_at[key]

    def _add(self, entity: sys_uuid.UUID, op: Operation) -> None:
        op.log("started")
        self._catalog.setdefault(entity, {})[op.op_key] = op
        LOG.info("Operation %s started for %s", op.op_key, entity)

    def _drop_conflicts(self, entity: sys_uuid.UUID, op: Operation) -> None:
        ops = self._catalog.get(entity, {})
        if op.group is None:
            return

        for other in tuple(ops.values()):
            if other.group != op.group or other.op_key == op.op_key:
                continue

            del ops[other.op_key]
            if not other.finished:
                LOG.info(
                    "Operation %s for %s is replaced by %s",
                    other.op_key,
                    entity,
                    op.op_key,
                )
