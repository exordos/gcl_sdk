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

import contextlib
import os

from oslo_config import cfg
from restalchemy.storage.sql import engines
from restalchemy.storage.sql import migrations

CONF = cfg.CONF


INIT_MIGRATION_FILENAME = "0000-init-events-table-2cfd220e"


@contextlib.contextmanager
def _database_session(conf, db_type, name, **kwargs):
    conf = conf or CONF

    configure_database_factory = {
        "postgresql": engines.engine_factory.configure_postgresql_factory,
        "mysql": engines.engine_factory.configure_mysql_factory,
    }

    configure_database_factory[db_type](conf=conf, name=name, **kwargs)

    try:
        yield
    finally:
        engines.engine_factory.destroy_engine(name=name)


def _get_migration_engine():
    migration_engine = migrations.MigrationEngine(
        migrations_path=os.path.dirname(__file__)
    )
    return migration_engine


def apply_migrations(
    conf=None, db_type="postgresql", name=engines.DEFAULT_NAME, **kwargs
):
    with _database_session(conf, db_type, name=name, **kwargs):
        migration_engine = _get_migration_engine()
        last_migration = migration_engine.get_latest_migration()
        migration_engine.apply_migration(last_migration)


def rollback_migrations(
    conf=None, db_type="postgresql", name=engines.DEFAULT_NAME, **kwargs
):
    with _database_session(conf, db_type, name=name, **kwargs):
        migration_engine = _get_migration_engine()
        migration_engine.rollback_migration(INIT_MIGRATION_FILENAME)
