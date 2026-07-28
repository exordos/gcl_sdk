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

from unittest import mock
import uuid as sys_uuid

from gcl_sdk.agents.universal.clients.http import status as status_client

NODE_UUID = sys_uuid.UUID("55c6968c-d26e-58a3-cfe2-11afde248319")


class TestNodeVerifiersClient:
    """Tests for NodeVerifiersClient."""

    def setup_method(self):
        self.client = status_client.NodeVerifiersClient(
            "http://localhost:8080",
            http_client=mock.MagicMock(),
        )

    def test_verify_exists_true(self):
        """Test verify returns True when node exists."""
        self.client.do_action = mock.MagicMock(return_value={"valid": True})

        result = self.client.verify(NODE_UUID)

        assert result is True
        self.client.do_action.assert_called_once_with("verify", NODE_UUID)

    def test_verify_exists_false(self):
        """Test verify returns False when node doesn't exist."""
        self.client.do_action = mock.MagicMock(return_value={"valid": False})

        result = self.client.verify(NODE_UUID)

        assert result is False
        self.client.do_action.assert_called_once_with("verify", NODE_UUID)

    def test_verify_default_false(self):
        """Test verify returns False when valid key is missing."""
        self.client.do_action = mock.MagicMock(return_value={})

        result = self.client.verify(NODE_UUID)

        assert result is False
