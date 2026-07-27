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
from restalchemy.dm import filters as dm_filters

from gcl_sdk.agents.universal import constants as c
from gcl_sdk.agents.universal.api import controllers as sdk_controllers
from gcl_sdk.agents.universal.api import crypto
from gcl_sdk.agents.universal.status_api.dm import models


class KindController(sdk_controllers.BaseSdkResourceController):
    """Controller for /v1/kind/ endpoint"""

    __resource__ = resources.ResourceByRAModel(
        model_class=models.KindModel,
        process_filters=True,
        convert_underscore=False,
    )


class ResourcesController(sdk_controllers.BaseSdkNestedResourceController):
    """Controller for /v1/kind/<name>/resources/ endpoint"""

    __resource__ = resources.ResourceByRAModel(
        model_class=models.Resource,
        process_filters=True,
        convert_underscore=False,
    )

    def filter(self, parent_resource: models.KindModel, filters, order_by=None):
        return models.Resource.objects.get_all(
            filters={
                "kind": dm_filters.EQ(parent_resource.kind),
            }
        )

    def get(self, uuid: sys_uuid.UUID, parent_resource: models.KindModel, **kwargs):
        resource = models.Resource.objects.get_one(
            filters={
                "kind": dm_filters.EQ(parent_resource.kind),
                "uuid": dm_filters.EQ(str(uuid)),
            }
        )
        return resource


class UniversalAgentsController(sdk_controllers.BaseSdkResourceController):
    """Controller for /v1/agents/ endpoint"""

    __resource__ = resources.ResourceByRAModel(
        model_class=models.UniversalAgent,
        process_filters=True,
        convert_underscore=False,
        fields_permissions=field_p.FieldsPermissions(
            default=field_p.Permissions.RW,
            fields={
                # UPDATE only - CREATE must still accept it, since a fresh
                # agent's registration payload always includes its own
                # (client-chosen) initial status.
                "status": {ra_c.UPDATE: field_p.Permissions.RO},
            },
        ),
    )

    def update(self, uuid: sys_uuid.UUID, **kwargs):
        """Update the agent, always (re)activating it.

        `status` is read-only over the API - an agent successfully calling
        this endpoint (e.g. on re-registration) is what proves it's alive,
        so the server activates it here rather than trusting a client-
        supplied `status` value.
        """
        kwargs["status"] = c.AgentStatus.ACTIVE.value
        return super().update(uuid, **kwargs)


class NodeVerifierController(sdk_controllers.BaseSdkResourceController):
    """Controller for /v1/node_verifiers/ endpoint"""

    __resource__ = resources.ResourceByRAModel(
        model_class=models.NodeVerifier,
        process_filters=True,
        convert_underscore=False,
    )

    @actions.get
    def verify(self, resource: models.NodeVerifier):
        """Verify that a node encryption key exists.

        The action automatically loads the resource. If the key doesn't
        exist, the framework raises an error. Successful execution confirms
        the key is valid and present in the database.

        Args:
            resource: The NodeVerifier model instance to verify.

        Returns:
            dict: {"valid": True} if the key exists.
        """
        return {"valid": True}


class NodesController(sdk_controllers.BaseSdkResourceController):
    """Controller for /v1/nodes/ endpoint"""

    __resource__ = resources.ResourceByRAModel(
        model_class=models.NodeEncryptionKey,
        process_filters=True,
        convert_underscore=False,
    )

    @actions.post
    def refresh_secret(self, resource: models.NodeEncryptionKey):
        _, key_base64 = crypto.generate_key_base64()
        resource.private_key = key_base64
        resource.save()

        return {"key": key_base64}
