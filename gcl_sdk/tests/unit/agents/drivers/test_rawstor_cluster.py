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
import signal
import socket
import subprocess
import time
import types
from unittest.mock import call
from unittest.mock import patch
import uuid as sys_uuid

import pytest

# rawstor_cluster imports the `rawstor` python bindings at module level.
# They ship as an optional extra (not installed in every dev/CI
# environment), so skip this module instead of failing collection - see
# test_exordos_hyper.py for the same pattern.
pytest.importorskip("rawstor")

import rawstor  # noqa: E402

from gcl_sdk.agents.universal.dm import models as ua_models  # noqa: E402
from gcl_sdk.agents.universal.drivers import pool as pool_base  # noqa: E402
from gcl_sdk.agents.universal.drivers import rawstor_cluster  # noqa: E402
from gcl_sdk.infra import constants as ic  # noqa: E402


def _cluster(tmp_path, speed=ic.DiskSpeed.HOT.value, ephemeral=False):
    # rawstor's "file://" location is a real, local, daemon-less backend
    # (see pyrawstor/tests) - no server needed to exercise it for real.
    spec = rawstor_cluster.RawstorStorageClusterDriverSpec(
        location=f"file://{tmp_path}",
        endpoint="ost://10.0.0.5:7777",
        speed=speed,
        ephemeral=ephemeral,
    )
    return rawstor_cluster.MetaStorageCluster(uuid=sys_uuid.uuid4(), driver_spec=spec)


class _FakeLocation:
    def __init__(self, used_gb, total_gb):
        self._used_gb = used_gb
        self._total_gb = total_gb

    def info(self):
        return types.SimpleNamespace(
            used=self._used_gb << 30, total=self._total_gb << 30
        )

    def __iter__(self):
        return iter(())


def _fake_location_info(monkeypatch, *, used_gb, total_gb):
    # __iter__ must live on the class, not an instance attribute (as a
    # plain SimpleNamespace(__iter__=...) used to try) - Python looks up
    # dunder methods on the type, so `for x in obj` never saw it there.
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
        assert storage_pool.capacity_usable == 100
        assert storage_pool.available_actual == 80

    def test_speed_and_ephemeral_come_from_the_driver_spec(self, tmp_path, monkeypatch):
        cluster = _cluster(tmp_path, speed=ic.DiskSpeed.COLD.value, ephemeral=True)
        _fake_location_info(monkeypatch, used_gb=0, total_gb=100)

        [storage_pool] = rawstor_cluster.RawstorStorageClusterDriver(
            cluster
        ).get_capacity()

        assert storage_pool.speed == ic.DiskSpeed.COLD.value
        assert storage_pool.ephemeral is True

    def test_capacity_provisioned_sums_real_objects_at_the_location(self, tmp_path):
        # No mocking here - real rawstor objects at a real (daemon-less)
        # file:// location, to exercise the actual enumeration path.
        cluster = _cluster(tmp_path)
        location = rawstor.Location(f"file://{tmp_path}")
        location.create(size=10 << 30, width=1)
        location.create(size=5 << 30, width=1)

        [storage_pool] = rawstor_cluster.RawstorStorageClusterDriver(
            cluster
        ).get_capacity()

        assert storage_pool.capacity_provisioned == 15

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
        assert driver.get_capabilities() == ["storage_cluster"]

        driver.start()
        cluster_uuid = sys_uuid.uuid4()
        resource = ua_models.Resource(
            uuid=cluster_uuid,
            kind="storage_cluster",
            value={
                "uuid": str(cluster_uuid),
                "driver_spec": {
                    "kind": "rawstor",
                    "location": f"file://{tmp_path}",
                    "endpoint": "ost://10.0.0.5:7777",
                    "speed": ic.DiskSpeed.HOT.value,
                    "ephemeral": False,
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
        cluster = _cluster(tmp_path)
        cluster.driver_spec.endpoint = f"mds://10.20.0.2:{port}/"
        cluster.driver_spec.ost_endpoint = "ost://10.0.0.5:7777"
        return rawstor_cluster.RawstorStorageClusterDriver(cluster)

    def test_configure_creates_isolated_topology_and_instance_config(
        self, tmp_path, monkeypatch
    ):
        driver = self._driver(tmp_path, monkeypatch)
        with patch.object(rawstor_cluster.subprocess, "run") as run:
            driver.configure()
        cluster_uuid = driver._cluster.uuid
        topology = (tmp_path / "config" / f"{cluster_uuid}.topology").read_text()
        assert topology == f"{cluster_uuid} ost://10.0.0.5:7777 1 {cluster_uuid}\n"
        config = (tmp_path / "config" / f"{cluster_uuid}.conf").read_text()
        assert config == "BIND_ADDR=10.20.0.2:7776\n"
        assert (
            call(["systemctl", "enable", "--now", driver.unit_name], check=True)
            in run.call_args_list
        )

    @pytest.mark.parametrize("weight", [1.0, 2.0])
    def test_topology_serializes_integral_legacy_weights_without_decimal_point(
        self, tmp_path, monkeypatch, weight
    ):
        driver = self._driver(tmp_path, monkeypatch)
        node_uuid = str(driver._cluster.uuid)
        driver._cluster.driver_spec.nodes = {
            node_uuid: {
                "endpoint": "ost://10.0.0.5:7777",
                "weight": weight,
                "failure_domain_path": "dc/server",
            }
        }
        with patch.object(rawstor_cluster.subprocess, "run"):
            driver.configure()
        assert (tmp_path / "config" / f"{node_uuid}.topology").read_text() == (
            f"{node_uuid} ost://10.0.0.5:7777 {int(weight)} dc/server\n"
        )

    @pytest.mark.parametrize("weight", [0, 1.5, float("inf"), 1 << 64])
    def test_topology_rejects_invalid_weights(self, tmp_path, monkeypatch, weight):
        driver = self._driver(tmp_path, monkeypatch)
        driver._cluster.driver_spec.nodes = {
            str(driver._cluster.uuid): {
                "endpoint": "ost://10.0.0.5:7777",
                "weight": weight,
                "failure_domain_path": "server",
            }
        }
        with pytest.raises(ValueError):
            driver.configure()

    def test_topology_update_requests_reload_without_inspecting_database_or_logs(
        self, tmp_path, monkeypatch
    ):
        driver = self._driver(tmp_path, monkeypatch)
        with patch.object(rawstor_cluster.subprocess, "run") as run:
            driver.configure()
            run.reset_mock()
            driver._cluster.driver_spec.ost_endpoint = "ost://10.0.0.6:7778"
            driver.configure()
        assert (
            call(["systemctl", "reload", driver.unit_name], check=True)
            in run.call_args_list
        )
        assert not any("restart" in c.args[0] for c in run.call_args_list)

    def test_unchanged_configuration_does_not_restart_mds(self, tmp_path, monkeypatch):
        driver = self._driver(tmp_path, monkeypatch)
        with patch.object(rawstor_cluster.subprocess, "run") as run:
            driver.configure()
            run.reset_mock()
            driver.configure()
        run.assert_called_once_with(
            ["systemctl", "enable", "--now", driver.unit_name], check=True
        )

    def test_capacity_is_read_from_mds_not_local_ost_backing_path(
        self, tmp_path, monkeypatch
    ):
        driver = self._driver(tmp_path, monkeypatch)
        with patch.object(
            rawstor_cluster.rawstor, "Location", return_value=_FakeLocation(20, 100)
        ) as location:
            [pool] = driver.get_capacity()
        location.assert_called_once_with("mds://10.20.0.2:7776/")
        assert pool.available_actual == 80

    def test_two_clusters_have_distinct_units_and_ports(self, tmp_path, monkeypatch):
        first = self._driver(tmp_path, monkeypatch, 7776)
        second = self._driver(tmp_path, monkeypatch, 7778)
        with patch.object(rawstor_cluster.subprocess, "run"):
            first.configure()
            second.configure()
        assert first.unit_name != second.unit_name
        assert (
            "BIND_ADDR=10.20.0.2:7778"
            in (tmp_path / "config" / f"{second._cluster.uuid}.conf").read_text()
        )

    @pytest.mark.parametrize("address", ["10.20.0.2", "2001:db8::2"])
    def test_hostname_endpoint_is_resolved_for_bind(
        self, tmp_path, monkeypatch, address
    ):
        driver = self._driver(tmp_path, monkeypatch)
        driver._cluster.driver_spec.endpoint = "mds://core.example:7776/"
        with (
            patch.object(rawstor_cluster.subprocess, "run"),
            patch.object(
                rawstor_cluster.socket,
                "getaddrinfo",
                return_value=[(None, None, None, "", (address, 7776))],
            ) as resolve,
        ):
            driver.configure()
        resolve.assert_called_once_with(
            "core.example", 7776, type=rawstor_cluster.socket.SOCK_STREAM
        )
        host = f"[{address}]" if ":" in address else address
        config = tmp_path / "config" / f"{driver._cluster.uuid}.conf"
        assert config.read_text() == f"BIND_ADDR={host}:7776\n"
        assert driver._cluster.driver_spec.endpoint == "mds://core.example:7776/"

    def test_delete_stops_only_this_clusters_mds(self, tmp_path, monkeypatch):
        driver = self._driver(tmp_path, monkeypatch)
        with patch.object(rawstor_cluster.subprocess, "run") as run:
            driver.configure()
            run.reset_mock()
            driver.delete()
        assert (
            call(["systemctl", "disable", "--now", driver.unit_name], check=True)
            in run.call_args_list
        )
        assert not (tmp_path / "config" / f"{driver._cluster.uuid}.topology").exists()


@pytest.mark.skipif(
    not os.environ.get("RAWSTOR_TEST_MDS") or not os.environ.get("RAWSTOR_TEST_OST"),
    reason="Set RAWSTOR_TEST_MDS and RAWSTOR_TEST_OST to run real daemon integration",
)
def test_two_core_mds_instances_create_enumerate_and_recover_disks(
    tmp_path, monkeypatch
):
    processes = []
    streams = []
    clusters = []
    env = dict(
        os.environ, RAWSTOR_MDS_OPTS_INFO_INTERVAL="50", RAWSTOR_OPTS_IO_ATTEMPTS="1"
    )
    monkeypatch.setattr(rawstor_cluster, "MDS_CONFIG_DIR", tmp_path / "config")

    def port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def start(binary, args, name):
        stream = (tmp_path / name).open("a")
        streams.append(stream)
        process = subprocess.Popen(
            [binary, *args], env=env, stdout=stream, stderr=subprocess.STDOUT
        )
        processes.append(process)
        return process

    def ready(cluster):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if rawstor.Location(cluster.driver_spec.endpoint).info().total:
                    return
            except OSError:
                pass
            assert all(process.poll() is None for process in processes)
            time.sleep(0.05)
        pytest.fail("MDS did not report OST capacity within 10 seconds")

    monkeypatch.setattr(rawstor_cluster, "MDS_STATE_DIR", tmp_path / "state")

    def start_mds(cluster):
        db = rawstor_cluster.MDS_STATE_DIR / str(cluster.uuid) / "mds.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        endpoint_port = cluster.driver_spec.endpoint.split(":")[-1].rstrip("/")
        return start(
            os.environ["RAWSTOR_TEST_MDS"],
            [
                "--bind",
                f"127.0.0.1:{endpoint_port}",
                "--db",
                str(db),
                "--topology",
                str(tmp_path / "config" / f"{cluster.uuid}.topology"),
                "--workers",
                "1",
                "--queue-size",
                "256",
            ],
            f"{cluster.uuid}.log",
        )

    try:
        mds_processes = []
        for index in range(2):
            cluster = _cluster(tmp_path)
            cluster.driver_spec.endpoint = f"mds://127.0.0.1:{port()}/"
            cluster.driver_spec.pools = (
                rawstor_cluster.storage_capacity.default_policies(cluster.uuid)
            )
            nodes = {}
            for member in range(2):
                backing = tmp_path / f"ost{index}-{member}"
                backing.mkdir()
                ost_port = port()
                start(
                    os.environ["RAWSTOR_TEST_OST"],
                    [
                        "--bind",
                        f"127.0.0.1:{ost_port}",
                        "--workers",
                        "1",
                        "--queue-size",
                        "256",
                        f"file://{backing}",
                    ],
                    f"ost{index}-{member}.log",
                )
                nodes[str(sys_uuid.uuid4())] = {
                    "endpoint": f"ost://127.0.0.1:{ost_port}",
                    "weight": 1.0,
                    "failure_domain_path": f"dc/row/rack{member}/server{member}",
                }
            cluster.driver_spec.nodes = nodes
            with patch.object(rawstor_cluster.subprocess, "run"):
                rawstor_cluster.RawstorStorageClusterDriver(cluster).configure()
            clusters.append(cluster)
            mds_processes.append(start_mds(cluster))
            ready(cluster)

        targets = []
        for cluster in clusters:
            target = rawstor.Target(f"{cluster.driver_spec.endpoint}{sys_uuid.uuid4()}")
            target.create(
                size=8 << 20, width=2, chunk_size=4 << 20, failure_domain="rack"
            )
            targets.append(target)
            assert target.spec().size == 8 << 20
            assert target.spec().width == 2
            assert target.spec().failure_domain == rawstor.OBJ_DOMAIN_RACK
            capacities = rawstor_cluster.RawstorStorageClusterDriver(
                cluster
            ).get_capacity()
            assert len(capacities) == 2
            assert (
                cluster.capacity_info["objects"][target.uri.rsplit("/", 1)[-1]]
                == 8 << 20
            )
            assert all(
                list(rawstor.Location(n["endpoint"]))
                for n in cluster.driver_spec.nodes.values()
            )
        assert [
            target.uri for target in rawstor.Location(clusters[0].driver_spec.endpoint)
        ] == [targets[0].uri]
        assert [
            target.uri for target in rawstor.Location(clusters[1].driver_spec.endpoint)
        ] == [targets[1].uri]

        # Apply a topology edit through the real daemon's HUP handler.
        first = clusters[0]
        first.driver_spec.nodes[next(iter(first.driver_spec.nodes))]["weight"] = 2

        def reload_service(args, **kwargs):
            if args[1] == "reload":
                mds_processes[0].send_signal(signal.SIGHUP)

        with patch.object(
            rawstor_cluster.subprocess, "run", side_effect=reload_service
        ) as run:
            rawstor_cluster.RawstorStorageClusterDriver(first).configure()
            assert (
                call(
                    ["systemctl", "reload", f"rawstor-mds@{first.uuid}.service"],
                    check=True,
                )
                in run.call_args_list
            )

        # Stop and restart one MDS with its original SQLite database.
        previous = mds_processes[0]
        previous.terminate()
        previous.wait(timeout=10)
        processes.remove(previous)
        start_mds(clusters[0])
        ready(clusters[0])
        assert targets[0].spec().size == 8 << 20
        targets[0].remove()
        assert list(rawstor.Location(clusters[0].driver_spec.endpoint)) == []
        assert targets[1].spec().size == 8 << 20
        targets[1].remove()
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
        for stream in streams:
            stream.close()


def test_managed_cluster_does_not_recreate_deleted_pools(tmp_path, monkeypatch):
    cluster = _cluster(tmp_path)
    cluster.driver_spec.managed = True
    with patch.object(rawstor_cluster.rawstor, "Location") as location:
        assert rawstor_cluster.RawstorStorageClusterDriver(cluster).get_capacity() == []
    location.assert_not_called()
    assert cluster.capacity_info["available"] == 0


def test_cached_driver_reports_capacity_on_current_model(tmp_path, monkeypatch):
    original = _cluster(tmp_path)
    _fake_location_info(monkeypatch, used_gb=0, total_gb=100)
    driver = original.load_driver()
    current = rawstor_cluster.MetaStorageCluster(
        uuid=original.uuid,
        driver_spec=original.driver_spec,
    )
    assert current.load_driver() is driver
    assert driver._cluster is current
