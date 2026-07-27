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

import collections
import datetime
import json
import logging
import os
import typing as tp
import uuid as sys_uuid

from restalchemy.dm import filters as dm_filters
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import relationships
from restalchemy.dm import types
from restalchemy.storage import exceptions as ra_storage_exceptions
from restalchemy.storage.sql import engines
from restalchemy.storage.sql import filters as sql_filters
from restalchemy.storage.sql import orm
import xxhash

from gcl_sdk.agents.universal import constants as c
from gcl_sdk.agents.universal import utils
from gcl_sdk.agents.universal.api import crypto
from gcl_sdk.common import utils as common_utils

LOG = logging.getLogger(__name__)


class ResourceIdentifier(tp.NamedTuple):
    """A resource identifier.

    The resource identifier is a tuple of kind and UUID.
    """

    kind: str
    uuid: sys_uuid.UUID

    @property
    def res_internal_uuid(self):
        return Resource.gen_res_uuid(self.uuid, self.kind)


# short alias
RI = ResourceIdentifier


class ResourcePair(tp.NamedTuple):
    """A pair of target and actual resources."""

    target_resource: TargetResource
    actual_resource: Resource | None = None


class Payload(models.Model, models.SimpleViewMixin):
    """This model is used to represent the payload of the agent.

    The models is used as for control plane and data plane.
    The control plane payload is received from Orch API and it
    has be applied to the data plane, excepting `facts`.
    A data plane payload is a collected payload from the data.
    If CP and DP payloads are different from target values of
    resources that means we need to update something on the data plane.
    If CP and DP payloads are different from facts point of view,
    it means we need to update something in the Status API to save
    new facts.

    capabilities - a set of managed resources, for example, configuration,
        secrets and so on. An orchestrator sets which resources should be
        presented on the data plane. CP resources from the capabilities
        contains only managed fields. When these resources are gathered
        from the data plane they may have some additional fields,
        for instance, created time, updated time and so on. Only the
        managed fields are orchestrated.

    facts - opposite to the capabilities. These resources are gathered from
        the data plane independently from the orchestrator. In other words,
        they are not managed by the orchestrator. A simple example of facts
        are network interfaces.

    hash - a hash of the payload. The formula is described below:
        hash(
            hash(cap_resource0.hash),
            hash(cap_resource1.hash),
            ...
            hash(fact_resource0.full_hash),
            hash(fact_resource1.full_hash),
            ...
        )

    """

    capabilities = properties.property(types.Dict(), default=dict)
    facts = properties.property(types.Dict(), default=dict)
    hash = properties.property(types.String(max_length=256), default="")
    version = properties.property(types.Integer(min_value=0), default=0, required=True)

    def __hash__(self):
        return hash((self.hash, self.version))

    def __eq__(self, other: Payload) -> bool:
        return self.__hash__() == other.__hash__()

    def calculate_hash(
        self, hash_method: tp.Callable[[str | bytes], str] = xxhash.xxh3_64
    ) -> None:
        m = hash_method()
        caps_resources = self.caps_resources()
        facts_resources = self.facts_resources()
        caps_resources.sort(key=lambda r: r.hash)
        facts_resources.sort(key=lambda r: r.full_hash)
        hashes = [r.hash for r in caps_resources]
        hashes.extend([r.full_hash for r in facts_resources])
        m.update(
            json.dumps(hashes, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        self.hash = m.hexdigest()

    def resources(self) -> list[Resource]:
        """Lists all resources by in the payload."""
        return self.caps_resources() + self.facts_resources()

    def caps_resources(self, capability: str | None = None) -> list[Resource]:
        """
        Lists all resources by capability or all resources if capability is None.
        """
        return self._resources(self.capabilities, capability)

    def add_caps_resource(
        self, resource: Resource, skip_fields: tuple[str, ...] = tuple()
    ) -> None:
        """Add a resource to the capabilities basket."""
        self._add_resource(self.capabilities, resource, skip_fields)

    def add_caps_resources(
        self, resources: list[Resource], skip_fields: tuple[str, ...] = tuple()
    ) -> None:
        """Add resources to the capabilities basket."""
        self._add_resources(self.capabilities, resources, skip_fields)

    def facts_resources(self, fact: str | None = None) -> list[Resource]:
        """
        Lists all resources by fact or all resources if fact is None.
        """
        return self._resources(self.facts, fact)

    def add_facts_resource(
        self, resource: Resource, skip_fields: tuple[str, ...] = tuple()
    ) -> None:
        """Add a resource to the facts basket."""
        self._add_resource(self.facts, resource, skip_fields)

    def add_facts_resources(
        self, resources: list[Resource], skip_fields: tuple[str, ...] = tuple()
    ) -> None:
        """Add resources to the facts basket."""
        self._add_resources(self.facts, resources, skip_fields)

    def save(self, payload_path: str) -> None:
        """Save the payload from the data plane."""
        self.calculate_hash()

        # Create missing directories
        payload_dir = os.path.dirname(payload_path)
        if not os.path.exists(payload_dir):
            os.makedirs(payload_dir)

        payload_data = self.dump_to_simple_view()

        tmp_file = f"{payload_path}.tmp"
        with open(tmp_file, "w", opener=common_utils.rw_owner_opener) as f:
            json.dump(payload_data, f, indent=2)
        os.replace(tmp_file, payload_path)

    @classmethod
    def _resources(cls, source: dict, res_filter: str | None = None) -> list[Resource]:
        """
        Lists all resources by capability/fact or all resources in the basket.
        """
        #  Lists all resources by capability
        if res_filter is not None:
            try:
                data = source[res_filter]["resources"]
            except KeyError:
                return []

            return [Resource.restore_from_simple_view(**r) for r in data]

        # Lists all resources
        resources = []
        for res_filter in source:
            resources.extend(cls._resources(source, res_filter))

        return resources

    @classmethod
    def _add_resource(
        cls,
        dest: dict,
        resource: Resource,
        skip_fields: tuple[str, ...] = tuple(),
    ) -> None:
        try:
            dest[resource.kind]["resources"].append(
                resource.dump_to_simple_view(skip=skip_fields)
            )
        except KeyError:
            dest[resource.kind] = {
                "resources": [resource.dump_to_simple_view(skip=skip_fields)]
            }

    @classmethod
    def _add_resources(
        cls,
        dest: dict,
        resources: list[Resource],
        skip_fields: tuple[str, ...] = tuple(),
    ) -> None:
        for resource in resources:
            cls._add_resource(dest, resource, skip_fields)

    @classmethod
    def empty(cls):
        return cls()

    @classmethod
    def load(cls, payload_path: str) -> Payload:
        """Load the saved payload from the file."""
        if not os.path.exists(payload_path):
            return cls.empty()

        # Load base from the payload file
        with open(payload_path) as f:
            payload_data = json.load(f)
            payload: Payload = Payload.restore_from_simple_view(**payload_data)

        return payload


class UniversalAgent(
    models.ModelWithRequiredUUID,
    models.ModelWithRequiredNameDesc,
    models.ModelWithTimestamp,
    models.SimpleViewMixin,
    orm.SQLStorableMixin,
):
    """Universal Agent model.

    Unified agent that implements common logic of abstract resource and
    fact management.

    The models has helpful tools to APIs, resource, payload and other models.
    """

    __tablename__ = "ua_agents"

    capabilities = properties.property(types.Dict(), default=dict)
    facts = properties.property(types.Dict(), default=dict)
    node = properties.property(types.UUID(), required=True)
    status = properties.property(
        types.Enum([s.value for s in c.AgentStatus]),
        default=c.AgentStatus.NEW.value,
    )

    @property
    def list_capabilities(self) -> list[str]:
        return self.capabilities["capabilities"]

    @property
    def list_facts(self) -> list[str]:
        return self.facts["facts"]

    @property
    def list_kinds(self) -> list[str]:
        return self.list_capabilities + self.list_facts

    @classmethod
    def from_system_uuid(
        cls,
        capabilities: tp.Iterable[str],
        facts: tp.Iterable[str],
        agent_uuid: sys_uuid.UUID | None = None,
        agent_name: str | None = None,
        system_uuid: sys_uuid.UUID | None = None,
    ):
        if system_uuid is None:
            system_uuid = utils.system_uuid()
        uuid = agent_uuid or system_uuid
        capabilities = {"capabilities": list(capabilities)}
        facts = {"facts": list(facts)}
        return cls(
            uuid=uuid,
            name=agent_name or f"Universal Agent {str(uuid)[:8]}",
            status=c.AgentStatus.ACTIVE.value,
            capabilities=capabilities,
            facts=facts,
            # Actually it's won't be true for some cases. For instance,
            # baremetal nodes added by hands. We dont' have such cases
            # so keep it simple so far.
            node=system_uuid,
        )

    def get_payload(self, hash: str = "", version: int = 0) -> Payload:
        # Calculate hash of the target resources
        caps_resources = TargetResource.objects.get_all(
            filters={
                "agent": dm_filters.EQ(str(self.uuid)),
                "kind": dm_filters.In(self.list_capabilities),
            }
        )
        facts_resources = Resource.objects.get_all(
            filters={
                "node": dm_filters.EQ(str(self.node)),
                "kind": dm_filters.In(self.list_kinds),
            }
        )

        payload = Payload.empty()

        payload.add_caps_resources(
            caps_resources,
            skip_fields=(
                "agent",
                "master",
                "master_hash",
                "master_full_hash",
                "tracked_at",
                "node",
            ),
        )
        payload.add_facts_resources(
            facts_resources,
            skip_fields=("node",),
        )
        payload.calculate_hash()

        # TODO(akremenetsky): Add support for versions
        if payload.hash == hash:
            # Return the empty payload with the same hash and version.
            # That means the local value of the agent is correct.
            return Payload(hash=hash, version=version)

        # Fill the payload with data for capabilities with empty resources.
        # Seems all resources for these capabilities were deleted.
        for capability in self.list_capabilities:
            payload.capabilities.setdefault(capability, {"resources": []})

            # All gathered resources for capabilities are consideredas facts too.
            payload.facts.setdefault(capability, {"resources": []})

        for fact in self.list_facts:
            payload.facts.setdefault(fact, {"resources": []})

        LOG.debug("Target and agents payloads are different. Agent %s", self.uuid)
        return payload

    @classmethod
    def have_capabilities(
        cls, capabilities: tp.Collection[str]
    ) -> dict[str, list["UniversalAgent"]]:
        if not capabilities:
            return {}

        caps_str = "'" + "','".join(capabilities) + "'"

        expression = (
            "SELECT * FROM ua_agents ua  "
            "WHERE ua.status = '{status}' AND "
            "      ua.capabilities->'capabilities' ?| "
            "  ARRAY[{caps}]; "
        ).format(status=c.AgentStatus.ACTIVE.value, caps=caps_str)

        engine = engines.engine_factory.get_engine()
        with engine.session_manager() as session:
            curs = session.execute(expression, tuple())
            response = curs.fetchall()

        if not response:
            return {}

        # Group agents by capabilities and return them as dict
        cap_map = collections.defaultdict(list)
        for data in response:
            # There is known issue with date format so we need
            # to handle it manually.
            if created_at := data.get("created_at"):
                data["created_at"] = created_at.replace(tzinfo=datetime.timezone.utc)

            if updated_at := data.get("updated_at"):
                data["updated_at"] = updated_at.replace(tzinfo=datetime.timezone.utc)

            agent = cls(**data)
            caps = set(agent.list_capabilities) & set(capabilities)
            for capability in caps:
                cap_map[capability].append(agent)

        return cap_map


class NodeEncryptionKey(
    models.ModelWithUUID,
    models.ModelWithTimestamp,
    models.SimpleViewMixin,
    orm.SQLStorableMixin,
):
    """API encryption key model for the universal agent APIs."""

    __tablename__ = "ua_node_encryption_keys"

    private_key = properties.property(types.String(max_length=44), required=True)
    encryption_disabled_until = properties.property(
        types.UTCDateTimeZ(),
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )

    @classmethod
    def get_or_create(
        cls,
        node: sys_uuid.UUID,
        private_key: str | None = None,
        session=None,
    ) -> "NodeEncryptionKey":
        """Return the node's encryption key, provisioning one if missing.

        Two callers racing to provision the same node uuid will not
        both succeed - the loser gets ra_storage_exceptions.ConflictRecords
        from the insert. Callers that run on a retry loop (e.g.
        reconciliation) don't need to handle this specially: the next
        attempt finds the winner's key already in place.

        Args:
            node: The node uuid to get or create the key for.
            private_key: If given and a key doesn't exist yet, seeds the
                new key with this value instead of a random one - e.g.
                to keep in sync with a key already deployed to the
                agent's disk by another trusted channel. If a key
                already exists and differs from this value, it's
                updated to match, so a caller that already deployed a
                specific key always ends up in sync with the DB.
            session: Optional DB session.

        Returns:
            The existing or newly created NodeEncryptionKey.
        """
        try:
            key = cls.objects.get_one(
                filters={"uuid": dm_filters.EQ(node)}, session=session
            )
        except ra_storage_exceptions.RecordNotFound:
            if private_key is None:
                _, private_key = crypto.generate_key_base64()
            key = cls(uuid=node, private_key=private_key)
            key.insert(session=session)
            return key

        if private_key is not None and key.private_key != private_key:
            key.private_key = private_key
            key.save(session=session)

        return key


class Resource(
    models.ModelWithID,
    models.ModelWithTimestamp,
    models.SimpleViewMixin,
    orm.SQLStorableMixin,
):
    """This model is represent an abstract resource for the Universal Agent.

    This model is mostly used as an actual resource, for instance, gathered
    from the data plane. In this case the `value` dict contains a real object
    from the data plane in dict format.

    kind - resource kind, for instance, "config", "secret", ...
    value - resource value in dict format.
    hash - hash value only for the target fields.
    full_hash - hash value for the whole value (all fields).
    status - resource status, for instance, "ACTIVE", "NEW", ...

    Some explanation for the `hash` and `full_hash`. Let's assume we have
    the following target node resource:
    {
        "uuid": "a1b2c3d4-e5f6-7890-a1b2-c3d4e5f67890",
        "name": "vm",
        "project_id": "12345678-c625-4fee-81d5-f691897b8142",
        "root_disk_size": 15,
        "cores": 1,
        "ram": 1024,
        "image": "http://10.20.0.1:8080/exordos-base.raw"
    }
    All these fields are considered as target fields and they are used
    to calculate `hash`.

    After node creation we have the the follwing:
    {
        "uuid": "a1b2c3d4-e5f6-7890-a1b2-c3d4e5f67890",
        "name": "vm",
        "project_id": "12345678-c625-4fee-81d5-f691897b8142",
        "root_disk_size": 15,
        "cores": 1,
        "ram": 1024,
        "image": "http://10.20.0.1:8080/exordos-base.raw",

        // Not target fields below
        "created_at": "2022-01-01T00:00:00+00:00",
        "updated_at": "2022-01-01T00:00:00+00:00",
        "default_network": {}
    }

    For hash calculation only the target fields are used as discussed
    above. `full_hash` is calculated for all fields.
    """

    # The default status is `ACTIVE` since it's mostly used when inner
    # object has no status itself. Otherwise the status extracted from
    # the object.
    RESOURCE_DEF_STATUS = "ACTIVE"

    __tablename__ = "ua_actual_resources"

    # Should be the same as `uuid` of internal object that the
    # resource is holding. Therefore isn't unique since we can have
    # the same `uuid` but different `kind`. The simplest example is
    # any resource is created by the Element Manager. We will have
    # two objects with the same `uuid` but different `kind`.
    # - (<uuid_foo>, em_core_object) (EM resource)
    # - (<uuid_foo>, object) (A particular service resource)
    uuid = properties.property(
        types.UUID(),
        read_only=True,
        default=lambda: sys_uuid.uuid4(),
    )
    kind = properties.property(types.String(max_length=64), required=True)
    value = properties.property(types.Dict())
    hash = properties.property(types.String(max_length=256), default="")
    full_hash = properties.property(types.String(max_length=256), default="")
    status = properties.property(
        types.String(max_length=32), default=RESOURCE_DEF_STATUS
    )
    node = properties.property(types.AllowNone(types.UUID()), default=None)

    # The `uuid` field isn't unique, since it's possible to have several
    # resource with the same `uuid` but different `kind`. Therefore the
    # primary key is `res_uuid`. it's useful to have a single unique
    # identifier and it can be any unique UUID but it's recommended to
    # use `uuid5(uuid, kind)`.
    res_uuid = properties.property(
        types.UUID(),
        id_property=True,
        required=True,
        default=lambda: sys_uuid.uuid4(),
    )

    def __eq__(self, other: Resource):
        if isinstance(other, self.__class__):
            return self.uuid == other.uuid and self.kind == other.kind
        return False

    def __ne__(self, other):
        return not self == other

    def __hash__(self):
        return hash((self.uuid, self.kind))

    def calculate_hash(self) -> None:
        self.hash = utils.calculate_hash(self.value)

    def sync_with_origin(self, origin_object) -> None:
        if self.uuid != origin_object.get_resource_uuid():
            raise Exception("sync_with_origin: resource-object UUID mismatch")
        value, target_data = origin_object.get_ua_all_and_target_values()
        self.value = value
        self.hash = utils.calculate_hash(target_data)
        self.full_hash = utils.calculate_hash(value)

    def replace_value(
        self,
        value: dict[str, tp.Any],
        target_fields: frozenset[str] | None = None,
        extract_status: bool = True,
    ) -> Resource:
        """Return a new resource with replaced value and hashes."""
        if target_fields is None:
            hash = self.hash
        else:
            hash = utils.calculate_hash({k: value[k] for k in target_fields})

        if extract_status:
            status = value.get("status", self.status)
        else:
            status = self.status

        return Resource(
            uuid=self.uuid,
            kind=self.kind,
            res_uuid=self.res_uuid,
            value=value,
            hash=hash,
            full_hash=utils.calculate_hash(value),
            status=status,
            node=self.node,
        )

    @property
    def ri(self) -> RI:
        return RI(uuid=self.uuid, kind=self.kind)

    @classmethod
    def gen_res_uuid(cls, uuid: sys_uuid.UUID, kind: str) -> sys_uuid.UUID:
        return sys_uuid.uuid5(uuid, kind)

    @classmethod
    def from_value(
        cls,
        value: dict[str, tp.Any],
        kind: str,
        target_fields: frozenset[str] | None = None,
    ) -> Resource:
        status = value.get("status", "ACTIVE")
        uuid = sys_uuid.UUID(value["uuid"])

        if target_fields is None:
            hash = ""
        else:
            hash = utils.calculate_hash({k: value[k] for k in target_fields})

        return cls(
            uuid=uuid,
            kind=kind,
            res_uuid=cls.gen_res_uuid(uuid, kind),
            value=value,
            hash=hash,
            full_hash=utils.calculate_hash(value),
            status=status,
        )

    @classmethod
    def fetch_by_uuids(
        cls,
        uuids: tp.Collection[sys_uuid.UUID],
        kinds: tp.Collection[str] = tuple(),
    ) -> dict[RI, Resource]:
        filters = {"uuid": dm_filters.In(u for u in uuids)}

        if kinds:
            filters["kind"] = dm_filters.In(kinds)

        return {r.ri: r for r in cls.objects.get_all(filters=filters)}


class TargetResource(Resource):
    """This model is an abstract target resource for the Universal Agent.

    It's pretty close to the `Resource` model but it's used as a target
    resource.

    The fields `master_hash` and `master_full_hash` are used to track
    the master resource hash. It's useful to track the master resource
    hash to determine if the master resource has been changed.

    Args:
        agent: The agent UUID that the resource belongs to.
        master: The master UUID. It's a master resource UUID that the
            resource is related to.
        tracked_at: The tracked at timestamp.
        master_hash: The master hash tracked last time.
        master_full_hash: The master full hash tracked last time.
    """

    __tablename__ = "ua_target_resources"

    agent = properties.property(types.AllowNone(types.UUID()), default=None)
    master = properties.property(types.AllowNone(types.UUID()), default=None)
    tracked_at = properties.property(
        types.UTCDateTimeZ(),
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
    master_hash = properties.property(types.String(max_length=256), default="")
    master_full_hash = properties.property(types.String(max_length=256), default="")

    def update_value(self, other: TargetResource) -> None:
        """Update the resource value."""
        self.hash = other.hash
        self.value = other.value
        self.status = other.status


class ResourceMixin(models.SimpleViewMixin):
    """A helpful mixin to convert models to the resource model."""

    def get_resource_uuid(self) -> sys_uuid.UUID:
        """Get resource uuid."""
        return self.uuid

    def get_resource_target_fields(self) -> tp.Collection[str]:
        """Return the collection of target fields.

        Refer to the Resource model for more details about target fields.
        """
        return set()

    def get_resource_ignore_fields(self) -> tp.Collection[str]:
        """Return fields that should not belong to the resource."""
        return set()

    def get_ua_all_and_target_values(self):
        value = self.dump_to_simple_view(skip=self.get_resource_ignore_fields())

        # Need to get only target fields with values to calculate hash
        target_fields = self.get_resource_target_fields()

        if target_fields:
            target_data = {k: v for k, v in value.items() if k in target_fields}
        else:
            target_data = value
        return value, target_data

    def to_ua_resource(self, kind: str, extract_status: bool = True) -> Resource:
        value, target_data = self.get_ua_all_and_target_values()

        if extract_status and "status" in value:
            status = value["status"]
        else:
            status = Resource.RESOURCE_DEF_STATUS

        return Resource(
            uuid=self.get_resource_uuid(),
            kind=kind,
            res_uuid=Resource.gen_res_uuid(self.get_resource_uuid(), kind),
            value=value,
            hash=utils.calculate_hash(target_data),
            full_hash=utils.calculate_hash(value),
            status=status,
        )

    @classmethod
    def from_ua_resource(cls, resource: Resource) -> "ResourceMixin":
        return cls.restore_from_simple_view(**resource.value)


class TargetResourceMixin(ResourceMixin):
    """A helpful mixin to convert models to the resource model.

    The same as ResourceMixin but it's used as a target resource.
    """

    def to_ua_resource(
        self,
        kind: str,
        master: sys_uuid.UUID | None = None,
        master_hash: str = "",
        master_full_hash: str = "",
        tracked_at: datetime.datetime | None = None,
        extract_status: bool = True,
    ) -> TargetResource:
        tracked_at = tracked_at or datetime.datetime.now(datetime.timezone.utc)
        _, target_data = self.get_ua_all_and_target_values()

        if extract_status and hasattr(self, "status"):
            status = self.status
        else:
            status = Resource.RESOURCE_DEF_STATUS

        return TargetResource(
            uuid=self.get_resource_uuid(),
            kind=kind,
            res_uuid=TargetResource.gen_res_uuid(self.get_resource_uuid(), kind),
            value=target_data,
            hash=utils.calculate_hash(target_data),
            full_hash="",
            master=master,
            master_hash=master_hash,
            master_full_hash=master_full_hash,
            tracked_at=tracked_at,
            status=status,
        )


class TargetResourceSQLStorableMixin:
    @classmethod
    def _execute_expression(
        cls, expression: str, params: tp.Collection, session=None
    ) -> list[dict[str, tp.Any]]:
        if session is None:
            engine = engines.engine_factory.get_engine()
            with engine.session_manager() as session:
                curs = session.execute(expression, params)
                response = curs.fetchall()
        else:
            curs = session.execute(expression, params)
            response = curs.fetchall()

        return response

    @classmethod
    def _to_sql_clause(
        cls, clause: dict[str, dm_filters.AbstractClause]
    ) -> sql_filters.AbstractClause:
        """Fast method to convert clause to the SQL clause."""
        # TODO(akremenetsky): support multiple clauses
        property_name, abs_clause = next(iter(clause.items()))
        property_type = cls.properties.properties[property_name].get_property_type()

        engine = engines.engine_factory.get_engine()
        sql_clause_class = sql_filters.FILTER_MAPPING[engine.dialect.name][
            abs_clause.__class__
        ]

        return sql_clause_class(
            property_name,
            property_type,
            abs_clause.value,
            # No need to pass the session
            None,
        )

    @classmethod
    def get_new_entities(
        cls,
        table: str,
        kind: str,
        clause: dict[str, dm_filters.AbstractClause] | None = None,
        limit: int = 100,
        session=None,
    ) -> list["TargetResourceSQLStorableMixin"]:
        if not clause:
            raw_clause = ""
        else:
            clause_value = next(iter(clause.values())).value
            sql_clause = cls._to_sql_clause(clause)
            raw_clause = f"AND {table}.{sql_clause.construct_expression()} "

        expression = (
            "SELECT "
            "  {table}.uuid as uuid "
            "FROM {table} LEFT JOIN "
            "( "
            "  SELECT "
            "    uuid "
            "  FROM ua_target_resources "
            "  WHERE kind = %s "
            ") AS ua_target_resources_by_kind "
            "ON {table}.uuid = ua_target_resources_by_kind.uuid "
            "WHERE ua_target_resources_by_kind.uuid is NULL {clause}"
            "LIMIT %s;"
        ).format(table=table, clause=raw_clause)
        params = (kind, clause_value, limit) if raw_clause else (kind, limit)

        response = cls._execute_expression(expression, params, session)
        if not response:
            return []

        return cls.objects.get_all(
            filters={"uuid": dm_filters.In(str(r["uuid"]) for r in response)},
        )

    @classmethod
    def get_updated_entities(
        cls,
        table: str,
        kind: str,
        clause: dict[str, dm_filters.AbstractClause] | None = None,
        limit: int = 100,
        session=None,
    ) -> list["TargetResourceSQLStorableMixin"]:
        if not clause:
            raw_clause = ""
        else:
            clause_value = next(iter(clause.values())).value
            sql_clause = cls._to_sql_clause(clause)
            raw_clause = f"AND {table}.{sql_clause.construct_expression()} "

        expression = (
            "SELECT "
            "  {table}.uuid as uuid "
            "FROM {table} INNER JOIN ua_target_resources ON  "
            "  {table}.uuid = ua_target_resources.uuid "
            "WHERE {table}.updated_at != ua_target_resources.tracked_at "
            "AND ua_target_resources.kind = %s {clause}"
            "LIMIT %s;"
        ).format(table=table, clause=raw_clause)
        params = (kind, clause_value, limit) if raw_clause else (kind, limit)

        response = cls._execute_expression(expression, params, session)
        if not response:
            return []

        return cls.objects.get_all(
            filters={"uuid": dm_filters.In(str(r["uuid"]) for r in response)},
        )

    @classmethod
    def get_deleted_target_resources(
        cls, table: str, kind: str, limit: int = 100, session=None
    ) -> list[TargetResource]:
        expression = (
            "SELECT "
            "  ua_target_resources.uuid as uuid "
            "FROM ua_target_resources LEFT JOIN {table} ON  "
            "  ua_target_resources.uuid = {table}.uuid  "
            "WHERE ua_target_resources.kind = %s "
            "AND {table}.uuid is NULL "
            "LIMIT %s;"
        ).format(table=table)
        params = (kind, limit)

        response = cls._execute_expression(expression, params, session)
        if not response:
            return []

        return TargetResource.objects.get_all(
            filters={
                "uuid": dm_filters.In(str(r["uuid"]) for r in response),
                "kind": dm_filters.EQ(kind),
            },
        )

    @classmethod
    def get_outdated_entities(
        cls,
        table: str,
        kind: str,
        clause: dict[str, dm_filters.AbstractClause],
        limit: int = 100,
        session=None,
    ) -> list["ResourcePair"]:
        clause_value = next(iter(clause.values())).value
        sql_clause = cls._to_sql_clause(clause)
        clause = f"AND {table}.{sql_clause.construct_expression()} "

        expression = (
            "SELECT "
            "  * "
            "FROM ua_outdated_resources_view INNER JOIN {table} ON  "
            "  ua_outdated_resources_view.uuid = {table}.uuid "
            "WHERE ua_outdated_resources_view.kind = %s {clause} "
            "LIMIT %s;"
        ).format(table=table, clause=clause)
        params = (kind, clause_value, limit)

        response = cls._execute_expression(expression, params, session)
        if not response:
            return []

        return ResourcePairView.fetch_pairs_by_uuids(
            uuids={r["uuid"] for r in response},
            kinds=(kind,),
        )


class ResourcePairView(models.ModelWithID, orm.SQLStorableMixin):
    __tablename__ = "ua_resource_pair_view"

    kind = properties.property(types.String(max_length=64), required=True)
    uuid = properties.property(types.UUID(), required=True)
    res_uuid = properties.property(types.UUID(), id_property=True, required=True)
    target_resource = relationships.relationship(
        TargetResource,
        prefetch=True,
        required=True,
    )
    actual_resource = relationships.relationship(
        Resource,
        prefetch=True,
    )

    def to_pair(self) -> "ResourcePair":
        return ResourcePair(
            target_resource=self.target_resource,
            actual_resource=self.actual_resource,
        )

    @classmethod
    def fetch_by_uuids(
        cls,
        uuids: tp.Collection[sys_uuid.UUID],
        kinds: tp.Collection[str] = tuple(),
    ) -> list["ResourcePairView"]:
        filters = {"uuid": dm_filters.In(u for u in uuids)}

        if len(kinds) == 1:
            filters["kind"] = dm_filters.EQ(kinds[0])
        elif len(kinds) > 1:
            filters["kind"] = dm_filters.In(kinds)

        return cls.objects.get_all(filters=filters)

    @classmethod
    def fetch_pairs_by_uuids(
        cls,
        uuids: tp.Collection[sys_uuid.UUID],
        kinds: tp.Collection[str] = tuple(),
    ) -> list[ResourcePair]:
        views = cls.fetch_by_uuids(uuids, kinds)
        return [view.to_pair() for view in views]


class OutdatedResource(models.ModelWithUUID, orm.SQLStorableMixin):
    __tablename__ = "ua_outdated_resources_view"

    kind = properties.property(types.String(max_length=64), required=True)
    target_resource = relationships.relationship(
        TargetResource,
        prefetch=True,
        required=True,
    )
    actual_resource = relationships.relationship(
        Resource,
        prefetch=True,
        required=True,
    )

    def to_pair(self) -> "ResourcePair":
        return ResourcePair(
            target_resource=self.target_resource,
            actual_resource=self.actual_resource,
        )


class OutdatedMasterHashResource(models.ModelWithUUID, orm.SQLStorableMixin):
    __tablename__ = "ua_outdated_master_hash_resources_view"

    kind = properties.property(types.String(max_length=64), required=True)
    target_resource = relationships.relationship(
        TargetResource,
        prefetch=True,
        required=True,
    )
    master = relationships.relationship(
        TargetResource,
        prefetch=True,
        required=True,
    )


class OutdatedMasterFullHashResource(models.ModelWithUUID, orm.SQLStorableMixin):
    __tablename__ = "ua_outdated_master_full_hash_resources_view"

    kind = properties.property(types.String(max_length=64), required=True)
    target_resource = relationships.relationship(
        TargetResource,
        prefetch=True,
        required=True,
    )
    master = relationships.relationship(
        TargetResource,
        prefetch=True,
        required=True,
    )


class PlainTrackedResource(
    models.ModelWithUUID, models.ModelWithTimestamp, orm.SQLStorableMixin
):
    __tablename__ = "ua_tracked_resources"

    watcher = properties.property(types.UUID(), required=True)
    target = properties.property(types.UUID(), required=True)
    watcher_kind = properties.property(types.String(max_length=64), required=True)
    target_kind = properties.property(types.String(max_length=64), required=True)
    tracked_at = properties.property(
        types.UTCDateTimeZ(),
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )

    @classmethod
    def fetch_by_watcher_kind(
        cls,
        watcher_kind: str,
        uuids: tp.Collection[sys_uuid.UUID] = tuple(),
    ) -> dict[RI, list[TrackedResource]]:
        """Fetch tracked resources by watcher kind.

        Args:
            watcher_kind: watcher kind
            uuids: Collection of UUIDs to filter by

        Returns:
            Dictionary mapping watcher RI to list of
            TrackedResource
        """

        result = {}
        filters = {"watcher_kind": dm_filters.EQ(watcher_kind)}
        if uuids:
            filters["watcher"] = dm_filters.In(
                RI(watcher_kind, u).res_internal_uuid for u in uuids
            )

        resources = cls.objects.get_all(filters=filters)

        for r in resources:
            result.setdefault(r.watcher.ri, []).append(r)
        return result


class TrackedResource(PlainTrackedResource):
    watcher = relationships.relationship(
        TargetResource,
        prefetch=True,
        required=True,
    )
    target = relationships.relationship(
        TargetResource,
        prefetch=True,
        required=True,
    )

    @property
    def target_ri(self) -> RI:
        return self.target.ri

    @property
    def watcher_ri(self) -> RI:
        return self.watcher.ri

    @classmethod
    def fetch_by_watchers(
        cls,
        watchers: tp.Collection[TargetResource],
    ) -> dict[RI, list[TrackedResource]]:
        """
        Fetch tracked resources by watcher instances.

        Args:
            watchers: Collection of watcher TargetResource instances

        Returns:
            Dictionary mapping watcher RI to list of TrackedResources

        Note:
            This method fetches all tracked resources for the given watcher
            instances and groups them by watcher RI.
        """
        result = {}

        if not watchers:
            return result

        filters = {"watcher": dm_filters.In(watchers)}
        tracked = cls.objects.get_all(filters=filters)

        for tr in tracked:
            result.setdefault(tr.watcher.ri, []).append(tr)
        return result


class OutdatedTrackedResource(models.ModelWithUUID, orm.SQLStorableMixin):
    __tablename__ = "ua_outdated_tracked_resources_view"

    watcher_kind = properties.property(types.String(max_length=64), required=True)

    tracked_resource = relationships.relationship(
        TrackedResource,
        prefetch=True,
        required=True,
    )

    tracked_at = properties.property(
        types.UTCDateTimeZ(),
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
    target_updated_at = properties.property(
        types.UTCDateTimeZ(),
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )

    def mark_as_actualized(self) -> None:
        self.tracked_resource.tracked_at = self.target_updated_at
        self.tracked_resource.update()
        LOG.debug("Tracked resource %s updated", self.tracked_resource.uuid)

    @classmethod
    def fetch_by_watcher_kind(
        cls,
        watcher_kind: str,
        limit: int = c.DEF_SQL_LIMIT,
    ) -> dict[RI, list[OutdatedTrackedResource]]:
        """Fetch outdated tracked full hash resources by watcher kind.

        Args:
            watcher_kind: watcher kind

        Returns:
            Dictionary mapping watcher RI to list of
            OutdatedTrackedResources
        """

        result = {}

        outdated = cls.objects.get_all(
            filters={"watcher_kind": dm_filters.EQ(watcher_kind)},
            limit=limit,
        )

        for o in outdated:
            result.setdefault(o.tracked_resource.watcher.ri, []).append(o)
        return result


class SchedulableToAgentMixin:
    """A helpful mixin to schedule resources to the UA agent for simple cases.

    Actually scheduling is a responsibility of a separated scheduler
    service but in some simple cases an UA agent is known right after
    a resource is created. The mixin provides a way to schedule the
    resource if the UA agent is able to get or calculate from the
    internal data.
    """

    def schedule_to_ua_agent(self, **kwargs) -> sys_uuid.UUID | None:
        """Schedule the resource to the UA agent.

        The method returns the UA agent UUID.

        The UA agent UUID is the same as the resource UUID
        for the simplest case when the resource is scheduled
        to the UA agent. It's convenient if relation between
        the resource and the UA agent is one-to-one.
        """
        return self.uuid


class SchedulableToAgentFromNodeMixin(SchedulableToAgentMixin):
    def schedule_to_ua_agent(self, **kwargs) -> sys_uuid.UUID | None:
        """Schedule the resource to the UA agent.

        The method returns the node UUID that is equal to the
        agent UUID.
        """
        return self.node


class SchedulableToAgentFromAgentUUIDMixin(
    SchedulableToAgentMixin,
    models.Model,
):
    agent_uuid = properties.property(types.AllowNone(types.UUID()), default=None)

    def schedule_to_ua_agent(self, **kwargs) -> sys_uuid.UUID | None:
        """Schedule the resource to the UA agent.

        The method returns the agent UUID.
        """
        return self.agent_uuid


class ReadinessMixin:
    """A helpful mixin to check the resource readiness.

    The mixin provides a way to check if the resource is ready
    to create, update, delete or actualize.
    Returns:
        bool: True if the resource is ready to create, update,
              delete or actualize.
    """

    def is_ready_to_create(self) -> bool:
        """Check if the resource is ready to create."""
        return self.is_ready_to_actualize()

    def is_ready_to_update(self) -> bool:
        """Check if the resource is ready to update."""
        return self.is_ready_to_actualize()

    def is_ready_to_delete(self) -> bool:
        """Check if the resource is ready to delete."""
        return True

    def is_ready_to_actualize(self) -> bool:
        """Check if the resource is ready to actualize."""
        return True


class DependenciesExistReadinessMixin(ReadinessMixin):
    """Check the dependencies exist.

    The resource is considered ready to actualize if all its dependencies
    exist.
    """

    def _fetch_dependencies(
        self,
    ) -> tuple[set[ResourceIdentifier], dict[ResourceIdentifier, TargetResource]]:
        """Fetch all dependencies and return them as a dictionary."""
        # Fetch dependencies
        dependencies = set()
        for dep in self.get_readiness_dependencies():
            if isinstance(dep, ResourceIdentifier):
                dependencies.add(dep)
            else:
                dependencies.add(RI(dep.get_resource_kind(), dep.uuid))

        if len(dependencies) == 0:
            return dependencies, {}

        dep_resources = {
            RI(r.kind, r.uuid): r
            for r in TargetResource.objects.get_all(
                filters={
                    "kind": dm_filters.In(r.kind for r in dependencies),
                    "uuid": dm_filters.In(r.uuid for r in dependencies),
                }
            )
        }

        # Get all possible target resources
        return dependencies, dep_resources

    def get_readiness_dependencies(
        self,
    ) -> tp.Collection["ResourceKindAwareMixin" | ResourceIdentifier]:
        """Get the dependencies to check readiness.

        Returns:
            tp.Collection["ResourceKindAwareMixin" | ResourceIdentifier]:
                The dependencies to check readiness.
        """
        return tuple()

    def is_ready_to_actualize(self) -> bool:
        """Check if the resource is ready to actualize.

        Fetches all dependencies and checks if they exist and are active.
        """
        # Fetch dependencies and get all possible target resources
        dependencies, dep_resources = self._fetch_dependencies()

        # Ensure all dependencies exist
        if dependencies - dep_resources.keys():
            return False

        return True


class DependenciesActiveReadinessMixin(DependenciesExistReadinessMixin):
    """Check the dependencies exist and are active.

    The resource is considered ready to actualize if all its dependencies
    exist and are active.
    """

    def is_ready_to_actualize(self) -> bool:
        """Check if the resource is ready to actualize.

        Fetches all dependencies and checks if they exist and are active.
        """
        # Fetch dependencies and get all possible target resources
        dependencies, dep_resources = self._fetch_dependencies()

        # Ensure all dependencies exist
        if dependencies - dep_resources.keys():
            return False

        # Ensure all dependencies are active
        return all(dep_resources[i].status == "ACTIVE" for i in dependencies)


class KindAwareMixin:
    """A helpful mixin to get the resource kind."""

    @classmethod
    def get_resource_kind(cls) -> str:
        """Return the resource kind."""
        raise NotImplementedError


class ResourceKindAwareMixin(KindAwareMixin, ResourceMixin):
    """A helpful mixin to convert models to the resource model.

    The difference from ResourceMixin is that it has method to get
    the resource kind.
    """

    def to_ua_resource(self) -> Resource:
        return super().to_ua_resource(kind=self.get_resource_kind())

    @property
    def ua_ri(self) -> ResourceIdentifier:
        return ResourceIdentifier(self.get_resource_kind(), self.uuid)


class TargetResourceKindAwareMixin(KindAwareMixin, TargetResourceMixin):
    """A helpful mixin to convert models to the target resource model.

    The difference from TargetResourceMixin is that it has method to get
    the resource kind.
    """

    # The resource status to set on initialization if the default
    # status is not set.
    __init_resource_status__ = None

    def to_ua_resource(
        self,
        master: sys_uuid.UUID | None = None,
        master_hash: str = "",
        master_full_hash: str = "",
        tracked_at: datetime.datetime | None = None,
        status: str | None = None,
    ) -> TargetResource:
        resource = super().to_ua_resource(
            kind=self.get_resource_kind(),
            master=master,
            master_hash=master_hash,
            master_full_hash=master_full_hash,
            tracked_at=tracked_at,
        )
        resource.status = status or self.__init_resource_status__ or resource.status
        return resource

    @classmethod
    def get_one_from_resource_storage(
        cls, uuid: sys_uuid.UUID
    ) -> "TargetResourceKindAwareMixin":
        resource = TargetResource.objects.get_one(
            filters={
                "kind": dm_filters.EQ(cls.get_resource_kind()),
                "uuid": dm_filters.EQ(uuid),
            },
        )
        return cls.from_ua_resource(resource)

    @classmethod
    def get_all_from_resource_storage(
        cls, filters: tp.Dict[str, tp.Any] | None = None
    ) -> tuple["TargetResourceKindAwareMixin"]:
        filters = filters.copy() if filters else {}
        filters["kind"] = dm_filters.EQ(cls.get_resource_kind())

        resources = TargetResource.objects.get_all(filters=filters)
        return tuple(cls.from_ua_resource(resource) for resource in resources)


class InstanceMixin(
    TargetResourceKindAwareMixin,
    TargetResourceSQLStorableMixin,
):
    """This is a core Mixin for models that is going to be used in builder.

    Any models that is going to be used in universal builders should inherit
    from this Mixin since it provides the necessary methods to work with the
    universal builder. Two main group of methods are provided:
    - methods to fetch entities from the database.
    - methods to work with derivatives.

    The derivatives objects are the objects that are created from the instance
    object and strongly coupled with it. For example, the config instance has
    derivatives like renders objects. Single config instance can have multiple
    renders objects.

    The default behavior for the `InstanceMixin` is to not have derivatives.
    """

    # A map of tracked instances kinds to tracked instances models.
    # Example:
    # __tracked_instances_model_map__ = {
    #     "node": Node,
    # }
    __tracked_instances_model_map__: dict | None = None

    @classmethod
    def _has_model_tracked_instances(cls) -> bool:
        """Return `True` if the class has tracked instances objects."""
        return cls.__tracked_instances_model_map__ is not None

    @classmethod
    def tracked_instance_model(cls, kind: str) -> tp.Type["InstanceMixin"]:
        """Return the tracked instance model by kind."""
        if not cls._has_model_tracked_instances():
            raise ValueError("The tracked instance model map is not initialized.")

        if kind not in cls.__tracked_instances_model_map__:
            raise ValueError(
                f"The tracked instance model for kind {kind} is not found."
            )

        return cls.__tracked_instances_model_map__[kind]

    @classmethod
    def _has_model_derivatives(cls) -> bool:
        """Return `True` if the class has derivatives objects.

        For example, config has derivatives like renders."""
        return False

    @classmethod
    def derivative_kinds(cls) -> frozenset[str]:
        """Return available derivative kinds for the instance class."""
        return frozenset()

    @classmethod
    def get_new_instances(
        cls,
        clause: dict[str, dm_filters.AbstractClause] | None = None,
        limit: int = c.DEF_SQL_LIMIT,
    ) -> list["InstanceMixin"]:
        kind = cls.get_resource_kind()
        return cls.get_new_entities(cls.__tablename__, kind, clause, limit)

    @classmethod
    def get_updated_instances(
        cls,
        clause: dict[str, dm_filters.AbstractClause] | None = None,
        limit: int = c.DEF_SQL_LIMIT,
    ) -> list["InstanceMixin"]:
        kind = cls.get_resource_kind()
        return cls.get_updated_entities(cls.__tablename__, kind, clause, limit)

    @classmethod
    def get_outdated_resources(
        cls,
        clause: dict[str, dm_filters.AbstractClause],
        limit: int = c.DEF_SQL_LIMIT,
    ) -> list["ResourcePair"]:
        kind = cls.get_resource_kind()
        return cls.get_outdated_entities(cls.__tablename__, kind, clause, limit)

    @classmethod
    def get_deleted_instances(
        cls, limit: int = c.DEF_SQL_LIMIT
    ) -> list[TargetResource]:
        kind = cls.get_resource_kind()
        return cls.get_deleted_target_resources(cls.__tablename__, kind, limit)

    @classmethod
    def get_filter_clause(cls, **kwargs) -> dict[str, dm_filters.AbstractClause] | None:
        """Get filter clause for the instance model.

        The clause is returned back to the service to take a chance for
        the service enrich the clause. After that the clause is used in
        the database queries. The service haven't must call method
        `get_new_instances` and other with the clause if it was returned.
        It depends on the service implementation.
        """
        return None

    def get_normalized_tracked_resources(
        self,
    ) -> frozenset[ResourceIdentifier]:
        """Return the normalized tracked resources.

        See `get_tracked_resources` for more details. The method returns
        frozenset of ResourceIdentifier objects and does basic validation.
        """
        # Get the tracked instances
        to_track = self.get_tracked_resources()

        if not to_track:
            return frozenset()

        # Normalize the tracked instances to ResourceIdentifier
        instances = frozenset(
            (
                RI(i.kind, i.uuid)
                if isinstance(i, RI)
                else RI(i.get_resource_kind(), i.uuid)
            )
            for i in to_track
        )

        # Ensure all tracked instances are valid
        for i in instances:
            if i.kind not in self.__tracked_instances_model_map__:
                raise ValueError(f"Unknown tracked instance kind: {i.kind}")

        return instances

    def get_tracked_resources(
        self,
    ) -> tp.Collection["ResourceKindAwareMixin" | ResourceIdentifier]:
        """Return the tracked resources.

        Method returns either collection of ResourceKindAwareMixin or
        ResourceIdentifier. If any of resources are changed, a special hooks
        in the UB will be called to actualize the instance.
        """
        return tuple()


class InstanceWithDerivativesMixin(InstanceMixin):
    """A core Mixin for models that is going to be used in builder.

    See the `InstanceMixin` for core functionality. The key difference is
    that this Mixin is focusing on instances that have derivatives.

    The default behavior is to have derivatives objects.
    """

    # A map of derivative kinds to derivative models.
    # Example:
    # __derivative_model_map__ = {
    #     "renders": Render,
    # }
    __derivative_model_map__: dict | None = None

    # The master model for the instance. It's attribute is used in the case
    # if the instance should track changes of the master.
    # Example:
    # __master_model__ = Config
    __master_model__: type[InstanceMixin] | None = None

    @classmethod
    def _has_model_derivatives(cls) -> bool:
        """Return `True` if the class has derivatives objects."""
        return True

    @classmethod
    def derivative_kinds(cls) -> frozenset[str]:
        """Return available derivative kinds for the instance class."""
        if cls.__derivative_model_map__ is None:
            return frozenset()

        return frozenset(cls.__derivative_model_map__.keys())

    @classmethod
    def fetch_all_derivatives_on_outdate(cls) -> bool:
        """Return `True` if need to fetch all derivatives on outdate.

        If the class has derivatives this predicate is used to solve
        how to actualize the instance if it is outdated.
        There are two approaches:
        1. Fetch all derivatives and update the instance.
        2. Fetch only changed derivatives and update the instance.

        Different classes can have different logic to actualize the instance.
        The default implementation is to fetch all derivatives.
        """
        if cls._has_model_derivatives():
            return True

        return False

    @classmethod
    def derivative_model(cls, kind: str) -> tp.Type[TargetResourceKindAwareMixin]:
        """Return the derivative model by kind."""
        if cls.__derivative_model_map__ is None:
            raise ValueError("The derivative model map is not initialized.")

        if kind not in cls.__derivative_model_map__:
            raise ValueError(f"The derivative model for kind {kind} is not found.")

        return cls.__derivative_model_map__[kind]
