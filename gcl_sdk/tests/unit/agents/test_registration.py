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

import os
from unittest import mock
import uuid as sys_uuid

from bazooka import exceptions as bazooka_exc

from gcl_sdk.agents.universal import registration
from gcl_sdk.agents.universal.dm import models

AGENT_UUID = sys_uuid.UUID("55c6968c-d26e-58a3-cfe2-11afde248319")
NODE_UUID = sys_uuid.UUID("6f9c968c-d26e-58a3-cfe2-11afde248319")


def _make_agent() -> models.UniversalAgent:
    return models.UniversalAgent.from_system_uuid(
        capabilities=["test_capability"],
        facts=[],
        agent_uuid=AGENT_UUID,
        system_uuid=NODE_UUID,
    )


class TestRegisterAndIssueKey:
    def test_registers_new_agent_and_writes_key(self, tmp_path):
        agent = _make_agent()
        core_api = mock.MagicMock()
        core_api.agents.issue_key.return_value = "s3cr3t"
        key_path = tmp_path / "universal_agent" / "private_key"

        result = registration.register_and_issue_key(
            core_api, agent, private_key_path=str(key_path)
        )

        assert result == "s3cr3t"
        core_api.agents.create.assert_called_once_with(agent)
        core_api.agents.update.assert_not_called()
        core_api.agents.issue_key.assert_called_once_with(agent.uuid)
        assert key_path.read_text() == "s3cr3t"
        assert oct(os.stat(key_path).st_mode & 0o777) == "0o600"

    def test_updates_existing_agent_on_conflict(self, tmp_path):
        agent = _make_agent()
        core_api = mock.MagicMock()
        core_api.agents.create.side_effect = bazooka_exc.ConflictError(
            cause=mock.MagicMock()
        )
        core_api.agents.issue_key.return_value = "s3cr3t"
        key_path = tmp_path / "private_key"

        result = registration.register_and_issue_key(
            core_api, agent, private_key_path=str(key_path)
        )

        assert result == "s3cr3t"
        core_api.agents.update.assert_called_once_with(
            agent.uuid,
            capabilities=agent.capabilities,
            facts=agent.facts,
        )
        assert key_path.read_text() == "s3cr3t"
