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
from gcl_sdk.common import exceptions


class OrchClientException(exceptions.UniversalAgentException):
    __template__ = "An unknown orchestrator client exception occurred."


class AgentAlreadyExists(OrchClientException):
    __template__ = "The agent already exists: {uuid}"
    uuid: sys_uuid.UUID


class AgentNotFound(OrchClientException):
    __template__ = "The agent not found: {uuid}"
    uuid: sys_uuid.UUID


class ResourceAlreadyExists(OrchClientException):
    __template__ = "The resource already exists: {uuid}"
    uuid: sys_uuid.UUID


class ResourceNotFound(OrchClientException):
    __template__ = "The resource not found: {uuid}"
    uuid: sys_uuid.UUID


class NodeNotFound(OrchClientException):
    __template__ = "The node not found: {uuid}"
    uuid: sys_uuid.UUID | None
