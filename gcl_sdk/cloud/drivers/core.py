# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0

"""Project-scoped resource operations against another Exordos Core."""

import dataclasses
import json
from urllib.parse import urlsplit
import uuid

import bazooka
from bazooka import exceptions
from bazooka.client import SensitiveMicroserviceSession

from gcl_sdk.clients.http import base


class CoreSession(SensitiveMicroserviceSession):
    @staticmethod
    def _response_body_for_log(response):
        return SensitiveMicroserviceSession.SANITIZED_PLUG


@dataclasses.dataclass(frozen=True)
class CoreConfig:
    connection: uuid.UUID
    api_url: str
    project_id: uuid.UUID
    hypervisors: tuple
    auth: dict = dataclasses.field(repr=False)

    def __post_init__(self):
        parsed = urlsplit(self.api_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Core API URL must be HTTP(S), without credentials")
        if not self.hypervisors or len(set(self.hypervisors)) != len(self.hypervisors):
            raise ValueError("Distinct remote hypervisors are required")
        for ref in (self.connection, self.project_id, *self.hypervisors):
            uuid.UUID(str(ref))
        allowed = {"username", "password", "client_uuid", "client_id", "client_secret"}
        if (
            not isinstance(self.auth, dict)
            or set(self.auth) - allowed
            or not {"username", "password"} <= self.auth.keys()
            or any(not isinstance(v, str) or not v.strip() for v in self.auth.values())
        ):
            raise ValueError("Invalid Core authentication fields")


class CoreDriver:
    RELATIONS = {"security_group_rule", "port_security_group"}

    def __init__(self, config, client=None):
        self.config = config
        self.project = str(config.project_id)
        if client is None:
            http = bazooka.Client(session=CoreSession)
            auth = base.CoreIamAuthenticator(
                config.api_url.rstrip("/"),
                http_client=http,
                scope=base.CoreIamAuthenticator.project_scope(config.project_id),
                **config.auth,
            )
            client = base.CollectionBaseClient(
                config.api_url.rstrip("/"), http_client=http, auth=auth
            )
        self.client = client

    @staticmethod
    def path(kind):
        return {
            "node": "/v1/compute/nodes/",
            "volume": "/v1/compute/volumes/",
            "hypervisor": "/v1/compute/hypervisors/",
            "network": "/v1/network/networks/",
            "subnet": "/v1/network/subnets/",
            "port": "/v1/network/ports/",
            "security_group": "/v1/network/catalog/security_groups/",
            "security_group_rule": "/v1/network/catalog/security_group_rules/",
            "port_security_group": "/v1/network/port_security_groups/",
            "config": "/v1/config/configs/",
        }[kind]

    def resource_id(self, kind, resource):
        if kind == "config":
            return uuid.uuid5(self.resource_id("node", resource), "provider-bootstrap")
        return uuid.uuid5(self.config.connection, f"{kind}:{resource}")

    def marker(self, kind, resource):
        return f"exordos:{self.config.connection}:{kind}:{resource}"

    def identity(self, kind, value):
        if str(value.get("project_id")) != self.project:
            return None
        prefix = f"exordos:{self.config.connection}:{kind}:"
        description = value.get("description", "")
        if not description.startswith(prefix):
            return None
        try:
            identity = uuid.UUID(description[len(prefix) :])
        except ValueError:
            return None
        if str(self.resource_id(kind, identity)) != str(value["uuid"]):
            return None
        return identity

    def get(self, kind, resource):
        identity = self.resource_id(kind, resource)
        try:
            value = self.client.get(self.path(kind), identity)
        except exceptions.NotFoundError:
            return None
        if not self._owns(kind, resource, value):
            raise ValueError("Remote resource ownership mismatch")
        return value

    def _owns(self, kind, resource, value):
        if kind in self.RELATIONS:
            return str(value.get("project_id")) == self.project and str(
                value["uuid"]
            ) == str(self.resource_id(kind, resource))
        return self.identity(kind, value) == uuid.UUID(str(resource))

    def list(self, kind, **filters):
        if kind == "config":
            return [
                v
                for v in self.client.filter(
                    self.path(kind), project_id=self.project, **filters
                )
                if self.identity(kind, v) is not None
            ]
        # Pagination is explicit: an omitted page must never look like deletion.
        result = []
        marker = None
        while True:
            page = self.client.filter(
                self.path(kind),
                project_id=self.project,
                page_limit=100,
                **({"page_marker": marker} if marker else {}),
                **filters,
            )
            result.extend(v for v in page if self.identity(kind, v) is not None)
            if len(page) < 100:
                return result
            next_marker = page[-1]["uuid"]
            if next_marker == marker:
                raise ValueError("Remote pagination did not advance")
            marker = next_marker

    def ensure(self, kind, resource, **attributes):
        value = self.get(kind, resource)
        if value is not None:
            return value
        data = {
            **attributes,
            "uuid": str(self.resource_id(kind, resource)),
            "project_id": self.project,
            "description": self.marker(kind, resource),
        }
        if kind in self.RELATIONS:
            data.pop("description")
            data.pop("name", None)
        try:
            value = self.client.create(self.path(kind), data)
        except exceptions.ConflictError:
            value = self.get(kind, resource)
            if value is None:
                raise
        if not self._owns(kind, resource, value):
            raise ValueError("Remote API did not preserve the resource identity")
        return value

    def update(self, kind, resource, **attributes):
        value = self.get(kind, resource)
        if value is None:
            raise ValueError("Remote resource is not ready")
        changes = {k: v for k, v in attributes.items() if value.get(k) != v}
        if changes:
            value = self.client.update(
                self.path(kind), uuid.UUID(value["uuid"]), **changes
            )
        return value

    def delete(self, kind, resource):
        value = self.get(kind, resource)
        if value is not None:
            try:
                self.client.delete(self.path(kind), uuid.UUID(value["uuid"]))
            except exceptions.NotFoundError:
                pass

    def hypervisors(self):
        result = []
        for identity in self.config.hypervisors:
            value = self.client.get(self.path("hypervisor"), uuid.UUID(str(identity)))
            if value["driver_spec"]["kind"] not in {"libvirt", "exordos_local_hyper"}:
                raise ValueError("A remote Core provider requires native hypervisors")
            if not value["driver_spec"].get("ovs"):
                raise ValueError("Remote hypervisors must support SDN")
            result.append(value)
        return result

    def authenticate(self):
        self.hypervisors()
        self.list("node")
        return {"project_id": self.project}

    @staticmethod
    def metadata(config):
        content = config["body"]["content"]
        line = content.splitlines()[0]
        prefix = "# exordos-provider "
        if not line.startswith(prefix):
            raise ValueError("Invalid provider bootstrap metadata")
        return json.loads(line[len(prefix) :])
