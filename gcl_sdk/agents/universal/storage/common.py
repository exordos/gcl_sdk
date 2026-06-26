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

import json
import os
from pathlib import Path
import threading

from gcl_sdk.common import utils


class JsonFileStorageSingleton(dict):
    _instances = {}
    _lock = threading.Lock()

    def __init__(self, storage_path: str):
        self._storage_path = Path(storage_path)
        super().__init__()
        self.load()

    def load(self) -> None:
        """Load the storage from the file."""
        if not os.path.exists(self._storage_path):
            self.clear()
            return

        with open(self._storage_path) as f:
            data = json.load(f)

        self.clear()
        self.update(data)

    def persist(self) -> None:
        """Persist the storage to the file."""

        # Create the directory if it doesn't exist
        os.makedirs(os.path.dirname(self._storage_path), exist_ok=True)

        # Save the new data
        tmp_file = self._storage_path.with_suffix(".tmp")
        with open(tmp_file, "w", opener=utils.rw_owner_opener) as f:
            json.dump(self, f, indent=2)
        os.replace(tmp_file, self._storage_path)

    @classmethod
    def get_instance(cls, storage_path: str) -> "JsonFileStorageSingleton":
        """Get or create a singleton instance for the given storage path."""
        if storage_path in cls._instances:
            return cls._instances[storage_path]
        with cls._lock:
            if storage_path not in cls._instances:
                cls._instances[storage_path] = cls(storage_path)
            return cls._instances[storage_path]
