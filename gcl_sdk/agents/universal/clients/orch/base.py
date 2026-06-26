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
import typing as tp
import uuid as sys_uuid

from gcl_sdk.agents.universal.dm import models


class AbstractOrchClient(abc.ABC):
    """Abstract Orchestrator client.

    An interface for clients to communicate with an orchestrator.
    """

    @abc.abstractmethod
    def agents_create(
        self,
        agent: models.UniversalAgent,
        check_node_exists: bool = False,
        **kwargs: tp.Any,
    ) -> models.UniversalAgent:
        """Create an instance of Universal agent."""

    @abc.abstractmethod
    def agents_update(
        self, agent: models.UniversalAgent, **kwargs: tp.Any
    ) -> models.UniversalAgent:
        """Update an instance of Universal agent."""

    @abc.abstractmethod
    def agents_get_payload(
        self,
        uuid: sys_uuid.UUID,
        payload: models.Payload | None,
        **kwargs: tp.Any,
    ) -> models.Payload:
        """Get payload for of the Universal agent."""

    @abc.abstractmethod
    def resources_create(
        self, resource: models.Resource, **kwargs: tp.Any
    ) -> models.Resource:
        """Create a resource."""

    @abc.abstractmethod
    def resources_get(
        self, kind: str, uuid: sys_uuid.UUID, **kwargs: tp.Any
    ) -> models.Resource:
        """Get the resource."""

    @abc.abstractmethod
    def resources_update(
        self, kind: str, uuid: sys_uuid.UUID, **kwargs: tp.Any
    ) -> models.Resource:
        """Update the resource."""

    @abc.abstractmethod
    def resources_delete(self, resource: models.Resource, **kwargs: tp.Any) -> None:
        """Delete the resource."""
