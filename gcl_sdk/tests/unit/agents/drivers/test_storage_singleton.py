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
import os
from unittest.mock import patch

from gcl_sdk.agents.universal.storage import common as storage_common


def test_storage_singleton_get_instance(tmp_path):
    meta_file = tmp_path / "test_meta.json"

    instance1 = storage_common.JsonFileStorageSingleton.get_instance(meta_file)
    instance1["test_storage_data"] = 2
    instance2 = storage_common.JsonFileStorageSingleton.get_instance(meta_file)

    assert "test_storage_data" in instance2
    assert instance1 is instance2
    assert isinstance(instance1, storage_common.JsonFileStorageSingleton)


def test_storage_singleton_get_instance_different(tmp_path):
    meta_file = tmp_path / "test_meta.json"
    meta_file2 = tmp_path / "test_meta2.json"

    instance1 = storage_common.JsonFileStorageSingleton.get_instance(meta_file)
    instance2 = storage_common.JsonFileStorageSingleton.get_instance(meta_file2)

    assert instance1 is not instance2
    assert isinstance(instance1, storage_common.JsonFileStorageSingleton)


def test_storage_singleton_load_non_existent_file(tmp_path):
    meta_file = tmp_path / "non_existent.json"

    with patch("os.path.exists", return_value=False):
        storage = storage_common.JsonFileStorageSingleton.get_instance(meta_file)
        storage.load()

    assert storage == {}


def test_storage_singleton_load_existing_file(tmp_path):
    meta_file = tmp_path / "existing_meta.json"
    expected_data = {"key": "value"}

    with open(meta_file, "w") as f:
        json.dump(expected_data, f)

    storage = storage_common.JsonFileStorageSingleton.get_instance(meta_file)
    storage.load()

    assert storage == expected_data


def test_storage_singleton_persist(tmp_path):
    meta_file = tmp_path / "test_persist.json"
    new_vals = {"key": "new_val"}

    with patch("os.makedirs") as mock_makedirs:
        storage = storage_common.JsonFileStorageSingleton.get_instance(meta_file)
        storage.update(new_vals)
        storage.persist()

    with open(meta_file) as f:
        data = json.load(f)

    assert data == new_vals
    mock_makedirs.assert_called_once_with(os.path.dirname(meta_file), exist_ok=True)


def test_storage_singleton_persist_overwrite(tmp_path):
    meta_file = tmp_path / "test_persist.json"
    new_vals = {"key": "new_val"}

    storage = storage_common.JsonFileStorageSingleton.get_instance(meta_file)
    storage.update(new_vals)
    storage.persist()

    with open(meta_file) as f:
        data = json.load(f)

    assert data == new_vals

    new_data = {"key": "bad_value"}
    with open(meta_file, "w") as f:
        json.dump(new_data, f)

    storage.load()

    assert storage == new_data
