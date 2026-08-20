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
import os
import typing as tp
import uuid as sys_uuid

import bazooka

from gcl_sdk.agents.universal import constants as c
from gcl_sdk.agents.universal.clients.backend import core as core_rest_back
from gcl_sdk.agents.universal.clients.backend import db as db_back
from gcl_sdk.agents.universal.drivers import direct
from gcl_sdk.agents.universal.storage import fs
from gcl_sdk.clients.http import base

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


SECRET_TARGET_FIELDS_FILENAME = "core_secret_target_fields.json"


class SecretCapabilityDriver(direct.DirectAgentDriver):
    """Capability driver for GC resources with secrets via REST API.

    Handles models that inherit from ``ModelWithSecret`` (e.g. ``User``,
    ``IamClient``). The secret is hashed in the control plane and cannot
    be restored from the REST API response, so it is enriched from the
    data plane (Resource table).

    Model specs are passed as keyword arguments where the key is the
    resource kind and the value is a string in one of the forms::

        kind = collection, secret_field
        kind = collection, secret_field, filter:field:value

    The optional third part adds a per-kind filter for the ``list``
    method. When no filter is specified, UUID-based filtering from the
    target fields storage is used.

    Example INI configuration::

        [SecretCapabilityDriver]
        username = admin
        password = my-secret-password
        user_api_base_url = http://localhost/v1/api/core
        project_id = 12345678-c625-4fee-81d5-f691897b8142
        use_project_scope = True
        em_core_iam_users = /v1/iam/users/, password
        em_core_iam_clients = /v1/iam/clients/, secret, filter:project_id:12345678-c625-4fee-81d5-f691897b8142
    """

    def __init__(
        self,
        username: str,
        password: str,
        user_api_base_url: str,
        project_id: sys_uuid.UUID | None = None,
        agent_work_dir: str = c.WORK_DIR,
        use_project_scope: bool = False,
        target_fields_filename: str = SECRET_TARGET_FIELDS_FILENAME,
        transformer_map: dict[str, direct.ResourceTransformer] | None = None,
        **model_specs_raw,
    ):
        model_specs = self._parse_model_specs(model_specs_raw)

        http = bazooka.Client()
        auth_kwargs = {}
        if use_project_scope:
            if project_id is None:
                raise ValueError("use_project_scope=True requires project_id to be set")
            auth_kwargs["scope"] = base.CoreIamAuthenticator.project_scope(project_id)
        auth = base.CoreIamAuthenticator(
            user_api_base_url, username, password, http_client=http, **auth_kwargs
        )

        rest_client = base.CollectionBaseClient(
            http_client=http, base_url=user_api_base_url, auth=auth
        )

        storage_path = os.path.join(agent_work_dir, target_fields_filename)

        storage = fs.TargetFieldsFileStorage(storage_path)
        client = core_rest_back.GCSecretRestApiBackendClient(
            rest_client,
            model_specs=model_specs,
            project_id=project_id,
            tf_storage=storage,
        )

        self._kinds = [s.kind for s in model_specs]

        super().__init__(
            storage=storage, client=client, transformer_map=transformer_map
        )

    @classmethod
    def _parse_model_specs(
        cls, raw: dict[str, str]
    ) -> list[core_rest_back.SecretModelSpec]:
        """Parse ``kind = "collection, secret_field[, filter:...]"`` kwargs."""
        specs: list[core_rest_back.SecretModelSpec] = []
        for kind, value in raw.items():
            parts = [p.strip() for p in value.split(",")]
            if len(parts) < 2:
                raise ValueError(
                    f"Invalid model spec for kind '{kind}': expected "
                    f"'collection, secret_field[, filter:field:value]', "
                    f"got '{value}'"
                )
            filters: dict[str, str] | None = None
            if len(parts) >= 3:
                filters = cls._parse_filter(parts[2])
            specs.append(
                core_rest_back.SecretModelSpec(
                    kind=kind,
                    collection=parts[0],
                    secret_field=parts[1],
                    filters=filters,
                )
            )
        return specs

    @staticmethod
    def _parse_filter(raw: str) -> dict[str, str]:
        """Parse ``filter:field:value`` into ``{field: value}``."""
        parts = raw.split(":", 2)
        if len(parts) != 3 or parts[0] != "filter":
            raise ValueError(
                f"Invalid filter spec: expected 'filter:field:value', got '{raw}'"
            )
        return {parts[1]: parts[2]}

    def get_capabilities(self) -> list[str]:
        """Returns a list of capabilities supported by the driver."""
        return list(self._kinds)


class UserCapabilityDriver(SecretCapabilityDriver):
    """User capability driver.

    .. deprecated::
        Use :class:`SecretCapabilityDriver` instead. This class remains
        as a thin wrapper for backward compatibility.
    """

    USERS_TARGET_FIELDS_FILENAME = "core_users_target_fields.json"
    USERS_COLLECTION = "/v1/iam/users/"
    USERS_SECRET_FIELD = "password"

    def __init__(
        self,
        username: str,
        password: str,
        user_api_base_url: str,
        user_kind: str = "em_core_iam_users",
        agent_work_dir: str = c.WORK_DIR,
    ):
        super().__init__(
            username=username,
            password=password,
            user_api_base_url=user_api_base_url,
            agent_work_dir=agent_work_dir,
            target_fields_filename=self.USERS_TARGET_FIELDS_FILENAME,
            **{user_kind: f"{self.USERS_COLLECTION}, {self.USERS_SECRET_FIELD}"},
        )
