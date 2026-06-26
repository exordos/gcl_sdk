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

import dataclasses
import logging
import typing as tp
import uuid as sys_uuid

from gcl_sdk.agents.universal.clients.backend import base as client_base
from gcl_sdk.agents.universal.clients.backend import exceptions as client_exc
from gcl_sdk.agents.universal.dm import models
from gcl_sdk.agents.universal.drivers import base
from gcl_sdk.agents.universal.drivers import exceptions as driver_exc
from gcl_sdk.agents.universal.storage import base as storage_base
from gcl_sdk.agents.universal.storage import exceptions as storage_exc

LOG = logging.getLogger(__name__)


@dataclasses.dataclass
class ResourceTransformer:
    """Process and perform some actions on the resource view.

    The main idea is to perform some additional transformations
    on the resource view. For instance, delete specific attributes
    or ignore null attributes and so on. The first implementation
    is pretty simple and it can ignore null attributes. The next
    implementation can be more complex and the interface can be
    changed.

    Examples:
    # Ignore null attributes
    In:
    {
        "uuid": "0000000-0000-0000-0000-000000000000",
        "name": "foo",
        "value": null,
        "created_at": "2026-01-01T00:00:00.000000",
        "updated_at": "2026-01-01T00:00:00.000000"
    }
    Out:
    {
        "uuid": "0000000-0000-0000-0000-000000000000",
        "name": "foo",
        "created_at": "2026-01-01T00:00:00.000000",
        "updated_at": "2026-01-01T00:00:00.000000"
    }
    """

    ignore_null_attributes: bool = False
    attributes: tp.Collection[str] | None = None

    def transform(self, view: dict) -> dict:
        if not self.ignore_null_attributes:
            return view

        # If attributes are not specified, ignore all null attributes
        if self.attributes is None:
            return {k: v for k, v in view.items() if v is not None}

        # If attributes are specified, ignore only null attributes from the list
        attributes = set(self.attributes)
        return {k: v for k, v in view.items() if not (v is None and k in attributes)}

    @classmethod
    def from_dict(cls, data: dict) -> ResourceTransformer:
        ignore_null_attributes = data.get("ignore_null_attributes", False)
        attributes = data.get("attributes", None)

        if isinstance(ignore_null_attributes, str):
            ignore_null_attributes = ignore_null_attributes.lower() == "true"

        if isinstance(attributes, str):
            attributes = {attr.strip() for attr in attributes.split(",")}

        return cls(
            ignore_null_attributes=ignore_null_attributes,
            attributes=attributes,
        )


class DirectAgentDriver(base.AbstractCapabilityDriver):
    """Direct driver. Directly gets all data from the backend.

    The key feature of this driver it's able to get all
    data from the data plane without using any additional
    information like meta files.

    It uses the target fields storage to store the target fields
    of the resources. This is needed to correctly calculate the
    hash of the resources.
    """

    def __init__(
        self,
        client: client_base.AbstractBackendClient,
        storage: storage_base.AbstractTargetFieldsStorage,
        transformer_map: dict[str, ResourceTransformer] | None = None,
    ):
        super().__init__()
        self._client = client
        self._storage = storage
        self._transformer_map = transformer_map or {}

    def _model_to_resource(
        self,
        kind: str,
        model: models.ResourceMixin,
        target_fields: frozenset[str],
    ) -> models.Resource:
        # We need to be sure the return object is resource
        # and not target resource
        value = model.dump_to_simple_view(skip=model.get_resource_ignore_fields())

        # Apply additional transformations if needed
        if kind in self._transformer_map:
            value = self._transformer_map[kind].transform(value)

        return models.Resource.from_value(value, kind, target_fields)

    def _prepare_res_response(
        self,
        origin_resource: models.Resource,
        value: dict[str, tp.Any] | models.Resource | models.ResourceMixin,
        target_fields: frozenset[str],
    ) -> models.Resource:
        """Prepare the response from the client."""
        if isinstance(value, models.Resource):
            return value
        elif isinstance(value, dict):
            # Apply additional transformations if needed
            if origin_resource.kind in self._transformer_map:
                value = self._transformer_map[origin_resource.kind].transform(value)

            return origin_resource.replace_value(value, target_fields)
        else:
            return self._model_to_resource(origin_resource.kind, value, target_fields)

    def _validate(self, resource: models.Resource) -> None:
        """Validate the resource."""
        if resource.kind not in self.get_capabilities():
            raise TypeError(f"Unsupported capability {resource.kind}")

    def get(self, resource: models.Resource) -> models.Resource:
        """Find and return a resource by uuid and kind.

        It returns the resource from the backend.
        """
        self._validate(resource)

        try:
            target_fields = self._storage.get(resource.kind, resource.uuid)
            value = self._client.get(resource)
        except (client_exc.ResourceNotFound, storage_exc.ItemNotFound):
            LOG.error("Unable to find resource on backend %s", resource.uuid)
            raise driver_exc.ResourceNotFound(resource=resource)

        return self._prepare_res_response(resource, value, target_fields.fields)

    def list(self, capability: str) -> list[models.Resource]:
        """Lists all resources by capability."""
        if capability not in self.get_capabilities():
            raise TypeError(f"Unsupported capability {capability}")

        # Collect all resources in convient format
        resources = []
        storage_items = {i.uuid: i for i in self._storage.list(capability)}
        client_resources = {}

        # Collect client items
        for i in self._client.list(capability):
            if isinstance(i, models.Resource):
                client_resources[i.uuid] = i
            elif isinstance(i, dict):
                uuid = sys_uuid.UUID(i["uuid"])
                if storage_item := storage_items.get(uuid):
                    # Apply additional transformations if needed
                    if capability in self._transformer_map:
                        i = self._transformer_map[capability].transform(i)
                    client_resources[uuid] = models.Resource.from_value(
                        i, capability, storage_item.fields
                    )
                else:
                    LOG.warning("Missing storage item for %s", uuid)
            else:
                uuid = i.get_resource_uuid()

                # NOTE(akremenetsky): It's important point to take target fields
                # from storage but not from the models since fields can be
                # changed during live time and it will follow to wrong hash
                # and as a result migration problems. Seems it will be better
                # to explicitly update target fields during the update procedure.
                if storage_item := storage_items.get(uuid):
                    client_resources[uuid] = self._model_to_resource(
                        capability, i, storage_item.fields
                    )
                else:
                    LOG.warning("Missing storage item for %s", uuid)

        # If storage item or client item is missing, consider it as
        # a missing resource
        for uuid in storage_items.keys() & client_resources.keys():
            resources.append(client_resources[uuid])

        return resources

    def create(self, resource: models.Resource) -> models.Resource:
        """Creates a resource."""
        self._validate(resource)

        # Figure out the target fields to correct hash calculation
        target_fields = frozenset(resource.value.keys())
        storage_item = storage_base.TargetFieldItem(
            resource.kind, resource.uuid, target_fields
        )

        # There are no problems if the client fails its operation later.
        # The resource will be created next iteration.
        self._storage.create(storage_item, force=True)

        try:
            resp = self._client.create(resource)
            LOG.debug("Created resource: %s", resource.uuid)
        except client_exc.ResourceAlreadyExists:
            LOG.error("The resource already exists: %s", resource.uuid)
            raise driver_exc.ResourceAlreadyExists(resource=resource)

        # Convert response to the resource
        return self._prepare_res_response(resource, resp, target_fields)

    def update(self, resource: models.Resource) -> models.Resource:
        """Update the resource."""
        self._validate(resource)

        # Figure out the target fields to correct hash calculation
        target_fields = frozenset(resource.value.keys())
        storage_item = storage_base.TargetFieldItem(
            resource.kind, resource.uuid, target_fields
        )

        try:
            resp = self._client.update(resource)
            self._storage.update(storage_item)
            LOG.debug("Updated resource: %s", resource.uuid)
        except client_exc.ResourceNotFound:
            LOG.error("The resource does not exist: %s", resource.uuid)
            raise driver_exc.ResourceNotFound(resource=resource)

        # Convert response to the resource
        return self._prepare_res_response(resource, resp, target_fields)

    def delete(self, resource: models.Resource) -> None:
        """Delete the resource."""
        self._validate(resource)

        try:
            self._client.delete(resource)
            LOG.debug("Deleted resource: %s", resource.uuid)
        except client_exc.ResourceNotFound:
            LOG.warning("The resource is already deleted: %s", resource.uuid)

        storage_item = storage_base.TargetFieldItem(
            resource.kind, resource.uuid, frozenset()
        )

        self._storage.delete(storage_item, force=True)

    def start(self) -> None:
        """Perform some initialization before starting any operations.

        This method is called once before any other method like list,
        create, update, delete are called. It can be used to do some
        preparations like establishing connections, opening files, etc.

        The driver iteration:
            start -> list -> [create | update | delete]* -> finalize
        """
        self._storage.load()

    def finalize(self) -> None:
        """Perform some finalization after finishing all operations.

        This method is called once after all other methods like list,
        create, update, delete are called. It can be used to do some
        finalization or cleanups like closing connections, files, etc.

        The driver iteration:
            start -> list -> [create | update | delete]* -> finalize
        """
        self._storage.persist()
