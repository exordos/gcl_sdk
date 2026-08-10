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


class TargetFieldsFileStorage(base.AbstractTargetFieldsStorage):
    """Target fields JSON file storage.

    It stores the target fields in a JSON file.
    The file structure is the following:
    {kind: {uuid: fields}}
    """

    def __init__(self, storage_path: str) -> None:
        self._storage = common.JsonFileStorageSingleton.get_instance(storage_path)

    def get(self, kind: str, uuid: sys_uuid.UUID) -> base.TargetFieldItem:
        """Get the target fields item from the storage."""
        try:
            fields = self._storage[kind][str(uuid)]
        except KeyError:
            raise se.ItemNotFound(item=base.TargetFieldItem(kind, uuid, frozenset()))

        return base.TargetFieldItem(kind, uuid, frozenset(fields))

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

        self._storage.setdefault(item.kind, {})[str(item.uuid)] = list(item.fields)
        return item

    def update(self, item: base.TargetFieldItem) -> base.TargetFieldItem:
        """Update the target fields item in the storage."""
        return self.create(item, force=True)

    def list(self, kind: str) -> list[base.TargetFieldItem]:
        """Lists all target fields items of a resource kind."""
        return [
            base.TargetFieldItem(kind, sys_uuid.UUID(uuid), frozenset(fields))
            for uuid, fields in self._storage.get(kind, {}).items()
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

    def storage(self) -> dict[str, dict[str, list[str]]]:
        """Return the raw storage."""
        return self._storage
