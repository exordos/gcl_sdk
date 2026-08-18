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
"""Target fields follow the declared value into nested dicts and lists.

The data plane fills in defaults the control plane never declared. Dropping
the top-level ones is what target fields have always done; the ones nested
inside a declared dict or list used to stay in the hash, so the target and
the actual never matched and the resource never settled.

The two values here are a load balancer backend pool and a route, taken
from resources that sat in IN_PROGRESS forever because of it.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock
import uuid as sys_uuid

from gcl_sdk.agents.universal import utils
from gcl_sdk.agents.universal.dm import models
from gcl_sdk.agents.universal.drivers.direct import DirectAgentDriver
from gcl_sdk.agents.universal.storage import base as storage_base
from gcl_sdk.agents.universal.storage import common
from gcl_sdk.agents.universal.storage import fs

POOL_UUID = sys_uuid.UUID("6fe9290c-6725-47a7-bbe2-79d5bac2ab39")
LB_UUID = "22e4f11a-896e-4d99-93e6-322f34366d24"
PROJECT_ID = "12345678-c625-4fee-81d5-f691897b8142"

# What the manifest declared.
POOL_TARGET = {
    "uuid": str(POOL_UUID),
    "parent": LB_UUID,
    "endpoints": [{"host": "10.20.0.23", "kind": "host", "port": 8080}],
    "project_id": PROJECT_ID,
}

# What the user API returned: extra top-level fields, and `weight` nested
# inside the declared endpoint.
POOL_ACTUAL = {
    "name": "",
    "uuid": str(POOL_UUID),
    "parent": LB_UUID,
    "status": "ACTIVE",
    "balance": "roundrobin",
    "endpoints": [{"host": "10.20.0.23", "kind": "host", "port": 8080, "weight": 1}],
    "created_at": "2026-08-18 05:56:56.605031",
    "project_id": PROJECT_ID,
    "updated_at": "2026-08-18 05:56:56.605033",
    "description": "",
}

ROUTE_UUID = sys_uuid.UUID("857b1839-3da5-4d4e-9fae-9b8c77b6c918")
VHOST_UUID = "5bbba898-00b2-48db-a3cd-5251518658cc"

ROUTE_TARGET = {
    "uuid": str(ROUTE_UUID),
    "parent": VHOST_UUID,
    "condition": {
        "kind": "prefix",
        "value": "/",
        "actions": [{"kind": "backend", "pool": str(POOL_UUID)}],
    },
    "project_id": PROJECT_ID,
}

# Three defaults nested in `condition`, one of them two levels down.
ROUTE_ACTUAL = {
    "name": "",
    "uuid": str(ROUTE_UUID),
    "parent": VHOST_UUID,
    "status": "ACTIVE",
    "enabled": True,
    "condition": {
        "kind": "prefix",
        "value": "/",
        "actions": [
            {
                "kind": "backend",
                "pool": str(POOL_UUID),
                "protocol": {"kind": "http"},
            }
        ],
        "modifiers": [
            {
                "kind": "auto_header",
                "headers": ["Host", "X-Forwarded-For", "X-Forwarded-Proto"],
            }
        ],
        "allowed_ips": ["0.0.0.0/0"],
    },
    "created_at": "2026-08-18 05:56:59.497202",
    "project_id": PROJECT_ID,
    "updated_at": "2026-08-18 05:56:59.497205",
    "description": "",
}


class TestValueShape:
    def test_keeps_keys_and_drops_every_leaf(self):
        shape = utils.value_shape(POOL_TARGET)

        assert shape == {
            "uuid": None,
            "parent": None,
            "endpoints": [{"host": None, "kind": None, "port": None}],
            "project_id": None,
        }

    def test_top_level_of_the_shape_is_the_target_fields(self):
        shape = utils.value_shape(ROUTE_TARGET)

        assert frozenset(shape) == frozenset(ROUTE_TARGET.keys())

    def test_carries_no_values(self):
        """The shape is persisted, and resource values hold secrets."""
        shape = utils.value_shape(
            {"uuid": str(POOL_UUID), "body": {"password": "hunter2"}}
        )

        assert "hunter2" not in json.dumps(shape)
        assert shape == {"uuid": None, "body": {"password": None}}


class TestProjectOnto:
    def test_drops_a_default_nested_in_a_declared_list(self):
        projected = utils.project_onto(POOL_ACTUAL, utils.value_shape(POOL_TARGET))

        assert projected == POOL_TARGET

    def test_drops_defaults_nested_in_a_declared_dict(self):
        projected = utils.project_onto(ROUTE_ACTUAL, utils.value_shape(ROUTE_TARGET))

        assert projected == ROUTE_TARGET

    def test_a_changed_nested_value_still_shows(self):
        """Only undeclared keys are dropped, never a declared one's value."""
        drifted = json.loads(json.dumps(POOL_ACTUAL))
        drifted["endpoints"][0]["port"] = 9090

        projected = utils.project_onto(drifted, utils.value_shape(POOL_TARGET))

        assert projected != POOL_TARGET
        assert projected["endpoints"][0]["port"] == 9090

    def test_an_extra_list_element_is_kept(self):
        """A data plane that grew the list has drifted, and must read so."""
        drifted = json.loads(json.dumps(POOL_ACTUAL))
        drifted["endpoints"].append(
            {"host": "10.20.0.24", "kind": "host", "port": 8080, "weight": 1}
        )

        projected = utils.project_onto(drifted, utils.value_shape(POOL_TARGET))

        assert len(projected["endpoints"]) == 2
        assert projected != POOL_TARGET

    def test_a_declared_key_the_data_plane_dropped_is_absent(self):
        stripped = {k: v for k, v in POOL_ACTUAL.items() if k != "parent"}

        projected = utils.project_onto(stripped, utils.value_shape(POOL_TARGET))

        assert "parent" not in projected
        assert projected != POOL_TARGET


class TestResourceHash:
    def test_shape_makes_the_hashes_match(self):
        target = models.Resource.from_value(
            POOL_TARGET, "em_core_network_lb_backend_pools"
        )
        target.calculate_hash()

        actual = target.replace_value(POOL_ACTUAL, shape=utils.value_shape(POOL_TARGET))

        assert actual.hash == target.hash

    def test_without_a_shape_the_old_top_level_filtering_stands(self):
        target_fields = frozenset(POOL_TARGET.keys())

        actual = models.Resource.from_value(
            POOL_ACTUAL, "em_core_network_lb_backend_pools", target_fields
        )

        assert actual.hash == utils.calculate_hash(
            {k: POOL_ACTUAL[k] for k in target_fields}
        )

    def test_full_hash_still_covers_everything(self):
        actual = models.Resource.from_value(
            POOL_ACTUAL,
            "em_core_network_lb_backend_pools",
            shape=utils.value_shape(POOL_TARGET),
        )

        assert actual.full_hash == utils.calculate_hash(POOL_ACTUAL)


class TestTargetFieldsFileStorage:
    def _storage(self, tmp_path, name: str) -> fs.TargetFieldsFileStorage:
        path = str(tmp_path / f"{name}.json")
        common.JsonFileStorageSingleton._instances.pop(path, None)
        return fs.TargetFieldsFileStorage(path)

    def test_shape_survives_persist_and_load(self, tmp_path):
        storage = self._storage(tmp_path, "round_trip")
        shape = utils.value_shape(POOL_TARGET)
        storage.create(
            storage_base.TargetFieldItem(
                "em_core_network_lb_backend_pools",
                POOL_UUID,
                frozenset(POOL_TARGET.keys()),
                shape,
            )
        )
        storage.persist()

        reloaded = self._storage(tmp_path, "round_trip")
        item = reloaded.get("em_core_network_lb_backend_pools", POOL_UUID)

        assert item.shape == shape
        assert item.fields == frozenset(POOL_TARGET.keys())

    def test_a_file_written_before_shapes_reads_as_shapeless(self, tmp_path):
        path = tmp_path / "legacy.json"
        path.write_text(
            json.dumps(
                {
                    "em_core_network_lb_backend_pools": {
                        str(POOL_UUID): sorted(POOL_TARGET.keys())
                    }
                }
            )
        )
        common.JsonFileStorageSingleton._instances.pop(str(path), None)

        item = fs.TargetFieldsFileStorage(str(path)).get(
            "em_core_network_lb_backend_pools", POOL_UUID
        )

        assert item.shape is None
        assert item.fields == frozenset(POOL_TARGET.keys())

    def test_an_older_agent_reads_a_newer_file_as_field_names(self, tmp_path):
        """Both formats iterate to the same names, so a downgrade holds."""
        storage = self._storage(tmp_path, "forward")
        storage.create(
            storage_base.TargetFieldItem(
                "em_core_network_lb_vhosts_routes",
                ROUTE_UUID,
                frozenset(ROUTE_TARGET.keys()),
                utils.value_shape(ROUTE_TARGET),
            )
        )
        storage.persist()

        stored = json.loads((tmp_path / "forward.json").read_text())
        raw = stored["em_core_network_lb_vhosts_routes"][str(ROUTE_UUID)]

        # What the pre-shape code does with whatever the file holds.
        assert frozenset(raw) == frozenset(ROUTE_TARGET.keys())

    def test_list_carries_the_shape(self, tmp_path):
        storage = self._storage(tmp_path, "listing")
        shape = utils.value_shape(ROUTE_TARGET)
        storage.create(
            storage_base.TargetFieldItem(
                "em_core_network_lb_vhosts_routes",
                ROUTE_UUID,
                frozenset(ROUTE_TARGET.keys()),
                shape,
            )
        )

        (item,) = storage.list("em_core_network_lb_vhosts_routes")

        assert item.shape == shape


def _driver(client, storage, capability: str) -> DirectAgentDriver:
    class _Drv(DirectAgentDriver):
        def get_capabilities(self) -> list[str]:
            return [capability]

    return _Drv(client=client, storage=storage)


class TestDirectDriverSettles:
    """The resource reaches the state where the agent stops updating it.

    `services/agent.py` compares target.hash to actual.hash to decide
    whether to update, so a hash that cannot match is not only a resource
    stuck in IN_PROGRESS -- it is a PUT to the user API every iteration,
    forever.
    """

    def test_pool_created_from_a_manifest_without_weight(self, tmp_path):
        client = MagicMock()
        storage = fs.TargetFieldsFileStorage(str(tmp_path / "pool.json"))
        kind = "em_core_network_lb_backend_pools"
        drv = _driver(client, storage, kind)

        target = models.Resource.from_value(POOL_TARGET, kind)
        target.calculate_hash()
        client.create.return_value = POOL_ACTUAL

        created = drv.create(target)

        assert created.hash == target.hash

    def test_route_listed_after_a_restart(self, tmp_path):
        """The steady-state path: the shape comes back off disk."""
        client = MagicMock()
        path = str(tmp_path / "route.json")
        common.JsonFileStorageSingleton._instances.pop(path, None)
        storage = fs.TargetFieldsFileStorage(path)
        kind = "em_core_network_lb_vhosts_routes"
        drv = _driver(client, storage, kind)

        target = models.Resource.from_value(ROUTE_TARGET, kind)
        target.calculate_hash()
        client.create.return_value = ROUTE_ACTUAL
        drv.create(target)
        storage.persist()

        # A fresh agent, reading the shape it wrote in a previous run.
        common.JsonFileStorageSingleton._instances.pop(path, None)
        restarted = _driver(client, fs.TargetFieldsFileStorage(path), kind)
        client.list.return_value = [ROUTE_ACTUAL]

        (listed,) = restarted.list(kind)

        assert listed.hash == target.hash

    def test_a_resource_already_stuck_heals_itself(self, tmp_path):
        """What happens to the resources that are stuck right now.

        Their storage entry predates shapes, so the first list() after the
        upgrade still mismatches -- and that mismatch is what makes the
        agent call update(), which writes the shape. The iteration after
        it settles. Being stuck is what unsticks them.
        """
        kind = "em_core_network_lb_backend_pools"
        path = str(tmp_path / "stuck.json")
        common.JsonFileStorageSingleton._instances.pop(path, None)
        (tmp_path / "stuck.json").write_text(
            json.dumps({kind: {str(POOL_UUID): sorted(POOL_TARGET.keys())}})
        )

        client = MagicMock()
        drv = _driver(client, fs.TargetFieldsFileStorage(path), kind)
        target = models.Resource.from_value(POOL_TARGET, kind)
        target.calculate_hash()
        client.list.return_value = [POOL_ACTUAL]

        # Still mismatching, exactly as before the fix.
        (before,) = drv.list(kind)
        assert before.hash != target.hash

        # Which is why the agent updates it, and the shape gets written.
        client.update.return_value = POOL_ACTUAL
        drv.update(target)

        (after,) = drv.list(kind)
        assert after.hash == target.hash

    def test_a_real_change_still_needs_an_update(self, tmp_path):
        client = MagicMock()
        storage = fs.TargetFieldsFileStorage(str(tmp_path / "drift.json"))
        kind = "em_core_network_lb_backend_pools"
        drv = _driver(client, storage, kind)

        target = models.Resource.from_value(POOL_TARGET, kind)
        target.calculate_hash()
        client.create.return_value = POOL_ACTUAL
        drv.create(target)

        drifted = json.loads(json.dumps(POOL_ACTUAL))
        drifted["endpoints"][0]["host"] = "10.20.0.99"
        client.list.return_value = [drifted]

        (listed,) = drv.list(kind)

        assert listed.hash != target.hash
