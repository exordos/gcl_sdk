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
import logging
import os
import shutil
import sys
import tempfile
import typing as tp

import importlib_metadata
from restalchemy.storage.sql import migrations

if sys.platform.startswith("linux"):
    import renameat2
else:
    renameat2 = None


LOG = logging.getLogger(__name__)

EVENT_PAYLOADS_GROUP = "gcl_sdk_event_payloads"


def ro_owner_opener(path, flags):
    return os.open(path, flags, 0o400)


def rw_owner_opener(path, flags):
    return os.open(path, flags, 0o600)


def load_event_payload_map() -> dict:
    event_payload_map = {
        ep.name: ep.load()
        for ep in importlib_metadata.entry_points(
            group=EVENT_PAYLOADS_GROUP,
        )
    }
    return event_payload_map


def load_from_entry_point(group: str, name: str) -> tp.Any:
    """Load class from entry points."""
    for ep in importlib_metadata.entry_points(group=group):
        if ep.name == name:
            return ep.load()

    raise RuntimeError(f"No class '{name}' found in entry points {group}")


class MigrationEngine(migrations.MigrationEngine):
    """
    Helper for apply library migration from another project.
    Used from the migration file. Example:

    from gcl_sdk.common.utils import MigrationEngine
    from gcl_sdk import migrations as sdk_migrations

    SDK_MIGRATION_FILE_NAME = "0002-init-auditlog-table-c6f740.py"

    class MigrationStep(migrations.AbstarctMigrationStep):
    ...
    def upgrade(self, session):
        migration_engine = MigrationEngine._get_migration_engine(sdk_migrations)
        migration_engine.apply_migration(SDK_MIGRATION_FILE_NAME, session)

    def downgrade(self, session)
        migration_engine = MigrationEngine._get_migration_engine(sdk_migrations)
        migration_engine.rollback_migration(SDK_MIGRATION_FILE_NAME, session)
    """

    @classmethod
    def _get_migration_engine(cls, migrations_module):
        migration_path = os.path.dirname(migrations_module.__file__)
        return cls(migrations_path=migration_path)

    def apply_migration(self, migration_name, session):
        filename = self.get_file_name(migration_name)
        self._init_migration_table(session)
        migrations = self._load_migration_controllers(session)

        migration = migrations[filename]
        if migration.is_applied():
            LOG.warning("Migration '%s' is already applied", migration.name)
        else:
            LOG.info("Applying migration '%s'", migration.name)
            migrations[filename].apply(session, migrations)

    def rollback_migration(self, migration_name, session):
        filename = self.get_file_name(migration_name)
        self._init_migration_table(session)
        migrations = self._load_migration_controllers(session)
        migration = migrations[filename]
        if not migration.is_applied():
            LOG.warning("Migration '%s' is not applied", migration.name)
        else:
            LOG.info("Rolling back migration '%s'", migration.name)
            migrations[filename].rollback(session, migrations)


def swap_dirs(dir1, dir2):
    """
    Platform-independent directory swapping.
    Uses renameat2 on Linux (since it's only available for Linux and is atomic,
    which is required for production use), and plain `os` calls on macOS.
    """
    if renameat2:
        # Linux way:
        renameat2.exchange(dir1, dir2)
    else:
        # Generic (macOS-friendly) way:
        _swap_dirs_mac_compatible(dir1, dir2)


def _swap_dirs_mac_compatible(dir1, dir2):
    """
    Swap the contents (or existence) of two directory paths on macOS/Windows.

    Behavior:
      - If both exist as directories → swap their contents by renaming.
      - If one exists and the other doesn't → move the existing one to the other's path.
      - If either is a file → raises ValueError (directories only).
      - Works across filesystems (uses copy+delete if needed).

    Not atomic.
    """
    dir1_exists = os.path.exists(dir1)
    dir2_exists = os.path.exists(dir2)

    # Validate types: if something exists, it must be a directory
    if dir1_exists and not os.path.isdir(dir1):
        raise ValueError(f"'{dir1}' exists but is not a directory")
    if dir2_exists and not os.path.isdir(dir2):
        raise ValueError(f"'{dir2}' exists but is not a directory")

    # Case 1: Both don't exist → nothing to do
    if not dir1_exists and not dir2_exists:
        return

    # Case 2: Only one exists → just rename it
    if dir1_exists and not dir2_exists:
        os.rename(dir1, dir2)
        return
    if dir2_exists and not dir1_exists:
        os.rename(dir2, dir1)
        return

    # Case 3: Both exist → perform three-way swap using a temporary name
    parent = os.path.dirname(os.path.abspath(dir1))
    if os.path.dirname(os.path.abspath(dir2)) != parent:
        raise ValueError(
            "Both directories must be in the same parent "
            "directory for reliable swapping"
        )

    with tempfile.TemporaryDirectory(dir=parent, prefix=".swap_tmp_") as tmp_dir:
        # Step 1: Move 'dir1' → tmp
        shutil.move(dir1, tmp_dir + "_a")
        # Step 2: Move 'dir2' → 'dir1'
        shutil.move(dir2, dir1)
        # Step 3: Move tmp → 'dir2'
        shutil.move(tmp_dir + "_a", dir2)
