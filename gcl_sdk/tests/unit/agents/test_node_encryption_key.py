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

import pytest
from restalchemy.storage import exceptions as ra_storage_exceptions

from gcl_sdk.agents.universal.dm import models as ua_models


class TestNodeEncryptionKeyGetOrCreate:
    def test_propagates_a_conflict_when_racing_to_insert(self):
        # Two callers both see no key and both try to create one - the
        # loser's insert conflicts on the unique node uuid. This isn't
        # handled specially here: it's left to the caller's own retry
        # (e.g. a reconciliation loop), which finds the winner's key
        # already in place on the next attempt.
        node = sys_uuid.uuid4()
        not_found = ra_storage_exceptions.RecordNotFound(model=None, filters={})
        conflict = ra_storage_exceptions.ConflictRecords(model=None, msg="dup")

        with (
            mock.patch.object(
                ua_models.NodeEncryptionKey,
                "objects",
                mock.MagicMock(get_one=mock.MagicMock(side_effect=not_found)),
            ),
            mock.patch.object(
                ua_models.NodeEncryptionKey, "insert", side_effect=conflict
            ),
            pytest.raises(ra_storage_exceptions.ConflictRecords),
        ):
            ua_models.NodeEncryptionKey.get_or_create(node)
