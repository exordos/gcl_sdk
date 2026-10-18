# Copyright 2026 Genesis Corporation. Licensed under the Apache License, Version 2.0.
"""A fail-static test needs a new trusted sync, not historical log entries."""

from types import SimpleNamespace
from unittest import mock

import pytest

from gcl_sdk.tests.integration.sdn import harness
from gcl_sdk.tests.integration.sdn_fabric.two_host import TwoHostFabric


def _fabric(tmp_path):
    fabric = object.__new__(TwoHostFabric)
    fabric._workdir = str(tmp_path)
    fabric.hosts = [SimpleNamespace(name=name) for name in ("h0", "h1")]
    fabric._forwarding_failure = lambda *args: "diagnostic"
    logs = [tmp_path / ("connector-%s.log" % host.name) for host in fabric.hosts]
    for log in logs:
        log.write_text("old metric_id=fail_static_active, value=0\n")
    return fabric, logs


def test_trusted_sync_ignores_history_and_requires_both_new_cycles(tmp_path):
    fabric, logs = _fabric(tmp_path)
    now = 0

    def advance(_):
        nonlocal now
        now += 1
        if now == 1:
            logs[0].write_text(
                logs[0].read_text() + "new metric_id=fail_static_active, value=1\n"
            )
        if now == 2:
            logs[0].write_text(
                logs[0].read_text() + "new metric_id=fail_static_active, value=0\n"
            )
        if now == 3:
            logs[1].write_text(
                logs[1].read_text() + "new metric_id=fail_static_active, value=0\n"
            )

    with (
        mock.patch.object(harness.time, "time", side_effect=lambda: now),
        mock.patch.object(harness.time, "sleep", side_effect=advance),
    ):
        assert fabric.wait_fail_static_ready({"ns": "guest"}, "target", timeout=4)
    assert now == 3


def test_historical_trust_alone_times_out(tmp_path):
    fabric, _ = _fabric(tmp_path)
    now = 0

    def advance(_):
        nonlocal now
        now += 1

    with (
        mock.patch.object(harness.time, "time", side_effect=lambda: now),
        mock.patch.object(harness.time, "sleep", side_effect=advance),
        pytest.raises(
            AssertionError, match="No new trusted connector sync.*"
        ) as failure,
    ):
        fabric.wait_fail_static_ready({"ns": "guest"}, "target", timeout=3)
    assert now == 3
    assert "diagnostic" in str(failure.value)
