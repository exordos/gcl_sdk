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

from restalchemy.storage import exceptions as ra_storage_exceptions

from gcl_sdk.agents.universal.dm import models as ua_models


class TestNodeEncryptionKeyGetOrCreate:
    def test_falls_back_to_the_winner_on_insert_race(self):
        # Two callers both see no key, both try to create one - the
        # loser's insert conflicts on the unique node uuid, so it must
        # fall back to the winner's key instead of raising.
        node = sys_uuid.uuid4()
        winner = mock.MagicMock(private_key="winners-key")
        not_found = ra_storage_exceptions.RecordNotFound(model=None, filters={})
        conflict = ra_storage_exceptions.ConflictRecords(model=None, msg="dup")

        with (
            mock.patch.object(
                ua_models.NodeEncryptionKey,
                "objects",
                mock.MagicMock(
                    get_one=mock.MagicMock(side_effect=[not_found, winner])
                ),
            ),
            mock.patch.object(
                ua_models.NodeEncryptionKey, "insert", side_effect=conflict
            ),
        ):
            key = ua_models.NodeEncryptionKey.get_or_create(node)

        assert key is winner
        # The loser generated its own random key to attempt the insert;
        # once it loses the race that value must be discarded, not
        # synced onto the winner's already-established key.
        assert winner.private_key == "winners-key"
        winner.save.assert_not_called()

    def test_syncs_the_winners_key_to_an_explicit_one_on_race(self):
        node = sys_uuid.uuid4()
        winner = mock.MagicMock(private_key="stale-key")
        not_found = ra_storage_exceptions.RecordNotFound(model=None, filters={})
        conflict = ra_storage_exceptions.ConflictRecords(model=None, msg="dup")

        with (
            mock.patch.object(
                ua_models.NodeEncryptionKey,
                "objects",
                mock.MagicMock(
                    get_one=mock.MagicMock(side_effect=[not_found, winner])
                ),
            ),
            mock.patch.object(
                ua_models.NodeEncryptionKey, "insert", side_effect=conflict
            ),
        ):
            key = ua_models.NodeEncryptionKey.get_or_create(
                node, private_key="fresh-key"
            )

        assert key is winner
        assert winner.private_key == "fresh-key"
        winner.save.assert_called_once()
