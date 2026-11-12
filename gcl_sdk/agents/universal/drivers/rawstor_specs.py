#    Copyright 2026 Genesis Corporation.
#    Licensed under the Apache License, Version 2.0 (the "License")

import enum

from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.dm import types_dynamic

from gcl_sdk.common import types as common_types
from gcl_sdk.infra import constants as ic


class FailureDomain(str, enum.Enum):
    OST = "ost"
    SERVER = "server"
    RACK = "rack"
    ROW = "row"
    DC = "dc"


class PolicyInteger(types.Integer):
    def validate(self, value):
        return not isinstance(value, bool) and super().validate(value)


class ChunkSize(PolicyInteger):
    def __init__(self):
        super().__init__(min_value=1, max_value=1 << 30)

    def validate(self, value):
        return super().validate(value) and value & (value - 1) == 0


_POLICY_FIELDS = {
    "mirrors": PolicyInteger(min_value=1, max_value=255),
    "chunk_size": ChunkSize(),
    "failure_domain": types.Enum([domain.value for domain in FailureDomain]),
}


class RawstorPoolPolicy(common_types.SchematicType):
    """Validated, serialized pool policy in an agent topology snapshot."""

    __scheme__ = {
        **_POLICY_FIELDS,
        "name": types.String(max_length=255),
        "tier": types.Enum([tier.value for tier in ic.DiskTier]),
        "ephemeral": types.Boolean(),
    }
    __mandatory__ = set(__scheme__)

    def validate(self, value):
        return isinstance(value, dict) and super().validate(value)

    def to_openapi_spec(self, prop_kwargs):
        spec = super().to_openapi_spec(prop_kwargs)
        spec["required"] = sorted(self.__mandatory__)
        return spec

    def from_simple_type(self, value):
        # Validate JSON types before Boolean.from_simple_type can coerce them.
        if not self.validate(value):
            raise ValueError("Invalid Rawstor pool policy")
        return super().from_simple_type(value)


class RawstorPoolDriverSpec(types_dynamic.AbstractKindModel, models.SimpleViewMixin):
    KIND = "rawstor"
    mirrors = properties.property(_POLICY_FIELDS["mirrors"], default=2)
    chunk_size = properties.property(_POLICY_FIELDS["chunk_size"], default=1 << 30)
    failure_domain = properties.property(
        _POLICY_FIELDS["failure_domain"], default="server"
    )


class RawstorNodeDriverSpec(types_dynamic.AbstractKindModel, models.SimpleViewMixin):
    KIND = "rawstor"
    endpoint = properties.property(types.String(max_length=2048), required=True)
    location = properties.property(types.String(max_length=2048), default="")
    bind_address = properties.property(types.String(max_length=255), default="")
    failure_domain_path = properties.property(
        types.String(max_length=255), required=True
    )
    weight = properties.property(
        types.Integer(min_value=1, max_value=(1 << 63) - 1), default=100
    )
