#    Copyright 2026 Genesis Corporation.
#    Licensed under the Apache License, Version 2.0 (the "License").

import uuid

import pytest
from restalchemy.common import exceptions as ra_exc

from gcl_sdk.agents.universal.drivers import pool
from gcl_sdk.agents.universal.drivers import rawstor_specs
from gcl_sdk.agents.universal.drivers import storage_capacity

INVALID_VALUES = [
    ("mirrors", 0),
    ("mirrors", 256),
    ("mirrors", True),
    ("mirrors", "2"),
    ("failure_domain", "unknown"),
    ("tier", "unknown"),
    ("ephemeral", 1),
    ("ephemeral", "false"),
    ("name", None),
]


@pytest.mark.parametrize("field,value", INVALID_VALUES)
def test_agent_policy_rejects_invalid_json_types_before_coercion(field, value):
    policies = storage_capacity.default_policies(uuid.uuid4())
    key = next(iter(policies))
    policies[key][field] = value
    with pytest.raises(ValueError, match="Invalid RawStor pool policy"):
        pool.RawstorStorageClusterAgentSpec.restore_from_simple_view(
            endpoint="mds://core:7776/",
            pools=policies,
        )
    with pytest.raises(ra_exc.TypeError):
        pool.RawstorStorageClusterAgentSpec(endpoint="mds://core:7776/", pools=policies)


@pytest.mark.parametrize("field", list(rawstor_specs.RawstorPoolPolicy.__scheme__))
def test_agent_policy_requires_every_policy_field(field):
    policies = storage_capacity.default_policies(uuid.uuid4())
    del policies[next(iter(policies))][field]
    with pytest.raises(ValueError):
        pool.RawstorStorageClusterAgentSpec.restore_from_simple_view(
            endpoint="mds://core:7776/",
            pools=policies,
        )


def test_agent_policy_roundtrip_and_schema_expose_nested_fields():
    policies = storage_capacity.default_policies(uuid.uuid4())
    spec = pool.RawstorStorageClusterAgentSpec.restore_from_simple_view(
        endpoint="mds://core:7776/",
        pools=policies,
    )
    assert spec.dump_to_simple_view()["pools"] == policies
    nested = (
        spec.properties["pools"]
        .get_property_type()
        .to_openapi_spec({})["additionalProperties"]["properties"]
    )
    assert nested["mirrors"]["minimum"] == 1
    assert nested["mirrors"]["maximum"] == 255
    assert "chunk_size" not in nested
    assert set(nested["failure_domain"]["enum"]) == {
        d.value for d in rawstor_specs.FailureDomain
    }


def test_policy_schema_lists_required_fields():
    spec = rawstor_specs.RawstorPoolPolicy().to_openapi_spec({})
    assert set(spec["required"]) == set(rawstor_specs.RawstorPoolPolicy.__scheme__)


def test_node_weight_is_not_a_public_property():
    node = rawstor_specs.RawstorNodeDriverSpec(
        endpoint="ost://host:7777", failure_domain_path="host"
    )
    assert "weight" not in node.dump_to_simple_view()
    assert "weight" not in node.properties
