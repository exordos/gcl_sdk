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

import logging
import os
import re
import shutil

import pytest

from gcl_sdk.tests.integration.sdn import harness as h

_MISSING = "SDN integration harness needs root + ovs-vsctl/ovs-ofctl/ip/conntrack"

# What the driver is allowed to complain about, and why. Everything else at
# WARNING or above fails the test that provoked it, whatever that test was
# checking -- borrowed from OVN, whose every test fails on an unexpected line
# in a daemon log against an allow-list of six benign messages. The point is
# not tidiness: a warning nobody reads is how a defect lives in the tree for
# months, and the case that first prints it is the one that can name it.
#
# Keep this list short and keep the reason attached. A test that means to
# provoke a warning says so itself with `@pytest.mark.expect_logs(...)`.
ALLOWED_LOGS = (
    # The harness wires guests by hand in several cases, so the driver meets
    # a port the vlan map has no entry for and says so before assigning one.
    re.compile(r"no vlan (assigned|mapping) for port", re.I),
)


# Clients that individual cases need, each of which skips itself when its
# tool is absent. That is right on a workstation and wrong on CI: a runner
# whose image stopped shipping one would retire every DHCP or DNS case here
# and still report green -- the same failure the session guard prevents,
# only quieter, because a skipped case is not a missing one until somebody
# counts them.
PER_TEST_TOOLS = ("dhclient", "dig")


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "expect_logs(*patterns): regexes this test is allowed to log at "
        "WARNING or above, because provoking them is what it checks",
    )
    # Here rather than in a fixture, and this is the whole point: `skipif` is
    # evaluated while collecting, so a case skipped for a missing tool never
    # asks for a fixture and no fixture can object on its behalf. This hook
    # runs before collection, which is the last moment anything can.
    if not os.environ.get("SDN_REQUIRE_ENV"):
        return
    missing = [tool for tool in PER_TEST_TOOLS if not shutil.which(tool)]
    if missing:
        raise pytest.UsageError(
            "SDN_REQUIRE_ENV is set and %s missing: the cases needing them "
            "would skip themselves and this run would pass without covering "
            "DHCP or DNS at all" % ", ".join(missing)
        )


class _Collector(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture(autouse=True)
def no_unexpected_logs(request):
    """Fail the test that made the driver complain.

    Attached to every case in this tier, so the tier's real coverage of
    "nothing went wrong quietly" grows with every test written, without any
    of them having to ask for it.
    """
    allowed = list(ALLOWED_LOGS)
    for marker in request.node.iter_markers("expect_logs"):
        allowed += [re.compile(pattern) for pattern in marker.args]

    collector = _Collector()
    logger = logging.getLogger("gcl_sdk")
    logger.addHandler(collector)
    previous, logger.level = (
        logger.level,
        min(logger.level or logging.WARNING, logging.WARNING),
    )
    try:
        yield
    finally:
        logger.removeHandler(collector)
        logger.level = previous

    unexpected = [
        "%s %s: %s" % (r.levelname, r.name, r.getMessage())
        for r in collector.records
        if not any(p.search(r.getMessage()) for p in allowed)
    ]
    if unexpected:
        raise AssertionError(
            "the driver logged %d unexpected line(s):\n  %s"
            % (len(unexpected), "\n  ".join(unexpected))
        )


@pytest.fixture(scope="session", autouse=True)
def requires_sdn_env():
    if h.env_ready():
        return
    # CI sets SDN_REQUIRE_ENV: there, a missing tool is a broken runner, and
    # skipping would let the whole tier disappear while the build stays green.
    if os.environ.get("SDN_REQUIRE_ENV"):
        pytest.fail(_MISSING, pytrace=False)
    pytest.skip(_MISSING, allow_module_level=True)


# What the NF responder may print. It is a supervisor of per-VNI workers, so
# a namespace disappearing under it while a case tears down is ordinary.
ALLOWED_NF_LOGS = (
    re.compile(r"no such (file or directory|process)", re.I),
    re.compile(r"namespace .* (is gone|disappeared)", re.I),
)


def _nf_complaints(path, allowed):
    """Lines of the responder's log that nobody asked for."""
    try:
        with open(path) as log:
            lines = log.read().splitlines()
    except OSError:
        return []
    return [
        line
        for line in lines
        if re.search(r"\b(ERROR|CRITICAL|Traceback)\b", line)
        and not any(p.search(line) for p in allowed)
    ]


@pytest.fixture
def fabric(tmp_path, request):
    """An isolated br-int fabric with the real NF responder, torn down after."""
    harness = h.SdnHarness(str(tmp_path))
    harness.setup()
    try:
        yield harness
    finally:
        harness.cleanup()

    allowed = list(ALLOWED_NF_LOGS)
    for marker in request.node.iter_markers("expect_logs"):
        allowed += [re.compile(pattern) for pattern in marker.args]
    complaints = _nf_complaints(getattr(harness, "nf_log_path", ""), allowed)
    if complaints:
        raise AssertionError(
            "the NF responder logged %d unexpected line(s):\n  %s"
            % (len(complaints), "\n  ".join(complaints[:10]))
        )
