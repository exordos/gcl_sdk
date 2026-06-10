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
from restalchemy.storage.sql import migrations


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0005-ua-api-encryption-keys-2f8d3a.py"]

    @property
    def migration_id(self):
        return "7c852bde-ca9b-47c3-ad2d-eb1fbe6d8f23"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        # Create a view on ua_node_encryption_keys table
        # exposing only the uuid column for node verification
        session.execute(
            """CREATE OR REPLACE VIEW ua_node_verifiers_view AS
                SELECT uuid FROM ua_node_encryption_keys;""",
            None,
        )

    def downgrade(self, session):
        # Drop the view
        session.execute(
            "DROP VIEW IF EXISTS ua_node_verifiers_view;",
            None,
        )


migration_step = MigrationStep()
