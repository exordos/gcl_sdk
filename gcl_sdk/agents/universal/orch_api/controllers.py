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
import uuid as sys_uuid

from restalchemy.api import actions
from restalchemy.api import constants as ra_c
from restalchemy.api import field_permissions as field_p
from restalchemy.api import resources

from gcl_sdk.agents.universal import constants as c
from gcl_sdk.agents.universal.api import controllers as sdk_controllers
from gcl_sdk.agents.universal.dm import models


class UniversalAgentsController(sdk_controllers.BaseSdkResourceController):
    """Controller for /v1/agents/ endpoint"""

    __resource__ = resources.ResourceByRAModel(
        model_class=models.UniversalAgent,
        process_filters=True,
        convert_underscore=False,
        fields_permissions=field_p.FieldsPermissions(
            default=field_p.Permissions.RW,
            fields={
                # UPDATE only - CREATE stays RW so older clients that still
                # send their own initial status keep working: the value is
                # simply overridden below, not rejected.
                "status": {ra_c.UPDATE: field_p.Permissions.RO},
            },
        ),
    )

    def create(self, **kwargs):
        """Create the agent, always starting it ACTIVE.

        The server decides the initial status, not the registering agent's
        own (client-side default) value. A client-supplied `status` is
        simply overridden here rather than rejected, so older clients that
        still send one don't break.
        """
        kwargs["status"] = c.AgentStatus.ACTIVE.value
        return super().create(**kwargs)

    def update(self, uuid: sys_uuid.UUID, **kwargs):
        """Update the agent, always (re)activating it.

        `status` is read-only over the API - an agent successfully calling
        this endpoint (e.g. on re-registration) is what proves it's alive,
        so the server activates it here rather than trusting a client-
        supplied `status` value.
        """
        kwargs["status"] = c.AgentStatus.ACTIVE.value
        return super().update(uuid, **kwargs)

    @actions.get
    def get_payload(
        self,
        resource: models.UniversalAgent,
        hash: str = "",
        version: str = "0",
    ):
        payload = resource.get_payload(hash=hash, version=int(version))
        return payload.dump_to_simple_view()
