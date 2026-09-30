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

from restalchemy.storage.sql import migrations


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0007-init-audit-events-4f3a2b.py"]

    @property
    def migration_id(self):
        return "78ea11df-1145-4230-bd2a-b28456b6268f"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        # A pair whose value is the same but whose hash has just caught up
        # (new target fields) still has an outdated target status. List it
        # too, otherwise the builders never actualize the target status.
        session.execute(
            """
            CREATE OR REPLACE VIEW ua_outdated_resources_view AS
                SELECT
                    ua_target_resources.uuid as uuid,
                    ua_target_resources.kind as kind,
                    ua_target_resources.res_uuid as target_resource,
                    ua_actual_resources.res_uuid as actual_resource
                FROM ua_target_resources INNER JOIN ua_actual_resources ON
                    ua_target_resources.res_uuid = ua_actual_resources.res_uuid
                WHERE ua_target_resources.full_hash != ua_actual_resources.full_hash
                    OR (
                        ua_target_resources.hash = ua_actual_resources.hash
                        AND ua_target_resources.status != ua_actual_resources.status
                    );
            """,
            None,
        )

    def downgrade(self, session):
        session.execute(
            """
            CREATE OR REPLACE VIEW ua_outdated_resources_view AS
                SELECT
                    ua_target_resources.uuid as uuid,
                    ua_target_resources.kind as kind,
                    ua_target_resources.res_uuid as target_resource,
                    ua_actual_resources.res_uuid as actual_resource
                FROM ua_target_resources INNER JOIN ua_actual_resources ON
                    ua_target_resources.res_uuid = ua_actual_resources.res_uuid
                WHERE ua_target_resources.full_hash != ua_actual_resources.full_hash;
            """,
            None,
        )


migration_step = MigrationStep()
