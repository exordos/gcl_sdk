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

import bazooka

from gcl_sdk.agents.universal.dm import models
from gcl_sdk.clients.http import base


class CoreAgentsClient(base.StaticCollectionBaseModelClient):
    __collection_path__ = "/v1/ua/agents/"
    __model__ = models.UniversalAgent

    def issue_key(self, uuid: sys_uuid.UUID) -> str:
        """Issue (or fetch the existing) private key for the agent's node.

        Agents sharing a node uuid receive the same key: the key is
        generated only the first time it's requested for that node.

        Args:
            uuid: The UUID of the agent to issue the key for.

        Returns:
            The base64-encoded private key.
        """
        result = self.do_action("issue_key", uuid, invoke=True) or {}
        return result["key"]


class CoreAPI:
    def __init__(
        self,
        base_url: str,
        http_client: bazooka.Client | None = None,
        auth: base.AbstractAuthenticator | None = None,
    ) -> None:
        http_client = http_client or bazooka.Client()
        self._agents_client = CoreAgentsClient(
            base_url, http_client=http_client, auth=auth
        )

    @property
    def agents(self) -> CoreAgentsClient:
        return self._agents_client
