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

import uuid as sys_uuid

import pytest

# The pool driver reaches the libvirt python bindings through the libvirt
# driver module, which imports them at module level.
pytest.importorskip("libvirt")

from gcl_sdk.agents.universal.drivers import pool as ua_pool  # noqa: E402


def _meta_pool() -> ua_pool.MetaPool:
    # libvirt's built-in "test" driver simulates a hypervisor in-memory, so
    # the pool reports a real (non-zero) capacity without a daemon.
    return ua_pool.MetaPool.restore_from_simple_view(
        uuid=str(sys_uuid.uuid4()),
        machine_type="VM",
        cores_ratio=1.0,
        ram_ratio=1.0,
        driver_spec={
            "kind": "libvirt",
            "connection_uri": "test:///default",
            "storage_pool": "default-pool",
            "network": "default",
            # The simulated hypervisor's own domain is not one of ours.
            "machine_prefix": "vm-",
        },
    )


def test_update_reports_the_pool_it_found_not_an_empty_one():
    """An update must read the pool back, not report the bare target.

    Otherwise it reports zero capacity and every guest on it as missing.
    """
    pool = _meta_pool()
    pool.update_on_dp()

    assert pool.all_cores > 0
    assert pool.all_ram > 0
    # The maps machines and volumes resolve themselves through.
    assert pool.dp_storage_pool_map


def test_update_agrees_with_a_plain_restore():
    """`update` must leave the pool as `list` would have left it.

    They feed the same field to the control plane, so if they disagree the
    resource never converges and the pool is updated on every iteration.
    """
    restored, updated = _meta_pool(), _meta_pool()
    restored.restore_from_dp()
    updated.update_on_dp()

    assert (updated.all_cores, updated.all_ram) == (
        restored.all_cores,
        restored.all_ram,
    )
