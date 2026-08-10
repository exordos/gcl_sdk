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

from gcl_sdk.agents.universal import utils


class TestExtractTargetValue(unittest.TestCase):
    def test_plain_top_level_fields(self):
        value = {"a": 1, "b": 2, "c": 3}

        result = utils.extract_target_value(value, ["a", "c"])

        self.assertEqual({"a": 1, "c": 3}, result)

    def test_missing_plain_field_raises_in_strict_mode(self):
        value = {"a": 1}

        with self.assertRaises(KeyError):
            utils.extract_target_value(value, ["a", "missing"])

    def test_missing_plain_field_is_skipped_when_not_strict(self):
        value = {"a": 1}

        result = utils.extract_target_value(value, ["a", "missing"], strict=False)

        self.assertEqual({"a": 1}, result)

    def test_nested_dotted_path_selects_only_that_subfield(self):
        value = {"setter": {"kind": "profile", "element": None, "profiles": [1, 2]}}

        result = utils.extract_target_value(value, ["setter.kind", "setter.profiles"])

        self.assertEqual({"setter": {"kind": "profile", "profiles": [1, 2]}}, result)
        self.assertNotIn("element", result["setter"])

    def test_default_nested_field_does_not_affect_result(self):
        # Same declared target fields, differing only by a field that
        # was never selected (e.g. left at its default value).
        declared = {"setter": {"kind": "profile", "profiles": [1]}}
        actual = {"setter": {"kind": "profile", "profiles": [1], "element": "abc"}}
        target_fields = ["setter.kind", "setter.profiles"]

        self.assertEqual(
            utils.extract_target_value(declared, target_fields, strict=False),
            utils.extract_target_value(actual, target_fields, strict=False),
        )

    def test_plain_key_wins_over_dotted_paths_for_same_top_level_field(self):
        value = {"setter": {"kind": "profile", "element": "abc"}}

        result = utils.extract_target_value(value, ["setter", "setter.kind"])

        self.assertEqual({"setter": {"kind": "profile", "element": "abc"}}, result)

    def test_missing_nested_top_level_key_is_skipped(self):
        value = {"a": 1}

        result = utils.extract_target_value(value, ["setter.kind"])

        self.assertEqual({}, result)

    def test_nested_value_that_is_not_a_dict_is_skipped(self):
        value = {"tags": ["a", "b"]}

        result = utils.extract_target_value(value, ["tags.0"])

        self.assertEqual({}, result)

    def test_deeply_nested_paths(self):
        value = {"a": {"b": {"c": 1, "d": 2}, "e": 3}}

        result = utils.extract_target_value(value, ["a.b.c"])

        self.assertEqual({"a": {"b": {"c": 1}}}, result)

    def test_empty_target_fields_returns_empty_dict(self):
        value = {"a": 1, "b": {"c": 2}}

        self.assertEqual({}, utils.extract_target_value(value, []))
