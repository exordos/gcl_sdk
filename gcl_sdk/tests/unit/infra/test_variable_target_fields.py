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

import unittest
import uuid

from gcl_sdk.infra.dm import models as infra_models


class TestVariableTargetFieldsHash(unittest.TestCase):
    """Regression tests for gcl_sdk issue #103.

    A `Variable.setter` is a nested, kind-based object with fields that
    may be left at their default value (e.g. `element`) instead of being
    declared explicitly. The target-field hash must stay stable
    regardless of the value of such undeclared nested fields.
    """

    def setUp(self):
        self.uuid = str(uuid.uuid4())
        self.project_id = str(uuid.uuid4())
        self.profile_uuid = str(uuid.uuid4())

    def _build_variable(self, element=None):
        setter = {
            "kind": "profile",
            "fallback_strategy": "ignore",
            "profiles": [{"profile": self.profile_uuid, "value": 1}],
        }
        if element is not None:
            setter["element"] = element

        return infra_models.Variable.restore_from_simple_view(
            uuid=self.uuid,
            name="default_replicas",
            project_id=self.project_id,
            setter=setter,
        )

    def test_hash_excludes_setter_element_when_not_set(self):
        var = self._build_variable()

        _, target_data = var.get_ua_all_and_target_values()

        self.assertNotIn("element", target_data["setter"])

    def test_hash_includes_setter_element_when_set(self):
        var = self._build_variable(element=str(uuid.uuid4()))

        _, target_data = var.get_ua_all_and_target_values()

        self.assertIn("element", target_data["setter"])

    def test_hash_is_stable_when_backend_fills_default_element(self):
        """The target has element=None, the backend fills it in.

        Target fields come from the target, so the actual is hashed
        with the same fields (without ``setter.element``) and the
        hashes match.
        """
        declared = self._build_variable()
        target_fields = declared.get_resource_target_fields()

        target = declared.to_ua_resource()
        # The backend echoes the value and fills in element.
        actual_value = declared.dump_to_simple_view()
        actual_value["setter"]["element"] = str(uuid.uuid4())
        actual = target.replace_value(actual_value, target_fields)

        self.assertEqual(target.hash, actual.hash)

    def test_hash_changes_when_a_real_target_field_changes(self):
        declared = self._build_variable()
        changed = infra_models.Variable.restore_from_simple_view(
            uuid=self.uuid,
            name="default_replicas",
            project_id=self.project_id,
            setter={
                "kind": "profile",
                "fallback_strategy": "ignore",
                "profiles": [{"profile": self.profile_uuid, "value": 2}],
            },
        )

        self.assertNotEqual(
            declared.to_ua_resource().hash,
            changed.to_ua_resource().hash,
        )

    def test_selector_setter_has_only_its_own_fields(self):
        var = infra_models.Variable.restore_from_simple_view(
            uuid=self.uuid,
            name="var_selector",
            project_id=self.project_id,
            setter={"kind": "selector", "selector_strategy": "latest"},
        )

        fields = var.get_resource_target_fields()

        self.assertIn("setter.selector_strategy", fields)
        self.assertNotIn("setter.fallback_strategy", fields)
        self.assertNotIn("setter.profiles", fields)
        self.assertNotIn("setter.element", fields)
