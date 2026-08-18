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

import uuid as sys_uuid

from gcl_sdk.agents.universal.storage import base
from gcl_sdk.agents.universal.storage import common
from gcl_sdk.agents.universal.storage import exceptions as se


def _to_item(
    kind: str, uuid: sys_uuid.UUID, stored: dict | list
) -> base.TargetFieldItem:
    """Build an item from what the file holds, in either format."""
    shape = stored if isinstance(stored, dict) else None
    return base.TargetFieldItem(kind, uuid, frozenset(stored), shape)


class TargetFieldsFileStorage(base.AbstractTargetFieldsStorage):
    """Target fields JSON file storage.

    It stores the target fields in a JSON file.
    The file structure is the following:
    {kind: {uuid: shape}}

    `shape` is the key skeleton of the target value, whose top-level names
    are the target fields. A file written before shapes were stored holds
    a plain list of those names instead, and is read as a shapeless item.
    Both directions work: a list and a dict of the same names iterate the
    same, so an older agent reads a newer file as the name list it expects.
    """

    def __init__(self, storage_path: str) -> None:
        self._storage = common.JsonFileStorageSingleton.get_instance(storage_path)

    def get(self, kind: str, uuid: sys_uuid.UUID) -> base.TargetFieldItem:
        """Get the target fields item from the storage."""
        try:
            stored = self._storage[kind][str(uuid)]
        except KeyError:
            raise se.ItemNotFound(item=base.TargetFieldItem(kind, uuid, frozenset()))

        return _to_item(kind, uuid, stored)

    def create(
        self,
        item: base.TargetFieldItem,
        force: bool = False,
    ) -> base.TargetFieldItem:
        """Creates the target fields item in the storage."""
        try:
            self.get(item.kind, item.uuid)
        except se.ItemNotFound:
            # Desirable behavior, the item should not exist
            pass
        else:
            if not force:
                raise se.ItemAlreadyExists(item=item)

        stored = dict(item.shape) if item.shape is not None else list(item.fields)
        self._storage.setdefault(item.kind, {})[str(item.uuid)] = stored
        return item

    def update(self, item: base.TargetFieldItem) -> base.TargetFieldItem:
        """Update the target fields item in the storage."""
        return self.create(item, force=True)

    def list(self, kind: str) -> list[base.TargetFieldItem]:
        """Lists all target fields items of a resource kind."""
        return [
            _to_item(kind, sys_uuid.UUID(uuid), stored)
            for uuid, stored in self._storage.get(kind, {}).items()
        ]

    def delete(self, item: base.TargetFieldItem, force: bool = False) -> None:
        """Delete the target fields item from the storage."""
        try:
            self.get(item.kind, item.uuid)
        except se.ItemNotFound:
            if not force:
                raise
        else:
            self._storage[item.kind].pop(str(item.uuid), None)

    def load(self) -> None:
        """Load the storage."""
        # Nothing to do. It is loaded on init.

    def persist(self) -> None:
        """Persist the storage."""
        self._storage.persist()

    def storage(self) -> dict[str, dict[str, dict | list[str]]]:
        """Return the raw storage."""
        return self._storage
