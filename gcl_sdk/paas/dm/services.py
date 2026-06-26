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

import enum

from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types as ra_types
from restalchemy.dm import types_dynamic as ra_types_dyn

from gcl_sdk.infra import constants as ic


class ServiceTypeSimple(ra_types_dyn.AbstractKindModel, models.SimpleViewMixin):
    KIND = "simple"

    count = properties.property(
        ra_types.Integer(min_value=1, max_value=1000), required=True, default=1
    )


class ServiceTypeOneshot(ra_types_dyn.AbstractKindModel, models.SimpleViewMixin):
    KIND = "oneshot"


class ServiceTypeMonopoly(ra_types_dyn.AbstractKindModel, models.SimpleViewMixin):
    KIND = "monopoly"

    count = properties.property(
        ra_types.Integer(min_value=1, max_value=1), required=True, default=1
    )


class ServiceTypeMonopolyOneshot(
    ra_types_dyn.AbstractKindModel, models.SimpleViewMixin
):
    KIND = "monopoly_oneshot"


class ServiceTarget(ra_types_dyn.AbstractKindModel, models.SimpleViewMixin):
    KIND = "service"

    service = properties.property(ra_types.UUID(), required=True)


class ServiceDPTarget(ServiceTarget):
    """Use to pass service name to dapaplane"""

    service_name = properties.property(ra_types.String(max_length=100), default="")


class CmdShell(ra_types_dyn.AbstractKindModel, models.SimpleViewMixin):
    KIND = "shell"

    command = properties.property(
        ra_types.String(max_length=262144), required=True, default=""
    )

    @classmethod
    def from_command(cls, command: str) -> "CmdShell":
        return cls(command=command)

    def get_dp_obj(self):
        return self


class ServiceTargetStatus(str, enum.Enum):
    enabled = "enabled"
    disabled = "disabled"


class Service(models.ModelWithRequiredUUID):
    """Service model."""

    status = properties.property(
        ra_types.Enum([s.value for s in ic.InstanceStatus]),
        default=ic.InstanceStatus.NEW.value,
    )
    target_status = properties.property(
        ra_types.Enum([s.value for s in ServiceTargetStatus]),
        default=ServiceTargetStatus.enabled.value,
    )
    name = properties.property(ra_types.String(max_length=100), default="")
    path = properties.property(
        ra_types.String(min_length=1, max_length=255),
        required=True,
    )
    user = properties.property(
        ra_types.String(min_length=1, max_length=255),
        required=True,
        default="root",
    )
    group = properties.property(
        ra_types.AllowNone(ra_types.String(min_length=1, max_length=255)),
        default=None,
    )
    service_type = properties.property(
        ra_types_dyn.KindModelSelectorType(
            ra_types_dyn.KindModelType(ServiceTypeSimple),
            ra_types_dyn.KindModelType(ServiceTypeOneshot),
            ra_types_dyn.KindModelType(ServiceTypeMonopoly),
            ra_types_dyn.KindModelType(ServiceTypeMonopolyOneshot),
        ),
        required=True,
    )
    before = properties.property(
        ra_types.TypedList(
            ra_types_dyn.KindModelSelectorType(
                ra_types_dyn.KindModelType(CmdShell),
                ra_types_dyn.KindModelType(ServiceDPTarget),
            ),
        ),
        required=True,
        default=[],
    )
    after = properties.property(
        ra_types.TypedList(
            ra_types_dyn.KindModelSelectorType(
                ra_types_dyn.KindModelType(CmdShell),
                ra_types_dyn.KindModelType(ServiceDPTarget),
            ),
        ),
        required=True,
        default=[],
    )
