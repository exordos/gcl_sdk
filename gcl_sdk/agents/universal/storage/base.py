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

    `target_fields` is what ``Resource.from_value`` / ``replace_value``
    filter the hash by. It is either a collection of plain names / dot
    separated paths (``"setter.kind"``) as returned by
    ``DirectAgentDriver.get_resource_target_fields``, or a mapping -- the
    key skeleton of the declared value (`utils.value_shape`) -- which is
    what lets the nested fields the data plane adds be dropped too when
    no paths were declared. A mapping is what the direct driver derives
    when nobody declared the fields explicitly.

    A dict cannot be hashed, so carrying a mapping would have cost the
    item the hashability every other field gave it -- and this is a
    storage type, handed out of `list()` into whatever a driver keeps it
    in. `__hash__` below buys that back.
    """

    kind: str
    uuid: sys_uuid.UUID
    target_fields: frozenset[str] | dict[str, tp.Any]

    def __hash__(self) -> int:
        """Hash the target fields by their canonical JSON.

        A mapping is encoded directly; a collection is sorted first, so
        two collections with the same members in a different order
        produce the same hash. ``sort_keys`` makes the encoding depend
        on nothing but the keys for a mapping. Items that compare equal
        have equal fields and so hash equal, which is the whole contract.
        """
        if isinstance(self.target_fields, tp.Mapping):
            canonical = json.dumps(
                self.target_fields, separators=(",", ":"), sort_keys=True
            )
        else:
            canonical = json.dumps(sorted(self.target_fields), separators=(",", ":"))

        return hash((self.kind, self.uuid, canonical))

    @classmethod
    def from_ua_resource(cls, resource: models.Resource) -> TargetFieldItem:
        return cls(
            resource.kind,
            resource.uuid,
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
