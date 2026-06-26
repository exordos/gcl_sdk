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

import unittest
import uuid

from oslo_config import cfg

from gcl_sdk.common.oslo.types import ObjectOpt
from gcl_sdk.common.oslo.types import ObjectType
from gcl_sdk.common.oslo.types import UuidOpt
from gcl_sdk.common.oslo.types import UuidType


class TestUuidType(unittest.TestCase):
    def setUp(self):
        self.t = UuidType()

    def test_accepts_none(self):
        self.assertIsNone(self.t(None))

    def test_accepts_uuid_instance(self):
        u = uuid.uuid4()
        self.assertEqual(self.t(u), u)

    def test_parses_valid_string(self):
        u = uuid.uuid4()
        parsed = self.t(str(u))
        self.assertIsInstance(parsed, uuid.UUID)
        self.assertEqual(parsed, u)

    def test_invalid_string_raises(self):
        with self.assertRaises(ValueError):
            self.t("not-a-uuid")

    def test_invalid_type_raises(self):
        with self.assertRaises(ValueError):
            self.t(123)  # type: ignore[arg-type]


class TestUuidOpt(unittest.TestCase):
    def test_cli_parsing_valid_uuid(self):
        conf = cfg.ConfigOpts()
        conf.register_cli_opt(UuidOpt("my_uuid"))
        u = uuid.uuid4()
        conf(["--my_uuid", str(u)])
        self.assertEqual(conf.my_uuid, u)

    def test_default_none_allowed(self):
        conf = cfg.ConfigOpts()
        conf.register_opt(UuidOpt("opt", default=None))
        conf([])
        self.assertIsNone(conf.opt)

    def test_cli_parsing_invalid_uuid_errors(self):
        conf = cfg.ConfigOpts()
        conf.register_cli_opt(UuidOpt("bad"))
        # oslo.config may raise ValueError or SystemExit
        # depending on parsing path
        with self.assertRaises((ValueError, SystemExit)):
            conf(["--bad", "zzz-not-uuid"])


class TestObjectType(unittest.TestCase):
    def setUp(self):
        self.t = ObjectType()

    def test_accepts_none(self):
        self.assertIsNone(self.t(None))

    def test_accepts_class_instance(self):
        self.assertIs(self.t(ObjectType), ObjectType)

    def test_parses_valid_string(self):
        cls = self.t("gcl_sdk.common.oslo.types:ObjectType")
        self.assertIs(cls, ObjectType)

    def test_invalid_format_raises(self):
        with self.assertRaises(ValueError):
            self.t("gcl_sdk.common.oslo.types.ObjectType")  # missing ':'

    def test_module_not_found_raises(self):
        with self.assertRaises(ValueError):
            self.t("no.such.module:ObjectType")

    def test_class_not_found_raises(self):
        with self.assertRaises(ValueError):
            self.t("gcl_sdk.common.oslo.types:NopeClass")


class TestObjectOpt(unittest.TestCase):
    def test_cli_parsing_valid_object(self):
        conf = cfg.ConfigOpts()
        conf.register_cli_opt(ObjectOpt("klass"))
        conf(
            [
                "--klass",
                "gcl_sdk.common.oslo.types:ObjectType",
            ]
        )
        self.assertIs(conf.klass, ObjectType)

    def test_default_none_allowed(self):
        conf = cfg.ConfigOpts()
        conf.register_opt(ObjectOpt("obj", default=None))
        conf([])
        self.assertIsNone(conf.obj)

    def test_cli_parsing_invalid_object_errors(self):
        conf = cfg.ConfigOpts()
        conf.register_cli_opt(ObjectOpt("bad"))
        with self.assertRaises((ValueError, SystemExit)):
            conf(["--bad", "gcl_sdk.common.oslo.types.ObjectType"])  # invalid format
