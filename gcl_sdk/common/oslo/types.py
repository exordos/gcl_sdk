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

import uuid

from oslo_config import cfg
from oslo_config import types as oslo_types

from gcl_sdk.agents.universal import utils


class UuidType(oslo_types.String):
    """oslo_config type for UUID values.

    Parses and validates UUID values. Accepts either a uuid.UUID instance or a
    string in standard UUID formats. Returns a uuid.UUID object on success.
    """

    def __init__(self):
        super().__init__(type_name="uuid value")

    def __call__(self, value):  # type: ignore[override]
        # Allow None to pass through so options can have default=None
        if value is None:
            return None

        # If already a uuid.UUID, validate nil acceptance and return
        if isinstance(value, uuid.UUID):
            return value

        # If it's a string, try to parse as UUID
        if isinstance(value, str):
            return uuid.UUID(value)

        # Any other type is invalid
        raise ValueError(f"Invalid UUID type: {type(value).__name__}")

    def __repr__(self) -> str:
        return "Uuid"


class ObjectType(oslo_types.String):
    """oslo_config type for fully qualified class references.

    Accepts a string in the form "module.path:ClassName" and returns the
    corresponding Python class object via cfg_load_class. Also accepts a class
    object directly and returns it unchanged. Allows None for defaults.
    """

    def __init__(self):
        super().__init__(type_name="object reference")

    def __call__(self, value):  # type: ignore[override]
        if value is None:
            return None

        # If already a class/type, return as-is
        if isinstance(value, type):
            return value

        # If it's a string, resolve using cfg_load_class
        if isinstance(value, str):
            return utils.cfg_load_class(value)

        raise ValueError(f"Invalid Object reference type: {type(value).__name__}")

    def __repr__(self) -> str:
        return "Object"


class UuidOpt(cfg.Opt):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, type=UuidType(), **kwargs)


class ObjectOpt(cfg.Opt):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, type=ObjectType(), **kwargs)
