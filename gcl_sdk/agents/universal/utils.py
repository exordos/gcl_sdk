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


def value_shape(value: tp.Any) -> tp.Any:
    """Return the key skeleton of `value`, without any of its leaves.

    Target fields are the top-level names the control plane declared, and
    they are all `replace_value` needs to strip the extra top-level fields
    the data plane adds. Nested fields it cannot help with: a default the
    data plane fills in *inside* a declared dict or list -- an endpoint's
    `weight`, a route condition's `allowed_ips` -- lands in the target
    hash, never matches, and the resource never settles.

    So the shape is kept alongside the names. Only keys are retained; every
    leaf becomes None, because this is persisted to the agent's work dir
    and resource values carry passwords and certificates.

    {"a": 1, "b": {"c": 2}} -> {"a": None, "b": {"c": None}}
    """
    if isinstance(value, dict):
        return {k: value_shape(v) for k, v in value.items()}
    if isinstance(value, list):
        return [value_shape(v) for v in value]
    return None


def project_onto(value: tp.Any, shape: tp.Any) -> tp.Any:
    """Reduce `value` to the keys `shape` has, recursively.

    A key the shape does not have is dropped. A key it has but the value
    does not is simply absent -- the hashes then differ, which is what a
    data plane that drops a declared field should look like.

    Lists are paired by index: elements past the end of the shape are kept
    as they are, so a data plane that returns more elements than were
    declared reads as the drift it is.
    """
    if isinstance(shape, dict) and isinstance(value, dict):
        return {k: project_onto(value[k], shape[k]) for k in shape if k in value}
    if isinstance(shape, list) and isinstance(value, list):
        return [
            project_onto(v, shape[i]) if i < len(shape) else v
            for i, v in enumerate(value)
        ]
    return value


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
