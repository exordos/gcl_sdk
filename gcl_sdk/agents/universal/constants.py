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
import enum
import os

EXORDOS_WORK_DIR = "/var/lib/exordos"
WORK_DIR = "/var/lib/exordos/universal_agent/"
PAYLOAD_PATH = os.path.join(WORK_DIR, "payload.json")
PRIVATE_KEY_PATH = os.path.join(WORK_DIR, "private_key")
NODE_UUID_PATH = os.path.join(EXORDOS_WORK_DIR, "node-id")
DEFAULT_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S.%1f"
EP_UNIVERSAL_AGENT = "gcl_sdk_universal_agent"

DEF_SQL_LIMIT = 100


class AgentStatus(str, enum.Enum):
    NEW = "NEW"
    ACTIVE = "ACTIVE"
    ERROR = "ERROR"
    DISABLED = "DISABLED"


class InstanceStatus(str, enum.Enum):
    NEW = "NEW"
    IN_PROGRESS = "IN_PROGRESS"
    ACTIVE = "ACTIVE"
    ERROR = "ERROR"
