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

import string
import typing as tp

from bazooka import exceptions as bazooka_exc

from gcl_sdk.agents.universal.clients.backend import base
from gcl_sdk.agents.universal.clients.backend import exceptions
from gcl_sdk.agents.universal.dm import models
from gcl_sdk.clients.http import base as http


class RestApiBackendClient(base.AbstractBackendClient):
    """Rest API backend client.

    A collection may be nested, e.g. ``/v1/network/lb/{lb}/vhosts/``: each
    placeholder names a resource field holding the parent's uuid. Such
    fields only address the collection, so they are kept out of the request
    body and put back into the returned value.
    """

    def __init__(
        self,
        http_client: http.CollectionBaseClient,
        collection_map: dict[str, str],
    ) -> None:
        self._client = http_client
        self._collection_map = collection_map

    def _url_fields(self, kind: str) -> list[str]:
        return [
            field
            for _, field, _, _ in string.Formatter().parse(self._collection_map[kind])
            if field
        ]

    def _collection(self, resource: models.Resource) -> str:
        fields = self._url_fields(resource.kind)
        return self._collection_map[resource.kind].format(
            **{f: resource.value[f] for f in fields}
        )

    def _body(self, resource: models.Resource) -> dict[str, tp.Any]:
        fields = self._url_fields(resource.kind)
        return {k: v for k, v in resource.value.items() if k not in fields}

    def _with_url_fields(
        self, resource: models.Resource, value: dict[str, tp.Any]
    ) -> dict[str, tp.Any]:
        fields = self._url_fields(resource.kind)
        return {**value, **{f: resource.value[f] for f in fields}}

    def get(self, resource: models.Resource) -> dict[str, tp.Any]:
        """Get the resource value in dictionary format."""
        try:
            value = self._client.get(self._collection(resource), resource.uuid)
        except bazooka_exc.NotFoundError:
            raise exceptions.ResourceNotFound(resource=resource)
        return self._with_url_fields(resource, value)

    def create(self, resource: models.Resource) -> dict[str, tp.Any]:
        """Creates the resource. Returns the created resource."""
        try:
            value = self._client.create(
                self._collection(resource), self._body(resource)
            )
        except bazooka_exc.ConflictError:
            raise exceptions.ResourceAlreadyExists(resource=resource)
        return self._with_url_fields(resource, value)

    def update(self, resource: models.Resource) -> dict[str, tp.Any]:
        """Update the resource. Returns the updated resource."""
        try:
            value = self._client.update(
                self._collection(resource), resource.uuid, **self._body(resource)
            )
        except bazooka_exc.NotFoundError:
            raise exceptions.ResourceNotFound(resource=resource)
        return self._with_url_fields(resource, value)

    def list(self, kind: str, **kwargs) -> list[dict[str, tp.Any]]:
        """Lists all resources by kind.

        For a nested collection every parent collection is listed to learn
        the parent uuids; only a ``project_id`` filter applies to parents.
        """
        parent_filters = {k: v for k, v in kwargs.items() if k == "project_id"}
        # (url fields, collection url so far) per reachable parent
        scopes: list[tuple[dict[str, tp.Any], str]] = [({}, "")]
        tail = ""
        for literal, field, _, _ in string.Formatter().parse(
            self._collection_map[kind]
        ):
            if not field:
                tail = literal
                continue
            scopes = [
                ({**fields, field: item["uuid"]}, f"{url}{literal}{item['uuid']}")
                for fields, url in scopes
                for item in self._client.filter(url + literal, **parent_filters)
            ]

        # TODO(akremenetsky): Use a project prefix to filter resources
        return [
            {**item, **fields}
            for fields, url in scopes
            for item in self._client.filter(url + tail, **kwargs)
        ]

    def delete(self, resource: models.Resource) -> None:
        """Delete the resource."""
        try:
            self._client.delete(self._collection(resource), resource.uuid)
        except bazooka_exc.NotFoundError:
            raise exceptions.ResourceNotFound(resource=resource)
