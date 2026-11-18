#    Copyright 2026 Genesis Corporation.
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

from unittest import mock
import uuid as sys_uuid

from cryptography.exceptions import InvalidTag
import pytest
import requests

from gcl_sdk.agents.universal.api import crypto
from gcl_sdk.agents.universal.clients.orch import http as orch_http
from gcl_sdk.agents.universal.dm import models
from gcl_sdk.clients.http import base


class TestHttpOrchClientAgentsUpdate:
    """Tests for HttpOrchClient.agents_update.

    `status` is read-only over the API (see UniversalAgentsController) -
    the client must not send its own self-reported value. The server
    activates a re-registering agent itself instead.
    """

    def setup_method(self):
        self.client = orch_http.HttpOrchClient(
            orch_endpoint="http://localhost:11011",
            status_endpoint="http://localhost:11012",
            http_client=mock.MagicMock(),
        )
        self.client._status_api = mock.MagicMock()

    def test_agents_update_does_not_send_status(self):
        agent = models.UniversalAgent.from_system_uuid(
            capabilities=["pool", "local_pool"],
            facts=[],
            agent_uuid=sys_uuid.uuid4(),
            system_uuid=sys_uuid.uuid4(),
        )
        self.client._status_api.agents.update.return_value = agent

        self.client.agents_update(agent)

        self.client._status_api.agents.update.assert_called_once_with(
            agent.uuid,
            capabilities=agent.capabilities,
            facts=agent.facts,
            name=agent.name,
        )


def test_unencrypted_agent_identifies_itself():
    import requests
    from gcl_sdk.clients.http import base

    node = sys_uuid.uuid4()
    request = requests.Request(
        "DELETE",
        "http://boot/v1/kind/fact/resources/resource",
        auth=base.NodeIdentityAuth(node),
    ).prepare()
    assert request.headers["X-Genesis-Node-UUID"] == str(node)


@pytest.mark.parametrize(
    ("status", "body"),
    [(200, b""), (200, b"invalid ciphertext"), (204, b"invalid ciphertext")],
)
def test_encrypted_success_response_requires_valid_ciphertext(status, body):
    key = crypto.generate_key()
    encryptor = base.Encryptor(key, sys_uuid.uuid4())
    response = requests.Response()
    response.status_code = status
    response._content = body
    _, response.headers = encryptor.encrypt({})
    http_client = mock.Mock()
    http_client.get.return_value = response
    client = base.CollectionBaseClient("http://localhost", encryptor=encryptor)

    with pytest.raises(InvalidTag):
        client._do_request(http_client.get, "http://localhost/v1/agents/")
