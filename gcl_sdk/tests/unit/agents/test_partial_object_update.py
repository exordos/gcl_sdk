#    Copyright 2026 Genesis Corporation.
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at http://www.apache.org/licenses/LICENSE-2.0

import pytest

from gcl_sdk.agents.universal.clients.backend.db import merge_declared_object


@pytest.mark.parametrize(
    "current, patch, expected",
    [
        (
            {"a": 1, "nested": {"x": 2, "y": 3}},
            {"nested": {"x": 4}},
            {"a": 1, "nested": {"x": 4, "y": 3}},
        ),
        ({"a": 1}, {}, {"a": 1}),
        ({"a": [1, 2]}, {"a": []}, {"a": []}),
        ({"a": 1}, {"a": None}, {"a": None}),
        ({"kind": "old", "a": 1}, {"kind": "new"}, {"kind": "new"}),
        ([{"x": 1, "y": 2}], [{"x": 3}], [{"x": 3}]),
    ],
)
def test_partial_dictionary_update_preserves_only_omitted_keys(
    current, patch, expected
):
    assert merge_declared_object(current, patch) == expected


def test_legacy_backend_without_create_only_metadata_remains_compatible():
    from types import SimpleNamespace
    import uuid

    from gcl_sdk.agents.universal import utils
    from gcl_sdk.agents.universal.dm import models
    from gcl_sdk.agents.universal.drivers.direct import DirectAgentDriver

    resource = models.Resource(
        kind="certificate", uuid=uuid.uuid4(), value={"name": "cert"}
    )
    driver = SimpleNamespace(_client=object())
    assert DirectAgentDriver._resolve_target_fields(
        driver, resource
    ) == utils.value_shape(resource.value)
