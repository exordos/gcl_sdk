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

from __future__ import annotations

from restalchemy.common import utils as ra_utils
from restalchemy.dm import filters as dm_filters
from restalchemy.dm import models as ra_models
from restalchemy.dm import properties
from restalchemy.dm import relationships
from restalchemy.dm import types
from restalchemy.storage import base as base_storage
from restalchemy.storage import exceptions as storage_exc
from restalchemy.storage.sql import engines
from restalchemy.storage.sql import orm

from gcl_sdk.agents.universal.dm import models


class KindCollection(base_storage.AbstractObjectCollection):
    def get_all(
        self,
        filters: dict[str, dm_filters.AbstractClause] | None = None,
        limit: int | None = None,
        order_by: dict | None = None,
        locked: bool = False,
    ) -> list[str]:
        expression = "SELECT kind FROM ua_actual_resources GROUP BY kind;"

        engine = engines.engine_factory.get_engine()
        with engine.session_manager() as session:
            curs = session.execute(expression, ())
            response = curs.fetchall()

        return [k["kind"] for k in response]

    def get_one(
        self,
        filters: dict[str, dm_filters.AbstractClause] | None = None,
        locked: bool = False,
    ) -> KindModel:
        # Unable get kind, means the collection cannot be found
        try:
            kind = filters["kind"].value
        except Exception:
            raise storage_exc.RecordNotFound(model=self.model_cls, filters=filters)

        return self.model_cls(kind=kind)


class KindModel(ra_models.ModelWithID):
    kind = properties.property(
        types.String(max_length=64),
        required=True,
        read_only=True,
        id_property=True,
    )

    @ra_utils.classproperty
    def objects(cls):
        return KindCollection(cls)

    @classmethod
    def to_simple_type(cls, value: KindModel | str) -> str:
        if isinstance(value, KindModel):
            return value.kind
        return value

    @classmethod
    def from_simple_type(self, value: str) -> KindModel:
        return KindModel(kind=value)


class Resource(models.Resource):
    kind = relationships.relationship(KindModel, required=True)


# NOTE(akremenetsky): Keep it as a separate class to be able to
# launch functional tests
class UniversalAgent(models.UniversalAgent):
    pass


# NOTE(akremenetsky): Keep it as a separate class to be able to
# launch functional tests
class NodeEncryptionKey(models.NodeEncryptionKey):
    pass


class NodeVerifier(ra_models.ModelWithUUID, orm.SQLStorableMixin):
    """View model for verifying node encryption key existence.

    This is a read-only view on ua_node_encryption_keys table
    containing only the uuid column for node verification purposes.
    """

    __tablename__ = "ua_node_verifiers_view"
