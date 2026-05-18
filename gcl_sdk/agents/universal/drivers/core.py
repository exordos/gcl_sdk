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

import os
import logging
import typing as tp
import uuid as sys_uuid

import bazooka

from gcl_sdk.clients.http import base
from gcl_sdk.agents.universal.drivers import direct
from gcl_sdk.agents.universal.storage import fs

from gcl_sdk.agents.universal.clients.backend import core as core_rest_back
from gcl_sdk.agents.universal.clients.backend import db as db_back
from gcl_sdk.agents.universal import constants as c

LOG = logging.getLogger(__name__)


CORE_TARGET_FIELDS_FILENAME = "core_target_fields.json"


# DEPRECATED(akremenetsky): Use RestCoreCapabilityDriver instead.
class CoreCapabilityDriver(direct.DirectAgentDriver):
    """Core capability driver for interacting with Exordos Core."""

    def __init__(
        self,
        username: str,
        password: str,
        project_id: sys_uuid.UUID,
        user_api_base_url: str,
        agent_work_dir: str = c.WORK_DIR,
        **collection_map,
    ):
        http = bazooka.Client()
        auth = base.CoreIamAuthenticator(
            user_api_base_url, username, password, http_client=http
        )
        self._collection_map = {k: v.strip() for k, v in collection_map.items()}

        rest_client = base.CollectionBaseClient(
            http_client=http, base_url=user_api_base_url, auth=auth
        )

        storage_path = os.path.join(agent_work_dir, CORE_TARGET_FIELDS_FILENAME)

        storage = fs.TargetFieldsFileStorage(storage_path)
        rest_client = core_rest_back.GCRestApiBackendClient(
            rest_client,
            collection_map,
            project_id=project_id,
            tf_storage=storage,
        )

        super().__init__(storage=storage, client=rest_client)

    def get_capabilities(self) -> list[str]:
        """Returns a list of capabilities supported by the driver."""
        return list(self._collection_map.keys())


class RestCoreCapabilityDriver(direct.DirectAgentDriver):
    """Core capability driver for interacting with GC using REST API."""

    def __init__(
        self,
        username: str,
        password: str,
        user_api_base_url: str,
        project_id: sys_uuid.UUID | None = None,
        agent_work_dir: str = c.WORK_DIR,
        use_project_scope: bool = False,
        **collection_map,
    ):
        http = bazooka.Client()
        auth_kwargs = {}
        if use_project_scope:
            auth_kwargs["scope"] = base.CoreIamAuthenticator.project_scope(project_id)
        auth = base.CoreIamAuthenticator(
            user_api_base_url, username, password, http_client=http, **auth_kwargs
        )
        self._collection_map = {k: v.strip() for k, v in collection_map.items()}

        rest_client = base.CollectionBaseClient(
            http_client=http, base_url=user_api_base_url, auth=auth
        )

        storage_path = os.path.join(agent_work_dir, CORE_TARGET_FIELDS_FILENAME)

        storage = fs.TargetFieldsFileStorage(storage_path)
        rest_client = core_rest_back.GCRestApiBackendClient(
            rest_client,
            collection_map,
            project_id=project_id,
            tf_storage=storage,
        )

        super().__init__(storage=storage, client=rest_client)

    def get_capabilities(self) -> list[str]:
        """Returns a list of capabilities supported by the driver."""
        return list(self._collection_map.keys())


class DatabaseCapabilityDriver(direct.DirectAgentDriver):
    """Database capability driver for interacting with GC using database."""

    def __init__(
        self,
        model_specs: tp.Collection[db_back.ModelSpec],
        target_fields_storage_path: str,
        transformer_map: dict[str, direct.ResourceTransformer] | None = None,
    ):

        storage = fs.TargetFieldsFileStorage(target_fields_storage_path)
        client = db_back.DatabaseBackendClient(model_specs, storage)

        self._kinds = {m.kind for m in model_specs}

        super().__init__(
            storage=storage, client=client, transformer_map=transformer_map
        )

    def get_capabilities(self) -> list[str]:
        """Returns a list of capabilities supported by the driver."""
        return list(self._kinds)


class UserCapabilityDriver(direct.DirectAgentDriver):
    """User capability driver."""

    USERS_TARGET_FIELDS_FILENAME = "core_users_target_fields.json"

    def __init__(
        self,
        username: str,
        password: str,
        user_api_base_url: str,
        user_kind: str = "em_core_iam_users",
        agent_work_dir: str = c.WORK_DIR,
    ):
        http = bazooka.Client()
        auth = base.CoreIamAuthenticator(
            user_api_base_url, username, password, http_client=http
        )

        rest_client = base.CollectionBaseClient(
            http_client=http, base_url=user_api_base_url, auth=auth
        )

        storage_path = os.path.join(agent_work_dir, self.USERS_TARGET_FIELDS_FILENAME)

        storage = fs.TargetFieldsFileStorage(storage_path)
        users_client = core_rest_back.GCUsersRestApiBackendClient(
            rest_client,
            user_kind=user_kind,
            tf_storage=storage,
        )

        self._user_kind = user_kind

        super().__init__(storage=storage, client=users_client)

    def get_capabilities(self) -> list[str]:
        """Returns a list of capabilities supported by the driver."""
        return [self._user_kind]
