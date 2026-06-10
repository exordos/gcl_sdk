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
import typing as tp
import logging

import bazooka
from bazooka import exceptions as baz_exc

from gcl_sdk.agents.universal.dm import models
from gcl_sdk.clients.http import base as base_client
from gcl_sdk.agents.universal.clients.orch import base
from gcl_sdk.agents.universal.clients.orch import exceptions
from gcl_sdk.agents.universal.clients.http import status as rest_status
from gcl_sdk.agents.universal.clients.http import orch as rest_orch

LOG = logging.getLogger(__name__)


class HttpOrchClient(base.AbstractOrchClient):
    """HTTP Orchestrator client.

    An interface for clients to communicate with an orchestrator.
    """

    def __init__(
        self,
        orch_endpoint: str,
        status_endpoint: str,
        http_client: bazooka.Client | None = None,
        encryptor: base_client.Encryptor | None = None,
    ):
        http_client = http_client or bazooka.Client(default_timeout=20)

        self._orch_api = rest_orch.OrchAPI(
            orch_endpoint,
            http_client=http_client,
            encryptor=encryptor,
        )
        self._status_api = rest_status.StatusAPI(
            status_endpoint,
            http_client=http_client,
            encryptor=encryptor,
        )

    def _verify_node_exists(self, agent: models.UniversalAgent) -> None:
        """Verify that the agent's node encryption key exists.

        Args:
            agent: The UniversalAgent to verify.

        Raises:
            exceptions.NodeNotFound: If the node encryption key doesn't exist.
        """
        if agent.node is None:
            raise exceptions.NodeNotFound(uuid=None)

        try:
            self._status_api.node_verifiers.verify(agent.node)
        except baz_exc.NotFoundError:
            raise exceptions.NodeNotFound(uuid=agent.node)

    def agents_create(
        self,
        agent: models.UniversalAgent,
        check_node_exists: bool = False,
        **kwargs: tp.Any,
    ) -> models.UniversalAgent:
        """Create an instance of Universal agent."""
        if check_node_exists:
            self._verify_node_exists(agent)

        try:
            agent = self._status_api.agents.create(agent)
            LOG.info("Agent registered: %s", agent.uuid)
        except baz_exc.ConflictError:
            raise exceptions.AgentAlreadyExists(uuid=agent.uuid)

        return agent

    def agents_update(
        self, agent: models.UniversalAgent, **kwargs: tp.Any
    ) -> models.UniversalAgent:
        """Update an instance of Universal agent."""
        try:
            data = {
                "capabilities": agent.capabilities,
                "facts": agent.facts,
                "name": agent.name,
            }

            agent = self._status_api.agents.update(agent.uuid, **data)
            LOG.info("Agent updated: %s", agent.uuid)
        except baz_exc.NotFoundError:
            raise exceptions.AgentNotFound(uuid=agent.uuid)

        return agent

    def agents_get_payload(
        self,
        uuid: sys_uuid.UUID,
        payload: models.Payload | None,
        **kwargs: tp.Any,
    ) -> models.Payload:
        """Get payload for of the Universal agent."""
        if payload is None:
            payload = models.Payload.empty()

        try:
            payload = self._orch_api.agents.get_payload(uuid, payload)
        except baz_exc.NotFoundError:
            raise exceptions.AgentNotFound(uuid=uuid)

        return payload

    def resources_create(
        self, resource: models.Resource, **kwargs: tp.Any
    ) -> models.Resource:
        """Create a resource."""
        try:
            resource = self._status_api.resources(resource.kind).create(resource)
            LOG.info("Resource created: %s", resource.uuid)
        except baz_exc.ConflictError:
            raise exceptions.ResourceAlreadyExists(uuid=resource.uuid)

        return resource

    def resources_get(
        self, kind: str, uuid: sys_uuid.UUID, **kwargs: tp.Any
    ) -> models.Resource:
        """Get the resource."""
        try:
            resource = self._status_api.resources(kind).get(uuid)
        except baz_exc.NotFoundError:
            raise exceptions.ResourceNotFound(uuid=uuid)

        return resource

    def resources_update(
        self, kind: str, uuid: sys_uuid.UUID, **kwargs: tp.Any
    ) -> models.Resource:
        """Update the resource."""

        try:
            if "uuid" in kwargs and str(uuid) != kwargs["uuid"]:
                raise ValueError("UUID in kwargs does not match the uuid")
            if "uuid" in kwargs:
                resource = self._status_api.resources(kind).update(**kwargs)
            else:
                resource = self._status_api.resources(kind).update(uuid, **kwargs)
        except baz_exc.NotFoundError:
            raise exceptions.ResourceNotFound(uuid=uuid)

        return resource

    def resources_delete(self, resource: models.Resource, **kwargs: tp.Any) -> None:
        """Delete the resource."""
        try:
            self._status_api.resources(resource.kind).delete(resource.uuid)
        except baz_exc.NotFoundError:
            raise exceptions.ResourceNotFound(uuid=resource.uuid)
