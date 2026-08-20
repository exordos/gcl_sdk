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

import logging
import typing as tp
import uuid as sys_uuid

from bazooka import exceptions as bazooka_exc
from restalchemy.dm import filters as dm_filters

from gcl_sdk.agents.universal.clients.backend import exceptions
from gcl_sdk.agents.universal.clients.backend import rest
from gcl_sdk.agents.universal.dm import models
from gcl_sdk.agents.universal.storage import base as storage_base
from gcl_sdk.clients.http import base as http

LOG = logging.getLogger(__name__)


class SecretModelSpec(tp.NamedTuple):
    """Specification of a resource with a secret handled via REST API.

    ``kind`` - resource kind, e.g. ``em_core_iam_users``.
    ``collection`` - REST collection URL, e.g. ``/v1/iam/users/``.
    ``secret_field`` - name of the secret field as it appears in the
        REST API (e.g. ``password`` for users, ``secret`` for IAM clients).
    ``filters`` - optional per-kind filters for the ``list`` method.
        When set, these filters are used instead of UUID-based filtering
        from the target fields storage. When empty/None, UUID-based
        filtering is used.
    """

    kind: str
    collection: str
    secret_field: str
    filters: dict[str, str] | None = None


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

        return {"uuid": tuple(str(u) for u in target_fields[kind])}

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


class GCSecretRestApiBackendClient(rest.RestApiBackendClient):
    """Exordos Core Rest API backend client for resources with secrets.

    Works with collections of resources that have a secret field (e.g.
    users, IAM clients). The secret is hashed in the control plane and
    cannot be restored from the REST API response, so it is enriched
    from the data plane (Resource table).

    ``model_specs`` describes each handled kind: its REST collection URL
    and the name of the secret field as it appears in the REST API
    (e.g. ``password`` for users, ``secret`` for IAM clients).
    """

    def __init__(
        self,
        http_client: http.CollectionBaseClient,
        model_specs: tp.Collection[SecretModelSpec],
        project_id: sys_uuid.UUID | None = None,
        tf_storage: storage_base.AbstractTargetFieldsStorage | None = None,
    ) -> None:
        super().__init__(
            http_client=http_client,
            collection_map={s.kind: s.collection for s in model_specs},
        )
        self._spec_map = {s.kind: s for s in model_specs}
        self._project_id = project_id
        self._tf_storage = tf_storage

    def _get_filters(self, kind: str) -> dict[str, str | tuple[str]]:
        """Get filters for the kind.

        If the spec has per-kind filters, use them. Otherwise, construct
        filters from the target fields storage (UUID-based).
        """
        spec = self._spec_map.get(kind)
        if spec is not None and spec.filters:
            return dict(spec.filters)

        if self._tf_storage is None:
            return {}

        # Construct filters from the target fields
        target_fields: dict = self._tf_storage.storage()
        if kind not in target_fields or not target_fields[kind]:
            return {}

        return {"uuid": tuple(str(u) for u in target_fields[kind])}

    def _enrich_resources(
        self, kind: str, resources: list[dict[str, tp.Any]]
    ) -> list[dict[str, tp.Any]]:
        """Enrich resources with the secret field from the data plane."""
        spec = self._spec_map.get(kind)
        if spec is None:
            return resources

        uuids = [r["uuid"] for r in resources]

        # Fetch actual resources from the data plane to get the secret
        db_resources = {
            str(r.uuid): r
            for r in models.Resource.objects.get_all(
                filters={
                    "uuid": dm_filters.In(uuids),
                    "kind": dm_filters.EQ(kind),
                }
            )
        }

        # Enrich resources with the secret from the data plane.
        # Skip resources not found in data plane - they may have been deleted.
        enriched = []
        for res in resources:
            if res["uuid"] not in db_resources:
                LOG.warning(
                    "Resource %s not found in data plane, skipping",
                    res["uuid"],
                )
                continue
            db_res = db_resources[res["uuid"]]
            res[spec.secret_field] = db_res.value.get(spec.secret_field, "")
            enriched.append(res)

        return enriched

    def get(self, resource: models.Resource) -> dict[str, tp.Any]:
        """Get the resource value in dictionary format."""
        collection_url = self._collection_map[resource.kind]

        try:
            result = self._client.get(collection_url, resource.uuid)
        except bazooka_exc.NotFoundError:
            raise exceptions.ResourceNotFound(resource=resource)

        enriched = self._enrich_resources(resource.kind, [result])
        if not enriched:
            # Resource exists in the control plane but has no data plane
            # record (e.g. after partial failure or manual creation). Use
            # the target resource's secret so the resource can be collected
            # and the DB record self-heals.
            spec = self._spec_map.get(resource.kind)
            if spec is not None:
                LOG.warning(
                    "Resource %s has no data plane record, "
                    "using target secret as fallback",
                    resource.uuid,
                )
                result[spec.secret_field] = resource.value.get(spec.secret_field, "")
            return result
        return enriched[0]

    def create(self, resource: models.Resource) -> dict[str, tp.Any]:
        """Creates the resource. Returns the created resource."""
        # Inject mandatory fields
        resource.value["uuid"] = str(resource.uuid)

        # Validate project_id. Only one project is supported.
        if self._project_id is not None:
            res_project_id = resource.value.get("project_id", None)
            if res_project_id and res_project_id != str(self._project_id):
                raise ResourceProjectMismatch(resource=resource)

        # Save the secret before sending to the backend (it may be stripped)
        spec = self._spec_map.get(resource.kind)
        secret = resource.value.get(spec.secret_field) if spec else None

        result = super().create(resource)

        if spec is not None and secret is not None:
            result[spec.secret_field] = secret

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

        # Restore the secret from the enriched resource
        spec = self._spec_map.get(resource.kind)
        if spec is not None:
            result[spec.secret_field] = enriched_resource[spec.secret_field]

        return result

    def list(self, kind: str) -> list[dict[str, tp.Any]]:
        """Lists all resources by kind."""
        filters = self._get_filters(kind)

        if not filters:
            return []

        resources = super().list(kind, **filters)
        return self._enrich_resources(kind, resources)
