#    Copyright 2026 Genesis Corporation.
#    Licensed under the Apache License, Version 2.0 (the "License").

from types import SimpleNamespace
from unittest.mock import Mock
import uuid

import pytest

pytest.importorskip("rawstor")
from gcl_sdk.agents.universal.drivers import rawstor_cluster as storage  # noqa: E402


@pytest.fixture
def object_job():
    job = storage.MetaStorageVolume(
        uuid=uuid.uuid4(),
        cluster=uuid.uuid4(),
        project_id=uuid.uuid4(),
        storage_pool=str(uuid.uuid4()),
        storage_location="mds://core:7776/",
        storage_policy={"mirrors": 2, "failure_domain": "server"},
        size=8,
    )
    state = {"size": None}
    target = Mock()

    def spec():
        if state["size"] is None:
            raise FileNotFoundError()
        return SimpleNamespace(size=state["size"], chunk_size=1 << 30)

    target.spec.side_effect = spec
    target.create.side_effect = lambda **kwargs: state.update(size=kwargs["size"])
    target.resize.side_effect = lambda size: state.update(size=size)
    bindings = Mock()
    bindings.Target.return_value = target
    cluster = Mock()
    cluster.load_driver.return_value.bindings = bindings
    return job, cluster, bindings, target, state


def test_core_creates_account_and_object_idempotently(object_job):
    job, cluster, bindings, target, state = object_job
    job.dump_to_dp(cluster)
    bindings.Location.return_value.create_account.assert_called_once_with(
        str(job.project_id)
    )
    bindings.Target.assert_called_with(f"mds://{job.project_id}@core:7776/{job.uuid}")
    target.create.assert_called_once_with(
        size=8 << 30, width=2, chunk_size=1 << 30, failure_domain="server"
    )
    bindings.Location.return_value.create_account.side_effect = FileExistsError()
    job.dump_to_dp(cluster)
    assert target.create.call_count == 1
    assert job.status == "ACTIVE"


def test_core_resizes_object_and_rejects_shrink(object_job):
    job, cluster, _, target, state = object_job
    state["size"] = 4 << 30
    job.dump_to_dp(cluster)
    target.resize.assert_called_once_with(8 << 30)
    job.size = 2
    with pytest.raises(ValueError, match="shrink"):
        job.update_on_dp(cluster)
    assert state["size"] == 8 << 30


def test_core_deletes_only_object_not_account(object_job):
    job, cluster, bindings, target, _ = object_job
    job.delete_from_dp(cluster)
    target.remove.assert_called_once_with()
    bindings.Location.assert_not_called()
    target.remove.side_effect = FileNotFoundError()
    job.delete_from_dp(cluster)


def test_missing_cluster_never_provisions_object(object_job):
    job, _, bindings, target, _ = object_job
    with pytest.raises(RuntimeError, match="not ready"):
        job.dump_to_dp(None)
    target.create.assert_not_called()
    bindings.Location.assert_not_called()


def test_core_replays_interrupted_grow_even_if_spec_already_has_new_size(object_job):
    job, cluster, _, target, state = object_job
    state["size"] = job.size << 30
    job.update_on_dp(cluster)
    target.resize.assert_called_once_with(job.size << 30)
