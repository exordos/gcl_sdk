#    Copyright 2026 Genesis Corporation.
#    Licensed under the Apache License, Version 2.0 (the "License")

from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.dm import types_dynamic


class RawstorPoolDriverSpec(types_dynamic.AbstractKindModel, models.SimpleViewMixin):
    KIND = "rawstor"
    mirrors = properties.property(types.Integer(min_value=1, max_value=255), default=2)
    chunk_size = properties.property(types.Integer(min_value=1), default=1 << 30)
    failure_domain = properties.property(
        types.Enum(["ost", "server", "rack", "row", "dc"]), default="server"
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
