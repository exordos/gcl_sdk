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

import functools
import itertools
import uuid as sys_uuid

import pytest
from restalchemy.api import applications
from restalchemy.api import controllers
from restalchemy.api import middlewares
from restalchemy.api import routes
from restalchemy.api.middlewares import contexts as context_mw
from restalchemy.api.middlewares import errors as errors_mw
from restalchemy.api.middlewares import logging as logging_mw
from restalchemy.dm import models as ra_models
from restalchemy.dm import properties
from restalchemy.dm import types as ra_types
from restalchemy.openapi import engines as openapi_engines
from restalchemy.openapi import structures as openapi_structures

from gcl_sdk.agents.universal import constants as c
from gcl_sdk.agents.universal.api import middlewares as sdk_middlewares
from gcl_sdk.agents.universal.api import packers
from gcl_sdk.agents.universal.dm import models
from gcl_sdk.agents.universal.orch_api import routes as orch_routes
from gcl_sdk.agents.universal.status_api import routes as status_routes
from gcl_sdk.audit.api import routes as audit_routes

FIRST_MIGRATION = "0000-init-events-table-2cfd220e.py"


class FooResource(ra_models.ModelWithUUID, models.ResourceMixin):
    name = properties.property(ra_types.String(max_length=64), default="foo-name")
    project_id = properties.property(ra_types.UUID(), default=lambda: sys_uuid.uuid4())


class FooTargetResource(ra_models.ModelWithUUID, models.TargetResourceMixin):
    name = properties.property(ra_types.String(max_length=64), default="foo-name")
    project_id = properties.property(ra_types.UUID(), default=lambda: sys_uuid.uuid4())


class DummyInstance(
    ra_models.ModelWithUUID,
    ra_models.ModelWithTimestamp,
    models.InstanceMixin,
):
    NAMESPACE = sys_uuid.UUID("14cf9662-6297-4ce8-92aa-9568a75f352e")

    __dummy_storage__ = {
        "new": [],
        "updated": [],
        "deleted": [],
        "existing": [],
    }

    name = properties.property(ra_types.String(max_length=64), default="foo-name")
    project_id = properties.property(ra_types.UUID(), default=lambda: sys_uuid.uuid4())

    def save(self, **kwargs):
        pass

    def delete(self, **kwargs):
        pass

    def update(self, **kwargs):
        pass

    @classmethod
    def get_resource_kind(cls):
        return "foo"

    @classmethod
    def _has_model_derivatives(cls) -> bool:
        return False

    @classmethod
    def get_new_instances(cls, limit: int = c.DEF_SQL_LIMIT) -> list[DummyInstance]:
        return cls.__dummy_storage__["new"]

    @classmethod
    def get_updated_instances(cls, limit: int = c.DEF_SQL_LIMIT) -> list[DummyInstance]:
        return cls.__dummy_storage__["updated"]

    @classmethod
    def get_deleted_instances(
        cls, limit: int = c.DEF_SQL_LIMIT
    ) -> list[models.TargetResource]:
        return cls.__dummy_storage__["deleted"]


class DummyDerivative(
    ra_models.ModelWithUUID,
    models.TargetResourceKindAwareMixin,
):
    NAMESPACE = sys_uuid.UUID("5bf3b9e4-8975-44d9-ade5-e7e1abcf408a")

    __dummy_storage__ = {
        "new": [],
        "updated": [],
        "deleted": [],
        "existing": [],
    }

    name = properties.property(ra_types.String(max_length=64), default="foo-name")
    project_id = properties.property(ra_types.UUID(), default=lambda: sys_uuid.uuid4())

    @classmethod
    def get_resource_kind(cls) -> str:
        return "foo-derivative"


class DummyInstanceWithDerivatives(DummyInstance, models.InstanceWithDerivativesMixin):
    __derivative_model_map__ = {
        "foo-derivative": DummyDerivative,
    }

    @classmethod
    def _has_model_derivatives(cls) -> bool:
        return True

    def to_derivative(self) -> DummyDerivative:
        return DummyDerivative(
            uuid=sys_uuid.uuid5(self.uuid, self.name),
            name=self.name,
            project_id=self.project_id,
        )


class DummySubInstanceWithDerivatives(
    DummyInstance, models.InstanceWithDerivativesMixin
):
    __master_model__ = DummyInstanceWithDerivatives
    __derivative_model_map__ = {
        "foo-derivative": DummyDerivative,
    }

    @classmethod
    def _has_model_derivatives(cls) -> bool:
        return True

    @classmethod
    def get_resource_kind(cls) -> str:
        return "sub-foo"

    def to_derivative(self) -> DummyDerivative:
        return DummyDerivative(
            uuid=sys_uuid.uuid5(self.uuid, self.name),
            name=self.name,
            project_id=self.project_id,
        )


def get_openapi_engine():
    openapi_engine = openapi_engines.OpenApiEngine(
        info=openapi_structures.OpenApiInfo(
            title="Test API",
            version="v1",
            description="OpenAPI - Test API",
        ),
        paths=openapi_structures.OpenApiPaths(),
    )
    return openapi_engine


@pytest.fixture(scope="module")
def orch_api_wsgi_app():
    class OrchApiApp(routes.RootRoute):
        pass

    class ApiEndpointController(controllers.RoutesListController):
        __TARGET_PATH__ = "/v1/"

    class ApiEndpointRoute(routes.Route):
        __controller__ = ApiEndpointController
        __allow_methods__ = [routes.FILTER]

        agents = routes.route(orch_routes.UniversalAgentsRoute)

    OrchApiApp.v1 = routes.route(ApiEndpointRoute)

    return middlewares.attach_middlewares(
        applications.OpenApiApplication(
            route_class=OrchApiApp,
            openapi_engine=get_openapi_engine(),
        ),
        [
            functools.partial(
                sdk_middlewares.SdkContextMiddleware,
                context_kwargs={
                    "encryption_information_class": (packers.NoEncryptionInformation)
                },
            ),
            errors_mw.ErrorsHandlerMiddleware,
            logging_mw.LoggingMiddleware,
        ],
    )


@pytest.fixture(scope="module")
def status_api_wsgi_app():
    class StatusApiApp(routes.RootRoute):
        pass

    class ApiEndpointController(controllers.RoutesListController):
        __TARGET_PATH__ = "/v1/"

    class ApiEndpointRoute(routes.Route):
        __controller__ = ApiEndpointController
        __allow_methods__ = [routes.FILTER]

        agents = routes.route(status_routes.UniversalAgentsRoute)
        node_verifiers = routes.route(status_routes.NodeVerifiersRoute)
        kind = routes.route(status_routes.KindRoute)

    StatusApiApp.v1 = routes.route(ApiEndpointRoute)

    return middlewares.attach_middlewares(
        applications.OpenApiApplication(
            route_class=StatusApiApp,
            openapi_engine=get_openapi_engine(),
        ),
        [
            functools.partial(
                sdk_middlewares.SdkContextMiddleware,
                context_kwargs={
                    "encryption_information_class": (packers.NoEncryptionInformation)
                },
            ),
            errors_mw.ErrorsHandlerMiddleware,
            logging_mw.LoggingMiddleware,
        ],
    )


@pytest.fixture(scope="module")
def audit_api_wsgi_app():
    class AuditApiApp(routes.RootRoute):
        pass

    class ApiEndpointController(controllers.RoutesListController):
        __TARGET_PATH__ = "/v1/"

    class ApiEndpointRoute(routes.Route):
        __controller__ = ApiEndpointController
        __allow_methods__ = [routes.FILTER]

        audit = routes.route(audit_routes.AuditRoute)

    AuditApiApp.v1 = routes.route(ApiEndpointRoute)

    return middlewares.attach_middlewares(
        applications.OpenApiApplication(
            route_class=AuditApiApp,
            openapi_engine=get_openapi_engine(),
        ),
        [
            context_mw.ContextMiddleware,
            errors_mw.ErrorsHandlerMiddleware,
            logging_mw.LoggingMiddleware,
        ],
    )


@pytest.fixture
def dummy_instance_factory():

    def factory(
        spec: dict | None = None, project_id: sys_uuid.UUID | None = None
    ) -> list[DummyInstance]:
        storage = {
            "new": [],
            "updated": [],
            "deleted": [],
            "existing": [],
        }
        project_id = project_id or sys_uuid.uuid4()

        if spec is None:
            spec = {"new": 1}

        for mode, count in spec.items():
            for i in range(count):
                uuid = sys_uuid.uuid5(DummyInstance.NAMESPACE, f"{mode}-{i}")
                name = f"inst-{mode}-{i}"

                instance = DummyInstance(uuid=uuid, name=name, project_id=project_id)
                if mode == "deleted":
                    instance = instance.to_ua_resource()
                elif mode == "existing":
                    resource = instance.to_ua_resource()
                    resource.save()
                elif mode == "updated":
                    resource = instance.to_ua_resource()
                    instance.name = f"{name}-updated"
                    resource.save()

                storage[mode].append(instance)

        DummyInstance.__dummy_storage__ = storage

        return list(itertools.chain.from_iterable(storage.values()))

    yield factory

    DummyInstance.__dummy_storage__ = {
        "new": [],
        "updated": [],
        "deleted": [],
        "existing": [],
    }


@pytest.fixture
def dummy_instance_with_derivatives_factory():

    def factory(
        spec: dict | None = None,
        project_id: sys_uuid.UUID | None = None,
        model: type[DummyInstance] = DummyInstanceWithDerivatives,
    ) -> list[DummyInstance]:
        storage = {
            "new": [],
            "updated": [],
            "deleted": [],
            "existing": [],
        }
        project_id = project_id or sys_uuid.uuid4()

        if spec is None:
            spec = {"new": 1}

        for mode, count in spec.items():
            for i in range(count):
                uuid = sys_uuid.uuid5(model.NAMESPACE, f"{mode}-{i}")
                name = f"inst-{mode}-{i}"

                instance = model(uuid=uuid, name=name, project_id=project_id)
                if master_model := getattr(model, "__master_model__", None):
                    master_instance = master_model(
                        uuid=uuid, name=name, project_id=project_id
                    )
                    master_resource = master_instance.to_ua_resource()
                    master_instance.save()
                    master_resource.save()
                else:
                    master_instance = None

                if mode == "deleted":
                    instance = instance.to_ua_resource()
                elif mode == "existing":
                    derivative = instance.to_derivative()
                    resource = instance.to_ua_resource(
                        master=(master_instance.uuid if master_instance else None),
                        master_hash=(master_resource.hash if master_instance else ""),
                        master_full_hash=(
                            master_resource.full_hash if master_instance else ""
                        ),
                    )
                    derivative_resource = derivative.to_ua_resource(
                        master=resource.uuid,
                        master_hash=resource.hash,
                        master_full_hash=resource.full_hash,
                    )
                    resource.save()
                    derivative_resource.save()

                elif mode == "updated":
                    derivative = instance.to_derivative()
                    resource = instance.to_ua_resource()
                    derivative_resource = derivative.to_ua_resource(
                        master=resource.uuid,
                        master_hash=resource.hash,
                        master_full_hash=resource.full_hash,
                    )
                    instance.name = f"{name}-updated"
                    resource.save()
                    derivative_resource.save()

                storage[mode].append(instance)

        model.__dummy_storage__ = storage

        return list(itertools.chain.from_iterable(storage.values()))

    yield factory

    DummyInstanceWithDerivatives.__dummy_storage__ = {
        "new": [],
        "updated": [],
        "deleted": [],
        "existing": [],
    }

    DummySubInstanceWithDerivatives.__dummy_storage__ = {
        "new": [],
        "updated": [],
        "deleted": [],
        "existing": [],
    }
