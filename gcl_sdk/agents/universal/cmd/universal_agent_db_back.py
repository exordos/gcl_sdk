#    Copyright 2025-2026 Genesis Corporation.
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
import sys
import uuid as sys_uuid

import bazooka
from oslo_config import cfg
from restalchemy.common import config_opts as ra_config_opts
from restalchemy.dm import filters as dm_filters
from restalchemy.storage.sql import engines

from gcl_sdk.agents.universal import constants as c
from gcl_sdk.agents.universal import utils as ua_utils
from gcl_sdk.agents.universal.clients.backend import db as db_back
from gcl_sdk.agents.universal.clients.orch import db as orch_db
from gcl_sdk.agents.universal.clients.orch import http as orch_http
from gcl_sdk.agents.universal.drivers import core as ua_core_drivers
from gcl_sdk.agents.universal.drivers import direct
from gcl_sdk.agents.universal.services import agent
from gcl_sdk.common import config
from gcl_sdk.common import log as infra_log

DOMAIN = "agent"

LOG = logging.getLogger(__name__)


core_agent_opts = [
    cfg.StrOpt(
        "uuid",
        default=None,
        help=("UUID of the agent, if not provided, the system UUID will be used"),
    ),
    cfg.StrOpt(
        "uuid5_name",
        default=None,
        help=(
            "UUID5 name component to generate UUID of the agent. "
            "This option is ignored if the `uuid` is set."
        ),
    ),
    cfg.StrOpt(
        "target_fields_path",
        default="/var/lib/exordos/universal_agent/db_back_target_fields.json",
        help="Path to the target fields storage file.",
    ),
    cfg.StrOpt(
        "orch_client",
        default="db",
        help=("Orch client to use for the core agent. Can be 'db' or 'http'."),
    ),
    cfg.StrOpt(
        "orch_endpoint",
        default="http://localhost:11011",
        help="Endpoint of Exordos Core Orch API for 'http' orch client",
    ),
    cfg.StrOpt(
        "status_endpoint",
        default="http://localhost:11012",
        help="Endpoint of Exordos Core Status API for 'http' orch client",
    ),
    cfg.StrOpt(
        "payload_path",
        default=None,
        help="Path to the payload file.",
    ),
    cfg.BoolOpt(
        "orch_secure_communication",
        default=True,
        help="Enable encrypted communication with the orchestrator APIs.",
    ),
    cfg.StrOpt(
        "private_key_path",
        default=c.PRIVATE_KEY_PATH,
        help="Path to the private key file.",
    ),
    cfg.BoolOpt(
        "verify_node_on_register",
        default=True,
        help="Verify that the node encryption key exists before registering the agent.",
    ),
]

CONF = cfg.CONF
ra_config_opts.register_posgresql_db_opts(CONF)
CONF.register_cli_opts(core_agent_opts, DOMAIN)


def load_filters() -> dict[str, dict[str, dm_filters.AbstractClause]]:
    filters = {}

    for kind, _filter in ua_utils.cfg_load_section_map(
        CONF.config_file, "filters"
    ).items():
        # TODO(akremenetsky): Only simplest 'EQ' filter is supported
        # for now. Add more complex filters support later.
        field, value = _filter.split(":", 1)

        filters[kind] = {field: dm_filters.EQ(value)}

    LOG.info("Loaded filters: %s", filters)

    return filters


def load_transformers(
    kinds: tuple[str, ...],
) -> dict[str, direct.ResourceTransformer]:
    transformer_map = {}
    for kind in kinds:
        section = f"resource_transformer:{kind}"
        transformer = ua_utils.cfg_load_section_map(CONF.config_file, section)
        if not transformer:
            continue

        transformer_map[kind] = direct.ResourceTransformer.from_dict(transformer)

    LOG.info("Loaded transformers: %s", transformer_map)
    return transformer_map


def main():
    # Parse config
    config.parse(sys.argv[1:])

    # Configure logging
    infra_log.configure()
    log = logging.getLogger(__name__)

    engines.engine_factory.configure_postgresql_factory(CONF)

    # Detect the agent UUID.
    if CONF[DOMAIN].uuid:
        agent_uuid = sys_uuid.UUID(CONF[DOMAIN].uuid)
    elif CONF[DOMAIN].uuid5_name:
        agent_uuid = sys_uuid.uuid5(ua_utils.system_uuid(), CONF[DOMAIN].uuid5_name)
    else:
        agent_uuid = ua_utils.system_uuid()

    # Prepare drivers
    facts_drivers = []

    # Prepare models
    models = {}
    models_map = ua_utils.cfg_load_section_map(CONF.config_file, "models")
    for kind, model_path in models_map.items():
        core_model = ua_utils.cfg_load_class(model_path)
        models[kind] = core_model
        LOG.info("Loaded model: %s by %s", core_model, model_path)

    # Prepare filters
    filters = load_filters()

    # Prepare model specs
    specs = []
    for kind, model in models.items():
        spec = db_back.ModelSpec(
            kind=kind,
            model=model,
            filters=filters.get(kind),
        )
        specs.append(spec)

    # Prepare resource transformers
    transformer_map = load_transformers(tuple(models.keys()))

    db_core_driver = ua_core_drivers.DatabaseCapabilityDriver(
        model_specs=specs,
        target_fields_storage_path=CONF[DOMAIN].target_fields_path,
        transformer_map=transformer_map,
    )

    caps_drivers = [
        db_core_driver,
    ]

    # Prepare orch client
    if CONF[DOMAIN].orch_client == "http":
        # Enable encrypted communication with the orchestrator APIs.
        if CONF[DOMAIN].orch_secure_communication:
            encryptor = ua_utils.get_encryptor(CONF[DOMAIN].private_key_path)
        else:
            encryptor = None

        http_client = bazooka.Client(default_timeout=20)
        orch_client = orch_http.HttpOrchClient(
            orch_endpoint=CONF[DOMAIN].orch_endpoint,
            status_endpoint=CONF[DOMAIN].status_endpoint,
            http_client=http_client,
            encryptor=encryptor,
        )
    elif CONF[DOMAIN].orch_client == "db":
        orch_client = orch_db.DatabaseOrchClient()
    else:
        raise ValueError(f"Unknown orch client: {CONF[DOMAIN].orch_client}")

    service = agent.UniversalAgentService(
        agent_uuid=agent_uuid,
        orch_client=orch_client,
        caps_drivers=caps_drivers,
        facts_drivers=facts_drivers,
        payload_path=CONF[DOMAIN].payload_path,
        iter_min_period=3,
        verify_node_on_register=CONF[DOMAIN].verify_node_on_register,
    )

    service.start()

    log.info("Bye!!!")


if __name__ == "__main__":
    main()
