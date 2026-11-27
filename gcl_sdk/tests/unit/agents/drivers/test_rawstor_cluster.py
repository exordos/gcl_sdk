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

import os
import socket
import subprocess
import time
import types
from unittest.mock import call
from unittest.mock import patch
from urllib.parse import urlparse
import uuid as sys_uuid

import pytest

# rawstor_cluster imports the `rawstor` python bindings at module level.
# They ship as an optional extra (not installed in every dev/CI
# environment), so skip this module instead of failing collection - see
# test_exordos_hyper.py for the same pattern.
pytest.importorskip("rawstor")

import rawstor  # noqa: E402

rawstor.initialize(insecure=True)

from gcl_sdk.agents.universal.dm import models as ua_models  # noqa: E402
from gcl_sdk.agents.universal.drivers import pool as pool_base  # noqa: E402
from gcl_sdk.agents.universal.drivers import rawstor_cluster  # noqa: E402
from gcl_sdk.infra import constants as ic  # noqa: E402

CORE_NODE_UUID = sys_uuid.UUID("e56b440c-b5fd-4537-b0ab-c9a311bfa851")


@pytest.fixture(autouse=True)
def core_node_identity(monkeypatch):
    monkeypatch.setattr(rawstor_cluster.ua_utils, "node_uuid", lambda: CORE_NODE_UUID)


def _cluster(tmp_path, tier=ic.DiskTier.HOT.value, ephemeral=False):
    identifier = sys_uuid.uuid4()
    spec = rawstor_cluster.RawstorStorageClusterDriverSpec(
        endpoint="mds://10.20.0.2:7776/",
        nodes={
            str(sys_uuid.uuid5(identifier, "ost")): {
                "endpoint": "ost://10.0.0.5:7777",
                "failure_domain_path": "server",
            }
        },
        pools={
            str(sys_uuid.uuid5(identifier, "default")): {
                "name": "default",
                "tier": tier,
                "ephemeral": ephemeral,
                "mirrors": 1,
                "failure_domain": "server",
            }
        },
    )
    return rawstor_cluster.MetaStorageCluster(uuid=identifier, driver_spec=spec)


class _FakeLocation:
    def __init__(self, used_gb, total_gb):
        self._used_gb = used_gb
        self._total_gb = total_gb

    def info(self):
        return types.SimpleNamespace(
            used=self._used_gb << 30, total=self._total_gb << 30
        )

    def objects(self):
        return iter(())

    def locations(self):
        return iter(("ost://10.0.0.5:7777",))


def _fake_location_info(monkeypatch, *, used_gb, total_gb):
    monkeypatch.setattr(
        rawstor_cluster.rawstor,
        "Location",
        lambda uri: _FakeLocation(used_gb, total_gb),
    )


class TestRawstorStorageClusterDriver:
    def test_rejects_a_mismatched_driver_spec_kind(self, tmp_path):
        class _OtherSpec(rawstor_cluster.AbstractStorageClusterDriverSpec):
            KIND = "other"

        cluster = types.SimpleNamespace(driver_spec=_OtherSpec())
        with pytest.raises(ValueError):
            rawstor_cluster.RawstorStorageClusterDriver(cluster)

    def test_capacity_usable_comes_from_location_info(self, tmp_path, monkeypatch):
        cluster = _cluster(tmp_path)
        _fake_location_info(monkeypatch, used_gb=20, total_gb=100)

        [storage_pool] = rawstor_cluster.RawstorStorageClusterDriver(
            cluster
        ).get_capacity()

        assert storage_pool.name == "default"
        assert storage_pool.pool_type == "rawstor"
        assert storage_pool.capacity_usable == 80
        assert storage_pool.available_actual == 80

    def test_tier_and_ephemeral_come_from_the_driver_spec(self, tmp_path, monkeypatch):
        cluster = _cluster(tmp_path, tier=ic.DiskTier.COLD.value, ephemeral=True)
        _fake_location_info(monkeypatch, used_gb=0, total_gb=100)

        [storage_pool] = rawstor_cluster.RawstorStorageClusterDriver(
            cluster
        ).get_capacity()

        assert storage_pool.tier == ic.DiskTier.COLD.value
        assert storage_pool.ephemeral is True

    def test_uuid_is_deterministic_per_cluster(self, tmp_path, monkeypatch):
        cluster = _cluster(tmp_path)
        _fake_location_info(monkeypatch, used_gb=0, total_gb=100)
        driver = rawstor_cluster.RawstorStorageClusterDriver(cluster)

        [first] = driver.get_capacity()
        [second] = driver.get_capacity()

        assert first.uuid == second.uuid == sys_uuid.uuid5(cluster.uuid, "default")


class _FakeStorageClusterDriver(rawstor_cluster.AbstractStorageClusterDriver):
    def __init__(self, cluster, pools):
        super().__init__(cluster)
        self._pools = pools

    def get_capacity(self):
        return self._pools


class TestMetaStorageCluster:
    def test_get_meta_model_fields_only_keeps_uuid_and_driver_spec(self, tmp_path):
        cluster = _cluster(tmp_path)
        assert cluster.get_meta_model_fields() == {"uuid", "driver_spec"}

    def test_restore_from_dp_refreshes_storage_pools_and_status(
        self, tmp_path, monkeypatch
    ):
        cluster = _cluster(tmp_path)
        pool = pool_base.ThinStoragePool(
            name="default", pool_type="rawstor", capacity_usable=50
        )
        monkeypatch.setattr(
            cluster, "load_driver", lambda: _FakeStorageClusterDriver(cluster, [pool])
        )

        cluster.restore_from_dp()

        assert cluster.storage_pools == [pool]
        assert cluster.status == pool_base.MachinePoolStatus.ACTIVE.value

    def test_dump_to_dp_also_reports_capacity(self, tmp_path, monkeypatch):
        cluster = _cluster(tmp_path)
        pool = pool_base.ThinStoragePool(
            name="default", pool_type="rawstor", capacity_usable=50
        )
        monkeypatch.setattr(
            cluster, "load_driver", lambda: _FakeStorageClusterDriver(cluster, [pool])
        )

        cluster.dump_to_dp()

        assert cluster.storage_pools == [pool]


class TestStorageClusterAgentDriver:
    def test_create_and_list_round_trip(self, tmp_path, monkeypatch):
        pool = pool_base.ThinStoragePool(
            name="default", pool_type="rawstor", capacity_usable=50
        )
        monkeypatch.setattr(
            rawstor_cluster.MetaStorageCluster,
            "load_driver",
            lambda self: _FakeStorageClusterDriver(self, [pool]),
        )

        meta_file = str(tmp_path / "meta.json")
        driver = rawstor_cluster.StorageClusterAgentDriver(meta_file=meta_file)
        assert driver.get_capabilities() == ["storage_cluster", "storage_volume"]

        driver.start()
        cluster_uuid = sys_uuid.uuid4()
        resource = ua_models.Resource(
            uuid=cluster_uuid,
            kind="storage_cluster",
            value={
                "uuid": str(cluster_uuid),
                "driver_spec": {
                    "kind": "rawstor",
                    "endpoint": "mds://10.20.0.2:7776/",
                },
            },
        )
        created = driver.create(resource)
        driver.finalize()

        assert created.value["status"] == pool_base.MachinePoolStatus.ACTIVE.value
        assert created.value["storage_pools"][0]["name"] == "default"

        driver2 = rawstor_cluster.StorageClusterAgentDriver(meta_file=meta_file)
        driver2.start()
        [listed] = driver2.list("storage_cluster")
        assert listed.value["uuid"] == str(cluster_uuid)


class TestCoreMDSLifecycle:
    def _driver(self, tmp_path, monkeypatch, port=7776):
        monkeypatch.setattr(rawstor_cluster, "MDS_CONFIG_DIR", tmp_path / "config")
        monkeypatch.setattr(rawstor_cluster, "MDS_STATE_DIR", tmp_path / "state")
        cluster = _cluster(tmp_path)
        cluster.driver_spec.endpoint = f"mds://10.20.0.2:{port}/"
        return rawstor_cluster.RawstorStorageClusterDriver(cluster)

    def test_configure_sets_database_uri_and_native_witness_registration(
        self, tmp_path, monkeypatch
    ):
        driver = self._driver(tmp_path, monkeypatch)
        with patch.object(rawstor_cluster.subprocess, "run"):
            driver.configure()
        config = tmp_path / "config" / f"{driver._cluster.uuid}.conf"
        values = dict(line.split("=", 1) for line in config.read_text().splitlines())
        assert (
            values["DB_URI"]
            == f"sqlite://{tmp_path}/state/{driver._cluster.uuid}/mds.db"
        )
        assert values["ADVERTISE"] == "ost://10.20.0.2:7776"
        assert values["DOMAIN_PATH"] == str(CORE_NODE_UUID)
        assert values["WEIGHT"] == "100"
        assert not list((tmp_path / "config").glob("*.topology"))

    def test_node_inventory_change_does_not_restart_or_reload_mds(
        self, tmp_path, monkeypatch
    ):
        driver = self._driver(tmp_path, monkeypatch)
        with patch.object(rawstor_cluster.subprocess, "run") as run:
            driver.configure()
            run.reset_mock()
            driver._spec.nodes.clear()
            driver.configure()
        run.assert_called_once_with(
            ["systemctl", "enable", "--now", driver.unit_name], check=True
        )

    def test_failed_restart_restores_configuration_and_retries(
        self, tmp_path, monkeypatch
    ):
        driver = self._driver(tmp_path, monkeypatch)
        with patch.object(rawstor_cluster.subprocess, "run"):
            driver.configure()
        config = tmp_path / "config" / f"{driver._cluster.uuid}.conf"
        previous = config.read_text()
        driver._spec.endpoint = "mds://10.20.0.2:7788/"

        def fail(args, **kwargs):
            if "restart" in args:
                raise subprocess.CalledProcessError(1, args)

        with patch.object(rawstor_cluster.subprocess, "run", side_effect=fail):
            with pytest.raises(subprocess.CalledProcessError):
                driver.configure()
        assert config.read_text() == previous
        with patch.object(rawstor_cluster.subprocess, "run") as retry:
            driver.configure()
        assert any("restart" in c.args[0] for c in retry.call_args_list)

    def test_restore_reads_settings_and_never_writes_or_reloads(
        self, tmp_path, monkeypatch
    ):
        driver = self._driver(tmp_path, monkeypatch)
        with patch.object(rawstor_cluster.subprocess, "run"):
            driver.configure()
        config = tmp_path / "config" / f"{driver._cluster.uuid}.conf"
        config.write_text(config.read_text().replace("10.20.0.2", "127.0.0.1"))
        with (
            patch.object(
                rawstor_cluster.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0),
            ) as run,
            patch.object(rawstor_cluster, "_write_if_changed") as write,
        ):
            driver.restore()
        assert driver._spec.endpoint == "mds://127.0.0.1:7776/"
        write.assert_not_called()
        run.assert_called_once_with(
            ["systemctl", "is-active", "--quiet", driver.unit_name], check=False
        )

    def test_missing_witness_configuration_is_reconciled(self, tmp_path, monkeypatch):
        driver = self._driver(tmp_path, monkeypatch)
        with patch.object(rawstor_cluster.subprocess, "run"):
            driver.configure()
        config = tmp_path / "config" / f"{driver._cluster.uuid}.conf"
        config.write_text(
            config.read_text().replace("ADVERTISE=ost://10.20.0.2:7776", "ADVERTISE=")
        )
        with pytest.raises(rawstor_cluster.driver_exc.ResourceNotFound):
            driver.restore()

    def test_delete_keeps_database_and_stops_only_its_service(
        self, tmp_path, monkeypatch
    ):
        driver = self._driver(tmp_path, monkeypatch)
        db = tmp_path / "state" / str(driver._cluster.uuid) / "mds.db"
        db.parent.mkdir(parents=True)
        db.write_text("persistent")
        with patch.object(rawstor_cluster.subprocess, "run") as run:
            driver.configure()
            driver.delete()
        assert db.read_text() == "persistent"
        assert not (tmp_path / "config" / f"{driver._cluster.uuid}.conf").exists()
        assert (
            call(["systemctl", "disable", "--now", driver.unit_name], check=True)
            in run.call_args_list
        )


def test_unregistered_ost_never_contributes_to_admission(tmp_path, monkeypatch):
    cluster = _cluster(tmp_path)
    location = _FakeLocation(0, 100)
    location.locations = lambda: iter(())
    monkeypatch.setattr(rawstor_cluster.rawstor, "Location", lambda _: location)
    [pool] = rawstor_cluster.RawstorStorageClusterDriver(cluster).get_capacity()
    assert pool.available_actual == 0


def test_deleted_policies_are_not_recreated(tmp_path, monkeypatch):
    cluster = _cluster(tmp_path)
    cluster.driver_spec.pools.clear()
    _fake_location_info(monkeypatch, used_gb=0, total_gb=100)
    assert rawstor_cluster.RawstorStorageClusterDriver(cluster).get_capacity() == []


def test_cached_driver_reports_capacity_on_current_model(tmp_path, monkeypatch):
    original = _cluster(tmp_path)
    driver = original.load_driver()
    current = rawstor_cluster.MetaStorageCluster(
        uuid=original.uuid, driver_spec=original.driver_spec
    )
    assert current.load_driver() is driver
    assert driver._cluster is current


@pytest.mark.skipif(
    not os.environ.get("RAWSTOR_TEST_MDS") or not os.environ.get("RAWSTOR_TEST_OST"),
    reason="Requires native RawStor 0.3.7 daemons",
)
def test_native_registration_accounts_and_mds_restart(tmp_path, monkeypatch):
    processes, streams = [], []
    monkeypatch.setattr(rawstor_cluster, "MDS_CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(rawstor_cluster, "MDS_STATE_DIR", tmp_path / "state")
    env = dict(
        os.environ,
        RAWSTOR_OPTS_IO_ATTEMPTS="1",
        RAWSTOR_MDS_OPTS_HEARTBEAT_INTERVAL="100",
        RAWSTOR_MDS_OPTS_INFO_INTERVAL="100",
        RAWSTOR_MDS_OPTS_INFO_REFRESH_DELAY="20",
    )

    def port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def start(binary, args):
        stream = (tmp_path / (str(len(processes)) + ".log")).open("w")
        streams.append(stream)
        proc = subprocess.Popen(
            [binary, "--insecure", "--workers", "1", *args],
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
        processes.append(proc)
        return proc

    def wait_for(callback):
        deadline = time.monotonic() + 20
        while True:
            try:
                value = callback()
                if value:
                    return value
            except OSError:
                pass
            assert time.monotonic() < deadline, "RawStor did not become ready"
            assert all(p.poll() is None for p in processes)
            time.sleep(0.05)

    try:
        clusters = []
        for index in range(2):
            cluster = _cluster(tmp_path)
            cluster.driver_spec.endpoint = f"mds://127.0.0.1:{port()}/"
            cluster.driver_spec.nodes.clear()
            driver = rawstor_cluster.RawstorStorageClusterDriver(cluster)
            with patch.object(rawstor_cluster.subprocess, "run"):
                driver.configure()
            db = tmp_path / "state" / str(cluster.uuid) / "mds.db"
            db.parent.mkdir(parents=True)
            args = [
                "--bind",
                urlparse(cluster.driver_spec.endpoint).netloc,
                "--db",
                f"sqlite://{db}",
                "--id",
                str(sys_uuid.uuid5(cluster.uuid, "mds-witness")),
                "--advertise",
                cluster.driver_spec.endpoint.replace("mds://", "ost://").rstrip("/"),
                "--domain-path",
                str(CORE_NODE_UUID),
            ]
            mds = start(os.environ["RAWSTOR_TEST_MDS"], args)
            location = rawstor.Location(cluster.driver_spec.endpoint)
            wait_for(lambda: location.info())
            node_id = str(sys_uuid.uuid4())
            ost_endpoint = f"ost://127.0.0.1:{port()}"
            backing = tmp_path / f"data{index}"
            backing.mkdir()
            ost = start(
                os.environ["RAWSTOR_TEST_OST"],
                [
                    "--bind",
                    urlparse(ost_endpoint).netloc,
                    "--id",
                    node_id,
                    "--mds",
                    cluster.driver_spec.endpoint,
                    "--advertise",
                    ost_endpoint,
                    "--domain-path",
                    f"rack/server{index}",
                    f"file://{backing}",
                ],
            )
            cluster.driver_spec.nodes = {
                node_id: {
                    "endpoint": ost_endpoint,
                    "failure_domain_path": f"rack/server{index}",
                }
            }
            wait_for(
                lambda: (
                    ost_endpoint in set(location.locations())
                    and location.info().total > 0
                )
            )
            account = str(sys_uuid.uuid4())
            location.create_account(account)
            target = rawstor.Target(
                f"mds://{account}@127.0.0.1:{urlparse(cluster.driver_spec.endpoint).port}/{sys_uuid.uuid4()}"
            )
            target.create(
                size=1 << 30, width=1, chunk_size=1 << 30, failure_domain="server"
            )
            assert target.spec().size == 1 << 30
            assert target.account == account
            assert target.uri.rsplit("/", 1)[1] in [
                t.uri.rsplit("/", 1)[1] for t in location.objects()
            ]
            driver.get_capacity()
            assert cluster.capacity_info["total"] == location.info().total
            clusters.append((cluster, mds, args, ost, target))
        cluster, mds, args, ost, target = clusters[0]
        mds.terminate()
        mds.wait(timeout=10)
        processes.remove(mds)
        start(os.environ["RAWSTOR_TEST_MDS"], args)
        location = rawstor.Location(cluster.driver_spec.endpoint)
        wait_for(lambda: location.info())
        assert target.spec().size == 1 << 30
        assert clusters[1][-1].spec().size == 1 << 30
        target.remove()
        assert clusters[1][-1].spec().size == 1 << 30
        clusters[1][-1].remove()
    finally:
        for p in reversed(processes):
            p.terminate()
            p.wait(timeout=10)
        for stream in streams:
            stream.close()
