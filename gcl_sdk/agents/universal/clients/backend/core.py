#    Copyright 2025-2026 Genesis Corporation.
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

import typing as tp
import uuid as sys_uuid

from bazooka import exceptions as bazooka_exc
from restalchemy.dm import filters as dm_filters

from gcl_sdk.agents.universal.dm import models
from gcl_sdk.clients.http import base as http
from gcl_sdk.agents.universal.clients.backend import rest
from gcl_sdk.agents.universal.clients.backend import exceptions
from gcl_sdk.agents.universal.storage import base as storage_base


class ResourceProjectMismatch(exceptions.BackendClientException):
    __template__ = "The resource project mismatch: {resource}"
    resource: models.Resource


class GCRestApiBackendClient(rest.RestApiBackendClient):
    """Exordos Core Rest API backend client."""

    def __init__(
        self,
        http_client: http.CollectionBaseClient,
        collection_map: dict[str, str],
        project_id: sys_uuid.UUID | None = None,
        tf_storage: storage_base.AbstractTargetFieldsStorage | None = None,
    ) -> None:
        super().__init__(http_client=http_client, collection_map=collection_map)
        self._project_id = project_id
        self._tf_storage = tf_storage

    def _get_filters(self, kind: str) -> dict[str, str | tuple[str]]:
        """Get filters for the kind.

        If the project_id is set, return it.
        Otherwise, construct filters from the target fields
        from the storage.
        """
        if self._project_id is not None:
            return {"project_id": str(self._project_id)}

        # Construct filters from the target fields
        target_fields: dict = self._tf_storage.storage()
        if kind not in target_fields or not target_fields[kind]:
            return {}

        return {"uuid": tuple(str(u) for u in target_fields[kind].keys())}

    def create(self, resource: models.Resource) -> dict[str, tp.Any]:
        """Creates the resource. Returns the created resource."""
        # Inject mandatory fields
        resource.value["uuid"] = str(resource.uuid)

        # Simple validation for project_id. Only one project is supported.
        if self._project_id is not None:
            res_project_id = resource.value.get("project_id", None)
            if res_project_id and res_project_id != str(self._project_id):
                raise ResourceProjectMismatch(resource=resource)

        return super().create(resource)

    def update(self, resource: models.Resource) -> dict[str, tp.Any]:
        """Update the resource. Returns the updated resource."""
        # FIXME(akremenetsky): Not the best implementation
        # Remove popential RO fields
        value = resource.value.copy()
        resource.value.pop("created_at", None)
        resource.value.pop("updated_at", None)
        resource.value.pop("project_id", None)
        resource.value.pop("uuid", None)

        try:
            result = super().update(resource)
        finally:
            resource.value = value

        return result

    def list(self, kind: str) -> list[dict[str, tp.Any]]:
        """Lists all resources by kind."""
        return super().list(kind, **self._get_filters(kind))


class GCUsersRestApiBackendClient(rest.RestApiBackendClient):
    """Exordos Core Users Rest API backend client.

    Works exclusively with the /v1/iam/users collection.
    """

    USERS_COLLECTION = "/v1/iam/users/"

    def __init__(
        self,
        http_client: http.CollectionBaseClient,
        user_kind: str,
        tf_storage: storage_base.AbstractTargetFieldsStorage | None = None,
    ) -> None:
        super().__init__(
            http_client=http_client,
            collection_map={user_kind: self.USERS_COLLECTION},
        )
        self._user_kind = user_kind
        self._tf_storage = tf_storage

    def _get_filters(self, kind: str) -> dict[str, str | tuple[str]]:
        """Get filters for the kind.

        Constructs filters from the target fields from the storage.
        Users are not project-scoped, so only UUID-based filtering is used.
        """
        if self._tf_storage is None:
            return {}

        # Construct filters from the target fields
        target_fields: dict = self._tf_storage.storage()
        if kind not in target_fields or not target_fields[kind]:
            return {}

        return {"uuid": tuple(str(u) for u in target_fields[kind].keys())}

    def _enrich_users(self, users: list[dict[str, tp.Any]]) -> list[dict[str, tp.Any]]:
        """Enrich users with additional fields."""
        uuids = [user["uuid"] for user in users]

        # Fetch actual resources of the users to get additional data
        resources = {
            str(r.uuid): r
            for r in models.Resource.objects.get_all(
                filters={"uuid": dm_filters.In(uuids)}
            )
        }

        # Enrich users with additional data
        for user in users:
            if user["uuid"] not in resources:
                raise ValueError(f"Resource with UUID {user['uuid']} not found")
            user["password"] = resources[user["uuid"]].value["password"]

        return users

    def get(self, resource: models.Resource) -> dict[str, tp.Any]:
        """Get the resource value in dictionary format."""
        collection_url = self._collection_map[resource.kind]

        try:
            result = self._client.get(collection_url, resource.uuid)
        except bazooka_exc.NotFoundError:
            raise exceptions.ResourceNotFound(resource=resource)

        result = self._enrich_users([result])[0]

        return result

    def create(self, resource: models.Resource) -> dict[str, tp.Any]:
        """Creates the resource. Returns the created resource."""
        # Inject mandatory fields
        resource.value["uuid"] = str(resource.uuid)

        # Save mandatory field before sending to the backend
        password = resource.value["password"]

        result = super().create(resource)

        result["password"] = password

        return result

    def update(self, resource: models.Resource) -> dict[str, tp.Any]:
        """Update the resource. Returns the updated resource."""
        enriched_resource = self.get(resource)

        # FIXME(akremenetsky): Not the best implementation
        # Remove popential RO fields
        value = resource.value.copy()
        resource.value.pop("created_at", None)
        resource.value.pop("updated_at", None)
        resource.value.pop("project_id", None)
        resource.value.pop("uuid", None)

        try:
            result = super().update(resource)
        finally:
            resource.value = value

        # Restore the password
        result["password"] = enriched_resource["password"]

        return result

    def list(self, kind: str) -> list[dict[str, tp.Any]]:
        """Lists all resources by kind."""
        filters = self._get_filters(kind)

        if not filters:
            return []

        users = super().list(kind, **filters)
        return self._enrich_users(users)
