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
        self._depends = ["0006-ua-node-verifier-view-7c852b.py"]

    @property
    def migration_id(self):
        return "4f3a2bc2-9e26-4cc6-a378-df27b04d2aa5"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """
            CREATE TABLE IF NOT EXISTS "gcl_sdk_audit_events" (
                "uuid" UUID PRIMARY KEY,
                "service_name" varchar(128) NOT NULL,
                "resource_type" varchar(128) NOT NULL,
                "resource_uuid" UUID NOT NULL,
                "project_id" UUID DEFAULT NULL,
                "actor_user_uuid" UUID DEFAULT NULL,
                "action" varchar(64) NOT NULL,
                "snapshot" JSON DEFAULT NULL,
                "status" varchar(32) NOT NULL DEFAULT 'NEW' CHECK (
                    status IN ('NEW', 'ERROR')
                ),
                "created_at" TIMESTAMP(6) NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMP(6) NOT NULL DEFAULT NOW()
            );
            """,
            """
            CREATE INDEX IF NOT EXISTS gcl_sdk_audit_events_project_created_idx
                ON gcl_sdk_audit_events (project_id, created_at);
            """,
            """
            CREATE INDEX IF NOT EXISTS gcl_sdk_audit_events_resource_created_idx
                ON gcl_sdk_audit_events (resource_uuid, created_at);
            """,
            """
            CREATE INDEX IF NOT EXISTS gcl_sdk_audit_events_service_resource_created_idx
                ON gcl_sdk_audit_events (
                    service_name, resource_type, created_at
                );
            """,
            """
            CREATE INDEX IF NOT EXISTS gcl_sdk_audit_events_action_created_idx
                ON gcl_sdk_audit_events (action, created_at);
            """,
            """
            CREATE INDEX IF NOT EXISTS gcl_sdk_audit_events_status_created_idx
                ON gcl_sdk_audit_events (status, created_at, uuid);
            """,
            """
            CREATE INDEX IF NOT EXISTS gcl_sdk_audit_events_created_uuid_idx
                ON gcl_sdk_audit_events (created_at, uuid);
            """,
        ]

        for expression in expressions:
            session.execute(expression, None)

    def downgrade(self, session):
        self._delete_table_if_exists(session, "gcl_sdk_audit_events")


migration_step = MigrationStep()
