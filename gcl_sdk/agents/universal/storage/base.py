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

import abc
import json
import typing as tp
import uuid as sys_uuid

from gcl_sdk.agents.universal import utils
from gcl_sdk.agents.universal.dm import models


class TargetFieldItem(tp.NamedTuple):
    """The target fields of one resource.

    `fields` are the top-level names the control plane declared. `shape`
    is the key skeleton of the whole declared value (`utils.value_shape`),
    which is what lets the nested fields the data plane adds be dropped
    too. It is optional: an item written by an older agent has none, and
    the top-level names are then all there is to filter with.

    A dict cannot be hashed, so carrying the shape would have cost the
    item the hashability every other field gave it -- and this is a
    storage type, handed out of `list()` into whatever a driver keeps it
    in. `__hash__` below buys that back.
    """

    kind: str
    uuid: sys_uuid.UUID
    fields: frozenset[str]
    shape: dict[str, tp.Any] | None = None

    def __hash__(self) -> int:
        """Hash the shape by its canonical JSON.

        The shape is written to a JSON file, so it is always JSON-able,
        and `sort_keys` makes the encoding depend on nothing but the
        keys. Items that compare equal have equal shapes and so hash
        equal, which is the whole contract.
        """
        canonical = (
            None
            if self.shape is None
            else json.dumps(self.shape, separators=(",", ":"), sort_keys=True)
        )

        return hash((self.kind, self.uuid, self.fields, canonical))

    @classmethod
    def from_ua_resource(cls, resource: models.Resource) -> TargetFieldItem:
        return cls(
            resource.kind,
            resource.uuid,
            frozenset(resource.value.keys()),
            utils.value_shape(resource.value),
        )


class AbstractTargetFieldsStorage(abc.ABC):
    """Abstract target fields storage.

    Abstract class that represents a storage for target fields.
    UUID of an item is unique across the kind.
    """

    @abc.abstractmethod
    def get(self, kind: str, uuid: sys_uuid.UUID) -> TargetFieldItem:
        """Get the target fields item from the storage."""

    @abc.abstractmethod
    def create(
        self,
        item: TargetFieldItem,
        force: bool = False,
    ) -> TargetFieldItem:
        """Creates the target fields item in the storage."""

    @abc.abstractmethod
    def update(self, item: TargetFieldItem) -> TargetFieldItem:
        """Update the target fields item in the storage."""

    @abc.abstractmethod
    def list(self, kind: str) -> list[TargetFieldItem]:
        """Lists all target fields items of a resource kind."""

    @abc.abstractmethod
    def delete(self, item: TargetFieldItem, force: bool = False) -> None:
        """Delete the target fields item from the storage."""

    @abc.abstractmethod
    def load(self) -> None:
        """Load the storage."""

    @abc.abstractmethod
    def persist(self) -> None:
        """Persist the storage."""

    @abc.abstractmethod
    def storage(self) -> tp.Any:
        """Return the raw storage."""
