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

import base64
import configparser
import importlib
import json
import os
import sys
import typing as tp
import uuid as sys_uuid

import xxhash

from gcl_sdk.agents.universal import constants as c
from gcl_sdk.clients.http import base as http_base


def system_uuid(
    system_uuid_path: str = "/sys/class/dmi/id/product_uuid",
) -> sys_uuid.UUID:
    """Return system uuid"""
    with open(system_uuid_path) as f:
        return sys_uuid.UUID(f.read().strip())


def node_uuid(
    node_path: str = c.NODE_UUID_PATH, use_machine_if_absent: bool = True
) -> sys_uuid.UUID:
    """Return node uuid"""
    if os.path.exists(node_path):
        with open(node_path) as f:
            return sys_uuid.UUID(f.read().strip())

    if use_machine_if_absent:
        return system_uuid()

    raise FileNotFoundError(f"The node-id location {node_path} not found")


def calculate_hash(
    value: dict, hash_method: tp.Callable[[str], str] = xxhash.xxh3_64
) -> str:
    m = hash_method()
    m.update(json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return m.hexdigest()


def extract_target_value(
    value: dict[str, tp.Any],
    target_fields: tp.Collection[str],
    strict: bool = True,
) -> dict[str, tp.Any]:
    """Extract a nested subset of ``value`` selected by ``target_fields``.

    Each item in ``target_fields`` is either a plain top-level key
    (``"setter"``), which selects the whole value under that key, or a
    dot-separated path (``"setter.kind"``), which selects only that
    nested key inside the top-level dict. If both a plain key and dotted
    paths are given for the same top-level key, the plain key wins and
    the whole nested value is included.

    If ``strict`` is True (default), a missing plain top-level key raises
    ``KeyError``, matching a plain dict-comprehension lookup. Nested keys
    (dotted paths, at any depth) are always skipped when missing, since a
    nested object legitimately may not have every possible sub-field set.
    """
    plain_fields = [f for f in target_fields if "." not in f]
    nested_fields = [f for f in target_fields if "." in f]

    if strict:
        result = {f: value[f] for f in plain_fields}
    else:
        result = {f: value[f] for f in plain_fields if f in value}

    nested_paths: dict[str, list[str]] = {}
    for field in nested_fields:
        top, _, rest = field.partition(".")
        nested_paths.setdefault(top, []).append(rest)

    for top, sub_paths in nested_paths.items():
        if top in result:
            # The whole value is already selected via a plain field.
            continue
        nested_value = value.get(top)
        if isinstance(nested_value, dict):
            result[top] = extract_target_value(nested_value, sub_paths, strict=False)

    return result


def cfg_load_class(model_path: str) -> type:
    """Load class from config file.

    Model path format: <module>:<class>
    Example: gcl_sdk.infra.dm.models:Node
    """
    if ":" not in model_path:
        raise ValueError(f"Invalid model path: {model_path}")

    module_path, class_name = model_path.split(":", 1)

    # Import the module if it's not already loaded
    if module_path not in sys.modules:
        try:
            module = importlib.import_module(module_path)
        except ImportError:
            raise ValueError(f"Module {module_path} not found")
    else:
        module = sys.modules[module_path]

    try:
        class_model = getattr(module, class_name)
    except AttributeError:
        raise ValueError(f"Class {class_name} not found in module {module_path}")

    return class_model


def cfg_load_section_map(config_file: str, section: str) -> dict[str, str]:
    """Load section map from config file

    Example:
    [section]
    option1 = value1
    option2 = value2

    Returns: {"option1": "value1", "option2": "value2"}
    """
    params = {}
    parser = configparser.ConfigParser()
    parser.read(config_file)

    if not parser.has_section(section):
        return params

    for option in parser.options(section):
        if option in parser.defaults():
            continue

        params[option] = parser.get(section, option)

    return params


def get_encryptor(private_key_path: str) -> http_base.Encryptor:
    if not os.path.exists(private_key_path):
        raise FileNotFoundError(f"Private key file not found: {private_key_path}")

    with open(private_key_path) as f:
        private_key_base64 = f.read()

    private_key = base64.b64decode(private_key_base64)
    node = system_uuid()

    return http_base.Encryptor(private_key, node)
