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

from bazooka import exceptions as bazooka_exc

from gcl_sdk.agents.universal import constants as c
from gcl_sdk.agents.universal.clients.http import core as rest_core
from gcl_sdk.agents.universal.dm import models

LOG = logging.getLogger(__name__)


def register_and_issue_key(
    core_api: rest_core.CoreAPI,
    agent: models.UniversalAgent,
    private_key_path: str = c.PRIVATE_KEY_PATH,
) -> str:
    """Register an external agent against Core and fetch its node's key.

    Registers the agent (updating capabilities/facts if it's already
    registered), issues the private key for the agent's node - agents
    sharing a node uuid get the same key - and writes it to
    `private_key_path`.

    Args:
        core_api: Client for Core's IAM-authenticated agent API.
        agent: The agent to register, e.g. built via
            `UniversalAgent.from_system_uuid()`.
        private_key_path: Where to write the base64-encoded private key.

    Returns:
        The base64-encoded private key.
    """
    try:
        core_api.agents.create(agent)
        LOG.info("Agent registered: %s", agent.uuid)
    except bazooka_exc.ConflictError:
        LOG.warning("Agent already registered: %s", agent.uuid)
        core_api.agents.update(
            agent.uuid,
            capabilities=agent.capabilities,
            facts=agent.facts,
        )

    key_base64 = core_api.agents.issue_key(agent.uuid)

    os.makedirs(os.path.dirname(private_key_path), exist_ok=True)
    with open(private_key_path, "w") as f:
        f.write(key_base64)
    os.chmod(private_key_path, 0o600)

    return key_base64
