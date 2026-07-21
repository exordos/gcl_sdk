#    Copyright 2026 Genesis Corporation.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License.

from typing import Any

from oslo_config import cfg

from gcl_sdk.audit import constants

CONF = cfg.CONF


def register_audit_delivery_opts(conf: Any = None) -> None:
    conf = conf or CONF

    opts = [
        cfg.BoolOpt(
            "enabled",
            default=False,
            help="Enable delivery of local audit events to central Audit",
        ),
        cfg.URIOpt(
            "endpoint",
            default="http://127.0.0.1:8080/",
            help="Central Audit service endpoint",
        ),
        cfg.StrOpt(
            "api_version",
            choices=["v1"],
            default="v1",
            help="Central Audit API version",
        ),
        cfg.StrOpt(
            "auth_token",
            default=None,
            secret=True,
            help="Bearer token with audit.events.create permission",
        ),
        cfg.IntOpt(
            "timeout",
            default=5,
            min=1,
            help="HTTP request timeout in seconds",
        ),
        cfg.IntOpt(
            "batch_size",
            default=100,
            min=1,
            help="Maximum number of local events processed per iteration",
        ),
    ]

    conf.register_cli_opts(opts, constants.DOMAIN)


def get_audit_delivery_config(conf: Any = None) -> Any:
    conf = conf or CONF
    return conf[constants.DOMAIN]
