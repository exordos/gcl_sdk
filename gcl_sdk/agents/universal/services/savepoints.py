# Copyright 2017 Eugene Frolov <eugene@frolov.net.ru>
#
# All Rights Reserved.
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
"""Transactional savepoint helpers for the universal builder.

This module mirrors :func:`restalchemy.storage.sql.utils.savepoint` but
defers the ``RELEASE`` on the success path.

The universal builder wraps every per-instance operation in a savepoint so a
single failing instance does not abort the whole tick. The operations are
executed sequentially inside one shared transaction and always reuse the same
savepoint name.

In PostgreSQL (and MySQL) issuing ``SAVEPOINT <name>`` with an already
existing name implicitly releases the previous savepoint of the same name,
and committing or rolling back the enclosing transaction releases every
remaining savepoint. Therefore an explicit ``RELEASE`` after a successful
instance carries no observable effect and only costs one extra round-trip per
processed instance. Skipping it halves the savepoint overhead on the hot path
(a savepoint decorated success goes from ``SAVEPOINT`` + ``RELEASE`` down to a
single ``SAVEPOINT``) while keeping the exact rollback semantics on failure.
"""

from __future__ import annotations

import contextlib
import logging
import typing as tp

from restalchemy.common import contexts
from restalchemy.storage.sql.dialect import mysql as mysql_dialect
from restalchemy.storage.sql.dialect import pgsql as pgsql_dialect

LOG = logging.getLogger(__name__)

DEFAULT_SAVEPOINT_NAME = "default_savepoint"


@contextlib.contextmanager
def savepoint(name: str = DEFAULT_SAVEPOINT_NAME) -> tp.Iterator[tp.Any]:
    """Create a savepoint and roll back to it on error, deferring release.

    The function can be used as a decorator or a context manager. For
    example::

        @savepoint()
        def my_function():
            pass

        with savepoint() as session:
            pass

    Unlike the stock restalchemy helper the ``RELEASE`` statement is issued
    only on the error path. On success the savepoint is intentionally left
    in place: it is either superseded by the next ``SAVEPOINT`` with the same
    name or discarded when the enclosing transaction is committed.
    """
    if not name.isidentifier():
        raise ValueError(f"Invalid savepoint name: {name}")

    ctx = contexts.Context()
    engine = ctx._engine
    dialect_name = engine.dialect.name

    if dialect_name == pgsql_dialect.PgSQLDialect.DIALECT_NAME:
        expression_map = pgsql_dialect.SAVEPOINT_EXP_MAP
    elif dialect_name == mysql_dialect.MySQLDialect.DIALECT_NAME:
        expression_map = mysql_dialect.SAVEPOINT_EXP_MAP
    else:
        raise ValueError("Unsupported database dialect: %s" % dialect_name)

    savepoint_exp = expression_map["savepoint"].format(name=name)
    rollback_exp = expression_map["rollback"].format(name=name)
    release_exp = expression_map["release"].format(name=name)

    session = ctx.get_session()
    session.execute(savepoint_exp, tuple())

    try:
        yield session
    except Exception:
        LOG.error("Exception occurred, rolling back to savepoint")
        session.execute(rollback_exp, tuple())
        session.execute(release_exp, tuple())
        raise
