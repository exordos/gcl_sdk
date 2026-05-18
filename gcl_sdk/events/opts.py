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

from oslo_config import cfg

from gcl_sdk.events import clients
from gcl_sdk.events import constants as event_c

CONF = cfg.CONF


def register_event_opts(conf=None):
    conf = conf or CONF

    events_opts = [
        cfg.BoolOpt(
            "enabled",
            default=True,
            help="Enable event processing",
        ),
        cfg.StrOpt(
            "client_type",
            default="async",
            choices=list(clients.EVENT_CLIENT_MAPPER.keys()),
            help="Event client type",
        ),
        cfg.StrOpt(
            "event_type_mapping_filepath",
            default="event_type_mapping.yaml",
            help="Event type mapping file path",
        ),
        cfg.URIOpt(
            "exordos_notification_endpoint",
            default="http://127.0.0.1:8080/",
            help="Genesis notification endpoint URL",
        ),
        cfg.StrOpt(
            "exordos_api_version",
            choices=["v1"],
            default="v1",
            help="API version of the Genesis service",
        ),
        cfg.StrOpt(
            "exordos_api_token",
            default="<inser token here>",
            help=("API token for authentication with the Genesis Notificatio service"),
        ),
        cfg.StrOpt(
            "project_id",
            default="00000000-0000-0000-0000-000000000000",
            help="Project ID for the events",
        ),
    ]

    conf.register_cli_opts(events_opts, event_c.DOMAIN)
