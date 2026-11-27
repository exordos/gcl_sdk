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
from __future__ import annotations

import abc
import logging
import os
from pathlib import Path
import subprocess
import tempfile
import time
import typing as tp
from urllib.parse import urlparse
import uuid as sys_uuid

from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.dm import types_dynamic

from gcl_sdk.agents.universal import constants as c
from gcl_sdk.agents.universal import utils as ua_utils
from gcl_sdk.agents.universal.drivers import exceptions as driver_exc
from gcl_sdk.agents.universal.drivers import meta
from gcl_sdk.agents.universal.drivers import rawstor_tls
from gcl_sdk.agents.universal.drivers import pool as pool_base
from gcl_sdk.agents.universal.drivers import storage_capacity
from gcl_sdk.common import utils

LOG = logging.getLogger(__name__)


# Re-exported for convenience/backward-compat of existing imports of this
# module - the driver_spec classes themselves live in pool.py (alongside
# the other driver specs) so exordos_core can reference them without
# pulling in the `rawstor` python bindings this module needs.
AbstractStorageClusterDriverSpec = pool_base.AbstractStorageClusterDriverSpec
RawstorStorageClusterDriverSpec = pool_base.RawstorStorageClusterAgentSpec


MDS_CONFIG_DIR = Path("/etc/rawstor/mds")
MDS_STATE_DIR = Path("/var/lib/rawstor/mds")
_TOPOLOGY_WEIGHT = 100


def _write_if_changed(path: Path, content: str) -> bool:
    if path.exists() and path.read_text() == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        temporary = Path(f.name)
        try:
            f.write(content)
            f.flush()
            os.fchmod(f.fileno(), 0o644)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return True


class AbstractStorageClusterDriver(abc.ABC):
    """Base class for storage cluster drivers.

    Owns the MDS configuration and provider access used by storage-volume
    resources. Hypervisors only attach already provisioned objects.
    """

    def __init__(self, cluster: "MetaStorageCluster") -> None:
        self._cluster = cluster

    @property
    def _spec(self) -> AbstractStorageClusterDriverSpec:
        return self._cluster.driver_spec

    def configure(self) -> None:
        """Apply backend configuration when the driver requires it."""

    def restore(self) -> None:
        """Read backend configuration without changing the data plane."""

    def delete(self) -> None:
        """Remove backend configuration when the driver owns it."""

    @abc.abstractmethod
    def get_capacity(self) -> tp.Collection[pool_base.AbstractStoragePool]:
        """Report this cluster's storage pool(s) and their capacity."""


class RawstorStorageClusterDriver(AbstractStorageClusterDriver):
    def __init__(self, cluster: "MetaStorageCluster") -> None:
        super().__init__(cluster)
        if not isinstance(self._spec, RawstorStorageClusterDriverSpec):
            raise ValueError(f"Unsupported driver spec kind: {self._spec.KIND!r}")

    @property
    def bindings(self):
        directory, _ = rawstor_tls.install(
            "control-" + str(self._cluster.uuid), self._spec.tls.get("control")
        )
        return rawstor_tls.Bindings(directory)

    @property
    def unit_name(self) -> str:
        return f"rawstor-mds@{self._cluster.uuid}.service"

    def _service_settings(self):
        directory, _ = rawstor_tls.install(
            "mds-" + str(self._cluster.uuid), self._spec.tls.get("service")
        )
        return 'EXTRA_ARGS=""\n' + "".join(
            f"{key}={value}\n"
            for key, value in rawstor_tls.environment(directory).items()
        )

    def _reload_credentials(self):
        rawstor_tls.reload_if_changed(
            rawstor_tls.TLS_DIR / ("mds-" + str(self._cluster.uuid)), self.unit_name
        )

    def _mds_settings(self, bind):
        storage_capacity.validate_bind_address(bind)
        return {
            "BIND_ADDR": bind,
            "DB_URI": f"sqlite://{MDS_STATE_DIR / str(self._cluster.uuid) / 'mds.db'}",
            "ADVERTISE": f"ost://{bind}",
            "DOMAIN_PATH": str(ua_utils.node_uuid()),
            "WEIGHT": str(_TOPOLOGY_WEIGHT),
        }

    def configure(self) -> None:
        mds = urlparse(self._spec.endpoint)
        if (
            mds.scheme != "mds"
            or mds.path not in ("", "/")
            or mds.username
            or mds.query
            or mds.fragment
        ):
            raise ValueError("MDS endpoint must be mds://host:port/")
        config = MDS_CONFIG_DIR / f"{self._cluster.uuid}.conf"
        previous = config.read_text() if config.exists() else None
        content = "".join(
            f"{key}={value}\n" for key, value in self._mds_settings(mds.netloc).items()
        )
        content += self._service_settings()
        changed = _write_if_changed(config, content)
        subprocess.run(["systemctl", "enable", "--now", self.unit_name], check=True)
        if changed and previous is not None:
            try:
                subprocess.run(["systemctl", "restart", self.unit_name], check=True)
            except subprocess.CalledProcessError:
                _write_if_changed(config, previous)
                raise
        if previous is not None:
            self._reload_credentials()

    def restore(self) -> None:
        config = MDS_CONFIG_DIR / f"{self._cluster.uuid}.conf"
        if not config.exists():
            raise driver_exc.ResourceNotFound(resource=self._cluster)
        values = dict(line.split("=", 1) for line in config.read_text().splitlines())
        try:
            actual_bind = values["BIND_ADDR"]
            expected = self._mds_settings(actual_bind)
        except (KeyError, ValueError) as error:
            raise driver_exc.ResourceNotFound(resource=self._cluster) from error
        if any(values.get(key) != value for key, value in expected.items()):
            raise driver_exc.ResourceNotFound(resource=self._cluster)
        self._spec.endpoint = f"mds://{actual_bind}/"
        active = subprocess.run(
            ["systemctl", "is-active", "--quiet", self.unit_name], check=False
        )
        if active.returncode:
            raise driver_exc.ResourceNotFound(resource=self._cluster)

    def delete(self) -> None:
        subprocess.run(["systemctl", "disable", "--now", self.unit_name], check=True)
        (MDS_CONFIG_DIR / f"{self._cluster.uuid}.conf").unlink(missing_ok=True)
        # The database and OST data outlive the service configuration.

    def get_capacity(self) -> tp.List[pool_base.ThinStoragePool]:
        bindings = self.bindings
        mds = bindings.Location(self._spec.endpoint)
        info = mds.info()
        registered = set(mds.locations())
        # Enumerate completed objects before sampling budgets so a concurrent
        # creation remains reserved in at least one of the two ledgers.
        objects = {
            target.uri.rstrip("/").rsplit("/", 1)[-1]: target.spec().size
            for target in mds.objects()
        }
        nodes = []
        for node_uuid, node in self._spec.nodes.items():
            location = bindings.Location(node["endpoint"])
            if node["endpoint"] not in registered:
                nodes.append(
                    {**node, "uuid": node_uuid, "total": 0, "used": 0, "available": 0}
                )
                continue
            try:
                sample = location.info()
                committed = sum(target.spec().size for target in location.objects())
                nodes.append(
                    {
                        **node,
                        "uuid": node_uuid,
                        "total": sample.total,
                        "used": sample.used,
                        "available": max(0, sample.total - max(sample.used, committed)),
                    }
                )
            except OSError:
                # Never schedule new data based on stale capacity from an unreachable OST.
                LOG.exception("Unable to query OST %s", node_uuid)
                nodes.append(
                    {**node, "uuid": node_uuid, "total": 0, "used": 0, "available": 0}
                )
        self._cluster.capacity_info = {
            "nodes": nodes,
            "objects": objects,
            "reported_at": time.time(),
            "total": info.total,
            "used": info.used,
            "available": min(
                max(0, info.total - info.used), sum(n["available"] for n in nodes)
            ),
        }
        result = []
        for pool_uuid, policy in self._spec.pools.items():
            available = storage_capacity.available_by_policy(nodes, policy) >> 30
            result.append(
                pool_base.ThinStoragePool(
                    uuid=sys_uuid.UUID(pool_uuid),
                    name=policy["name"],
                    pool_type="rawstor",
                    tier=policy["tier"],
                    ephemeral=policy["ephemeral"],
                    driver_spec=pool_base.rawstor_specs.RawstorPoolDriverSpec(
                        mirrors=policy["mirrors"],
                        failure_domain=policy["failure_domain"],
                    ),
                    capacity_usable=available,
                    available_actual=available,
                    oversubscription_ratio=1.0,
                )
            )
        return result


class MetaStorageCluster(meta.MetaDataPlaneModel):
    """Storage cluster meta model.

    Much smaller than MetaPool: a storage cluster has no machines/volumes
    of its own to reconcile, just capacity to report periodically.
    """

    __driver_map__ = {}

    capacity_info = properties.property(types.Dict(), default=dict)

    driver_spec = properties.property(
        types_dynamic.KindModelSelectorType(
            types_dynamic.KindModelType(RawstorStorageClusterDriverSpec),
        ),
        required=True,
    )
    status = properties.property(
        types.Enum([s.value for s in pool_base.MachinePoolStatus]),
        default=pool_base.MachinePoolStatus.ACTIVE.value,
    )
    storage_pools = properties.property(
        types.TypedList(
            types_dynamic.KindModelSelectorType(
                types_dynamic.KindModelType(pool_base.ThinStoragePool),
            ),
        ),
        default=list,
    )

    def load_driver(self) -> AbstractStorageClusterDriver:
        """Load the driver for the storage cluster.

        The driver is restored from the cache if it is already loaded.
        """
        driver_key = (self.uuid, str(self.driver_spec))

        if driver_key in self.__driver_map__:
            driver = self.__driver_map__[driver_key]
            driver._cluster = self
            return driver

        driver_kind = self.driver_spec.KIND
        class_ = utils.load_from_entry_point(c.EP_STORAGE_CLUSTER_DRIVERS, driver_kind)
        driver = class_(self)
        self.__driver_map__[driver_key] = driver
        return driver

    def get_meta_model_fields(self) -> tp.Optional[tp.Set[str]]:
        """Return a list of meta fields or None.

        Meta fields are the fields that cannot be fetched from
        the data plane or we just want to save them into the meta file.
        """
        return {"uuid", "driver_spec"}

    def restore_from_dp(self, **kwargs) -> None:
        """Refresh this cluster's reported capacity."""
        driver = self.load_driver()
        driver.restore()
        try:
            self.storage_pools = list(driver.get_capacity())
        except OSError:
            LOG.exception("Unable to query storage cluster %s", self.uuid)
            self.storage_pools = []
            self.capacity_info = {}
            self.status = pool_base.MachinePoolStatus.DISABLED.value
        else:
            self.status = pool_base.MachinePoolStatus.ACTIVE.value

    def dump_to_dp(self, **kwargs) -> None:
        """Configure the core MDS and report its capacity."""
        driver = self.load_driver()
        driver.configure()
        self.storage_pools = list(driver.get_capacity())
        self.status = pool_base.MachinePoolStatus.ACTIVE.value

    def update_on_dp(self, **kwargs) -> None:
        self.dump_to_dp(**kwargs)

    def delete_from_dp(self, **kwargs) -> None:
        driver = self.load_driver()
        driver.delete()


class MetaStorageVolume(meta.MetaDataPlaneModel):
    """An object managed by the Core agent, independently of VM attachment."""

    cluster = properties.property(types.UUID(), required=True)
    project_id = properties.property(types.UUID(), required=True)
    storage_pool = properties.property(types.String(), required=True)
    storage_location = properties.property(types.String(), required=True)
    storage_policy = properties.property(types.Dict(), required=True)
    size = properties.property(types.Integer(min_value=1), required=True)
    status = properties.property(types.String(), default="NEW")

    def _target(self, cluster):
        if cluster is None:
            raise RuntimeError("Storage cluster is not ready")
        from urllib.parse import urlsplit, urlunsplit

        parsed = urlsplit(self.storage_location)
        address = urlunsplit(
            (
                parsed.scheme,
                f"{self.project_id}@{parsed.netloc}",
                f"/{self.uuid}",
                "",
                "",
            )
        )
        return cluster.load_driver().bindings.Target(address)

    def dump_to_dp(self, cluster):
        target = self._target(cluster)
        bindings = cluster.load_driver().bindings
        try:
            bindings.Location(self.storage_location).create_account(
                str(self.project_id)
            )
        except FileExistsError:
            pass
        try:
            spec = target.spec()
        except FileNotFoundError:
            target.create(
                size=self.size << 30,
                width=self.storage_policy["mirrors"],
                chunk_size=pool_base.rawstor_specs.RAWSTOR_CHUNK_SIZE,
                failure_domain=self.storage_policy["failure_domain"],
            )
            spec = target.spec()
        else:
            if spec.size > self.size << 30:
                raise ValueError("RawStor objects cannot shrink")
            # A previous grow may have reserved chunks before materializing
            # them. Replaying the same size completes that interrupted grow.
            target.resize(self.size << 30)
        self.restore_from_dp(cluster)

    def restore_from_dp(self, cluster):
        try:
            spec = self._target(cluster).spec()
        except FileNotFoundError as error:
            raise driver_exc.ResourceNotFound(resource=self) from error
        self.size = spec.size >> 30
        self.status = "ACTIVE"

    def update_on_dp(self, cluster):
        self.dump_to_dp(cluster)

    def delete_from_dp(self, cluster):
        try:
            self._target(cluster).remove()
        except FileNotFoundError:
            pass


class StorageClusterAgentDriver(meta.MetaCoordinatorAgentDriver):
    __model_map__ = {
        "storage_cluster": MetaStorageCluster,
        "storage_volume": MetaStorageVolume,
    }
    __coordinator_map__ = {
        "storage_cluster": {},
        "storage_volume": {
            "cluster": {"kind": "storage_cluster", "relation": "storage_volume:cluster"}
        },
    }
