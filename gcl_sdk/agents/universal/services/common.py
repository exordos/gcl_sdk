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

import logging
import typing as tp
import uuid as sys_uuid

from gcl_sdk.agents.universal.clients.orch import base as orch_base
from gcl_sdk.agents.universal.clients.orch import exceptions as orch_exc
from gcl_sdk.agents.universal.dm import models as ua_models

LOG = logging.getLogger(__name__)


class UAServiceSpec(tp.NamedTuple):
    """The service specification for the UA and UB services."""

    uuid: sys_uuid.UUID
    orch_client: orch_base.AbstractOrchClient
    capabilities: tp.Collection[str] = tuple()
    facts: tp.Collection[str] = tuple()
    name: str | None = None


class RegistrableUAServiceMixin:
    """A Mixin for services that can be registered.

    The services are divided into two categories:

    1. Nameless services: These services are not registered and
       manage all entities.
    2. Named services: These services are registered and manage
       specific entities. The named services use some kind of filters
       to determine which entities they manage.
    """

    @property
    def ua_service_spec(self) -> UAServiceSpec | None:
        """Get the service spec."""
        return None

    @property
    def is_nameless_ua_service(self) -> bool:
        """Check if the service is nameless.

        A nameless service is a service that is not registered and
        manages all entities.
        """
        return self.ua_service_spec is None

    def register_ua_service(self) -> None:
        """Register the service.

        Use the universal agent table to register a service.
        """
        # If the service is nameless, it means that it is not registered
        if self.is_nameless_ua_service:
            return

        spec = self.ua_service_spec

        agent = ua_models.UniversalAgent.from_system_uuid(
            spec.capabilities, spec.facts, spec.uuid, spec.name
        )
        try:
            spec.orch_client.agents_create(agent)
            LOG.info("The service registered: %s", agent.uuid)
        except orch_exc.AgentAlreadyExists:
            LOG.warning("The service already registered: %s", agent.uuid)

            # Update the agent capabilities and facts if they were changed
            spec.orch_client.agents_update(agent)
