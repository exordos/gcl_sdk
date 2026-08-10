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

import configparser
import logging
import sys
import uuid as sys_uuid

import bazooka
from oslo_config import cfg
from restalchemy.common import config_opts as ra_config_opts
from restalchemy.storage.sql import engines

from gcl_sdk.agents.universal import constants as c
from gcl_sdk.agents.universal import utils as ua_utils
from gcl_sdk.agents.universal.clients.orch import http as orch
from gcl_sdk.agents.universal.drivers import base as driver_base
from gcl_sdk.agents.universal.services import agent
from gcl_sdk.common import config
from gcl_sdk.common import log as infra_log
from gcl_sdk.common import utils

DOMAIN = "universal_agent"


core_agent_opts = [
    cfg.StrOpt(
        "orch_endpoint",
        default="http://localhost:11011",
        help="Endpoint of Exordos Core Orch API",
    ),
    cfg.StrOpt(
        "status_endpoint",
        default="http://localhost:11011",
        help="Endpoint of Exordos Core Status API",
    ),
    cfg.ListOpt(
        "caps_drivers",
        default=None,
        help="List of agent capability drivers",
    ),
    cfg.ListOpt(
        "facts_drivers",
        default=None,
        help="List of agent facts drivers",
    ),
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
        "payload_path",
        default=c.PAYLOAD_PATH,
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
CONF.register_cli_opts(core_agent_opts, DOMAIN)


def load_driver(
    class_: type[driver_base.AbstractCapabilityDriver | driver_base.AbstractFactDriver],
) -> driver_base.AbstractCapabilityDriver | driver_base.AbstractFactDriver:
    parser = configparser.ConfigParser()
    parser.read(cfg.CONF.config_file)

    if not parser.has_section(class_.__name__):
        return class_()

    params = {}
    for option in parser.options(class_.__name__):
        if option in parser.defaults():
            continue

        params[option] = parser.get(class_.__name__, option)

    return class_(**params)


def register_db_opts(config_file: str) -> bool:
    print(f"Using config file: {config_file}")
    parser = configparser.ConfigParser()
    parser.read(config_file)

    # Register DB options if they are present
    if parser.has_section("db"):
        ra_config_opts.register_posgresql_db_opts(CONF)
        return True

    return False


def main():
    # Get the config file path
    for i, arg in enumerate(sys.argv):
        if arg == "--config-file":
            config_file = sys.argv[i + 1]
            break
    else:
        raise FileNotFoundError("Unable to find config file")

    # Parse config
    need_db = register_db_opts(config_file)
    config.parse(sys.argv[1:])

    # Configure logging
    infra_log.configure()
    log = logging.getLogger(__name__)

    # Enable encrypted communication with the orchestrator APIs.
    if CONF[DOMAIN].orch_secure_communication:
        encryptor = ua_utils.get_encryptor(CONF[DOMAIN].private_key_path)
    else:
        encryptor = None

    # Prepare clients
    http_client = bazooka.Client(default_timeout=20)
    orch_client = orch.HttpOrchClient(
        orch_endpoint=CONF[DOMAIN].orch_endpoint,
        status_endpoint=CONF[DOMAIN].status_endpoint,
        http_client=http_client,
        encryptor=encryptor,
    )

    # Detect the agent UUID.
    if CONF[DOMAIN].uuid:
        agent_uuid = sys_uuid.UUID(CONF[DOMAIN].uuid)
    elif CONF[DOMAIN].uuid5_name:
        agent_uuid = sys_uuid.uuid5(ua_utils.system_uuid(), CONF[DOMAIN].uuid5_name)
    else:
        agent_uuid = ua_utils.system_uuid()

    # Load drivers from entry points
    caps_drivers = []
    facts_drivers = []

    capabilities = set()
    facts = set()

    if need_db:
        engines.engine_factory.configure_postgresql_factory(CONF)

    # Load capability drivers
    for driver_name in CONF[DOMAIN].caps_drivers or ():
        driver_class = utils.load_from_entry_point(c.EP_UNIVERSAL_AGENT, driver_name)
        driver = load_driver(driver_class)

        # Check for duplicate capabilities
        driver_capabilities = driver.get_capabilities()
        if set(driver_capabilities) & capabilities:
            raise ValueError(f"Driver {driver_name} has duplicate capabilities")
        capabilities |= set(driver_capabilities)

        caps_drivers.append(driver)
        log.info("Loaded driver: %s", driver_name)

    # Load fact drivers
    for driver_name in CONF[DOMAIN].facts_drivers or ():
        driver_class = utils.load_from_entry_point(c.EP_UNIVERSAL_AGENT, driver_name)
        driver = load_driver(driver_class)

        # Check for duplicate capabilities
        driver_facts = driver.get_facts()
        if set(driver_facts) & facts:
            raise ValueError(f"Driver {driver_name} has duplicate facts")
        facts |= set(driver_facts)

        facts_drivers.append(driver)
        log.info("Loaded driver: %s", driver_name)

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
