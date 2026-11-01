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
        self._depends = ["0007-init-audit-events-4f3a2b.py"]

    @property
    def migration_id(self):
        return "a83d71f4-621e-4e3a-89f7-bd619515bf43"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE "gcl_sdk_audit_events"
                ADD COLUMN "actor" JSON NOT NULL DEFAULT '{"type": "system"}'::json;
            """,
            None,
        )
        session.execute(
            """
            UPDATE "gcl_sdk_audit_events"
                SET "actor" = json_build_object(
                    'type', 'user', 'user_uuid', "actor_user_uuid"::text
                )
                WHERE "actor_user_uuid" IS NOT NULL;
            """,
            None,
        )

    def downgrade(self, session):
        session.execute(
            'ALTER TABLE "gcl_sdk_audit_events" DROP COLUMN "actor";',
            None,
        )


migration_step = MigrationStep()
