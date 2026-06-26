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
import functools
import typing as tp
import uuid as sys_uuid

import bazooka

from gcl_sdk.agents.universal.dm import models
from gcl_sdk.agents.universal.status_api.dm import models as status_models
from gcl_sdk.clients.http import base


class UniversalAgentsClient(base.StaticCollectionBaseModelClient):
    __collection_path__ = "/v1/agents/"
    __model__ = models.UniversalAgent


class NodeVerifiersClient(base.StaticCollectionBaseModelClient):
    __collection_path__ = "/v1/node_verifiers/"
    __model__ = status_models.NodeVerifier

    def verify(self, uuid: sys_uuid.UUID) -> bool:
        """Verify that a node encryption key exists.

        Args:
            uuid: The UUID of the node to verify.

        Returns:
            True if the node encryption key exists, False otherwise.
        """
        result = self.do_action("verify", uuid)
        return result.get("valid", False)


class NodesClient(base.StaticCollectionBaseModelClient):
    __collection_path__ = "/v1/nodes/"
    __model__ = models.NodeEncryptionKey


class ResourcesClient(base.CollectionBaseModelClient):
    API_VERSION = "v1"

    __model__ = models.Resource

    def __init__(
        self,
        base_url: str,
        kind: str,
        http_client: bazooka.Client | None = None,
        auth: base.AbstractAuthenticator | None = None,
        encryptor: base.Encryptor | None = None,
    ) -> None:
        collecton_path = f"/{self.API_VERSION}/kind/{kind}/resources/"
        super().__init__(base_url, collecton_path, http_client, auth, encryptor)
        self._kind = kind

    def _set_kind_ref(self, resource: models.Resource | dict) -> None:
        """Set the resource kind as a reference.

        The `kind` field in the response is a reference since the kind is
        a collection now. It's not convenient to use it as the reference
        so set it to a reference before creating the resource in Status API.
        """
        ref = f"/{self.API_VERSION}/kind/{self._kind}"

        try:
            resource.kind = ref
        except AttributeError:
            if "kind" in resource:
                resource["kind"] = ref

    def _drop_kind_ref(self, resource: models.Resource | dict) -> None:
        """Drop the kind reference and set it as a string.

        The `kind` field in the response is a reference since the kind is
        a collection now. It's not convenient to use it as the reference
        so drop the reference and set the kind as a string before using it.
        """
        try:
            resource.kind = self._kind
        except AttributeError:
            if "kind" in resource:
                resource["kind"] = self._kind

    def get(self, uuid: sys_uuid.UUID) -> models.Resource:
        resource = super().get(uuid)
        self._drop_kind_ref(resource)
        return resource

    def filter(self, **filters: tp.Dict[str, tp.Any]) -> list[models.Resource]:
        resources = super().filter(**filters)
        for r in resources:
            self._drop_kind_ref(r)
        return resources

    def create(self, object: models.Resource) -> models.Resource:
        self._set_kind_ref(object)
        resource = super().create(object)
        self._drop_kind_ref(resource)
        return resource

    def update(
        self, uuid: sys_uuid.UUID, **params: tp.Dict[str, tp.Any]
    ) -> models.Resource:
        self._set_kind_ref(params)
        resource = super().update(uuid, **params)
        self._drop_kind_ref(resource)
        return resource


class StatusAPI:
    def __init__(
        self,
        base_url: str,
        http_client: bazooka.Client | None = None,
        encryptor: base.Encryptor | None = None,
    ) -> None:
        http_client = http_client or bazooka.Client()
        self._http_client = http_client
        self._base_url = base_url
        self._encryptor = encryptor
        self._agents_client = UniversalAgentsClient(
            base_url, http_client=http_client, encryptor=encryptor
        )
        self._node_verifiers_client = NodeVerifiersClient(
            base_url, http_client=http_client, encryptor=encryptor
        )

    @property
    def agents(self) -> UniversalAgentsClient:
        return self._agents_client

    @property
    def node_verifiers(self) -> NodeVerifiersClient:
        return self._node_verifiers_client

    @functools.lru_cache
    def resources(self, kind: str) -> ResourcesClient:
        return ResourcesClient(
            self._base_url, kind, self._http_client, encryptor=self._encryptor
        )
