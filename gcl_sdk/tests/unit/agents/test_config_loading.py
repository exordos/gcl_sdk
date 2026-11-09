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

from types import SimpleNamespace

import pytest

from gcl_sdk.agents.universal import utils
from gcl_sdk.agents.universal.cmd import universal_agent


class LiteralDriver:
    def __init__(self, **params):
        self.params = params


LITERAL_VALUES = (
    "Example%value",
    "Example%%value",
    "%(debug)s/value",
    "ordinary-value",
    "%",
    "%%",
    "%value",
    "value%",
)


@pytest.mark.parametrize("value", LITERAL_VALUES)
def test_load_driver_preserves_literal_values(tmp_path, monkeypatch, value):
    config_file = tmp_path / "agent.conf"
    config_file.write_text(
        f"[DEFAULT]\ndebug = True\n\n[LiteralDriver]\npassword = {value}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        universal_agent.cfg,
        "CONF",
        SimpleNamespace(config_file=[str(config_file)]),
    )

    driver = universal_agent.load_driver(LiteralDriver)

    assert driver.params == {"password": value}


@pytest.mark.parametrize("value", LITERAL_VALUES)
def test_cfg_load_section_map_preserves_literal_values(tmp_path, value):
    config_file = tmp_path / "agent.conf"
    config_file.write_text(
        f"[DEFAULT]\ndebug = True\n\n[section]\npassword = {value}\n",
        encoding="utf-8",
    )

    params = utils.cfg_load_section_map(str(config_file), "section")

    assert params == {"password": value}
