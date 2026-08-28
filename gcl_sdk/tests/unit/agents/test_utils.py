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

    def test_path_below_a_leaf_element_selects_nothing(self):
        # A path below a list applies to every element, but "0" is no key
        # of the leaf elements, so nothing is selected anywhere. Indexing
        # list elements through a path is not supported.
        value = {"tags": ["a", "b"]}

        result = utils.extract_target_value(value, ["tags.0"])

        self.assertEqual({"tags": []}, result)

    def test_deeply_nested_paths(self):
        value = {"a": {"b": {"c": 1, "d": 2}, "e": 3}}

        result = utils.extract_target_value(value, ["a.b.c"])

        self.assertEqual({"a": {"b": {"c": 1}}}, result)

    def test_empty_target_fields_returns_empty_dict(self):
        value = {"a": 1, "b": {"c": 2}}

        self.assertEqual({}, utils.extract_target_value(value, []))

    def test_trailing_dot_raises(self):
        with self.assertRaises(ValueError):
            utils.extract_target_value({"setter": {"kind": "k"}}, ["setter."])

    def test_leading_dot_raises(self):
        with self.assertRaises(ValueError):
            utils.extract_target_value({"a": 1}, [".x"])

    def test_consecutive_dots_raise(self):
        with self.assertRaises(ValueError):
            utils.extract_target_value({"a": {"b": {"c": 1}}}, ["a..b"])

    def test_empty_field_name_raises(self):
        with self.assertRaises(ValueError):
            utils.extract_target_value({"a": 1}, [""])


class TestExtractTargetValueThroughLists(unittest.TestCase):
    def test_paths_apply_to_every_element_of_a_list(self):
        value = {
            "setter": {
                "kind": "profile",
                "element": "materialized-default",
                "profiles": [
                    {"profile": "a", "value": 1, "note": "data plane junk"},
                    {"profile": "b", "value": 2, "note": "data plane junk"},
                ],
            }
        }

        result = utils.extract_target_value(
            value,
            ["setter.kind", "setter.profiles.profile", "setter.profiles.value"],
        )

        self.assertEqual(
            {
                "setter": {
                    "kind": "profile",
                    "profiles": [
                        {"profile": "a", "value": 1},
                        {"profile": "b", "value": 2},
                    ],
                }
            },
            result,
        )

    def test_non_dict_list_elements_are_dropped(self):
        value = {"a": [{"b": 1}, "leaf", {"b": 2, "c": 3}, {"nested": [{"b": 4}]}]}

        result = utils.extract_target_value(value, ["a.b", "a.nested.b"])

        self.assertEqual({"a": [{"b": 1}, {"b": 2}, {"nested": [{"b": 4}]}]}, result)

    def test_plain_field_wins_over_paths_below_a_list(self):
        value = {"a": [{"b": 1, "c": 2}]}

        result = utils.extract_target_value(value, ["a", "a.b"])

        self.assertEqual({"a": [{"b": 1, "c": 2}]}, result)

    def test_same_head_paths_merge(self):
        value = {"a": {"b": {"c": 1, "d": 2, "e": 3}}}

        result = utils.extract_target_value(value, ["a.b.c", "a.b.d"])

        self.assertEqual({"a": {"b": {"c": 1, "d": 2}}}, result)

    def test_top_level_only_filtering_is_the_plain_dict_filter(self):
        value = {"a": 1, "b": {"c": 2}, "d": [{"e": 3}]}
        target_fields = frozenset({"a", "b"})

        result = utils.extract_target_value(value, target_fields)

        # Bit-for-bit the legacy {k: value[k] for k in target_fields}, so
        # hashes of resources whose fields are top-level never change.
        self.assertEqual(
            utils.calculate_hash(result),
            utils.calculate_hash({k: value[k] for k in target_fields}),
        )
