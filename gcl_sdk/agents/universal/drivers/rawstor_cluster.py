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
import typing as tp
from urllib.parse import urlparse
import uuid as sys_uuid

import rawstor
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.dm import types_dynamic

from gcl_sdk.agents.universal import constants as c
from gcl_sdk.agents.universal.drivers import meta
from gcl_sdk.agents.universal.drivers import pool as pool_base
from gcl_sdk.common import utils

LOG = logging.getLogger(__name__)

# Re-exported for convenience/backward-compat of existing imports of this
# module - the driver_spec classes themselves live in pool.py (alongside
# the other driver specs) so exordos_core can reference them without
# pulling in the `rawstor` python bindings this module needs.
AbstractStorageClusterDriverSpec = pool_base.AbstractStorageClusterDriverSpec
RawstorStorageClusterDriverSpec = pool_base.RawstorStorageClusterDriverSpec


MDS_CONFIG_DIR = Path("/etc/rawstor-mds")
MDS_UNIT_DIR = Path("/etc/systemd/system")
MDS_UNIT_TEMPLATE = """\
[Unit]
Description=Rawstor MDS for Exordos storage {uuid}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=rawstor
Group=rawstor
StateDirectory=exordos/exordos_core/rawstor-mds/{uuid}
ExecStart=/usr/bin/rawstor-mds --bind=0.0.0.0:{port} --db=/var/lib/exordos/exordos_core/rawstor-mds/{uuid}/mds.db --topology=/etc/rawstor-mds/{uuid}.topology
ExecReload=/bin/kill -HUP $MAINPID
Restart=always
RestartSec=5
RestartPreventExitStatus=66 77
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
"""


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

    Unlike AbstractPoolDriver, this only ever reports capacity - actual
    disk CRUD for a volume scheduled onto this cluster is done by
    whichever hypervisor's pool driver owns the VM (see
    ExordosLocalHyperDriver._rawstor_address), not by this driver.
    """

    def __init__(self, cluster: "MetaStorageCluster") -> None:
        self._cluster = cluster

    @property
    def _spec(self) -> AbstractStorageClusterDriverSpec:
        return self._cluster.driver_spec

    @abc.abstractmethod
    def get_capacity(self) -> tp.Collection[pool_base.AbstractStoragePool]:
        """Report this cluster's storage pool(s) and their capacity."""


class RawstorStorageClusterDriver(AbstractStorageClusterDriver):
    def __init__(self, cluster: "MetaStorageCluster") -> None:
        super().__init__(cluster)
        if not isinstance(self._spec, RawstorStorageClusterDriverSpec):
            raise ValueError(f"Unsupported driver spec kind: {self._spec.KIND!r}")

    @property
    def unit_name(self) -> str:
        return f"rawstor-mds@{self._cluster.uuid}.service"

    def configure(self) -> None:
        if not self._spec.ost_endpoint:
            return
        mds = urlparse(self._spec.endpoint)
        ost = urlparse(self._spec.ost_endpoint)
        if (
            mds.scheme != "mds"
            or not mds.hostname
            or not mds.port
            or mds.path not in ("", "/")
            or mds.query
            or mds.fragment
        ):
            raise ValueError("MDS endpoint must be mds://host:port/")
        if (
            ost.scheme != "ost"
            or not ost.hostname
            or not ost.port
            or ost.path not in ("", "/")
            or ost.query
            or ost.fragment
            or any(c.isspace() for c in self._spec.ost_endpoint)
        ):
            raise ValueError("OST endpoint must be ost://host:port")
        unit_changed = _write_if_changed(
            MDS_UNIT_DIR / self.unit_name,
            MDS_UNIT_TEMPLATE.format(uuid=self._cluster.uuid, port=mds.port),
        )
        topology = MDS_CONFIG_DIR / f"{self._cluster.uuid}.topology"
        previous_topology = topology.read_text() if topology.exists() else None
        topology_changed = _write_if_changed(
            topology,
            f"{self._cluster.uuid} {self._spec.ost_endpoint.rstrip('/')} 1 {self._cluster.uuid}\n",
        )
        if unit_changed:
            subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", "--now", self.unit_name], check=True)
        if unit_changed:
            subprocess.run(["systemctl", "restart", self.unit_name], check=True)
        elif topology_changed and previous_topology is not None:
            try:
                subprocess.run(["systemctl", "reload", self.unit_name], check=True)
            except subprocess.CalledProcessError:
                _write_if_changed(topology, previous_topology)
                raise

    def delete(self) -> None:
        if not self._spec.ost_endpoint:
            return
        subprocess.run(["systemctl", "disable", "--now", self.unit_name], check=True)
        (MDS_UNIT_DIR / self.unit_name).unlink(missing_ok=True)
        (MDS_CONFIG_DIR / f"{self._cluster.uuid}.topology").unlink(missing_ok=True)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        # Keep the SQLite index and OST data when unregistering a cluster.

    def get_capacity(self) -> tp.List[pool_base.ThinStoragePool]:
        # A single named pool per cluster in this version - see
        # RawstorStorageClusterDriverSpec's speed/ephemeral fields.
        location = rawstor.Location(
            self._spec.endpoint if self._spec.ost_endpoint else self._spec.location
        )
        info = location.info()
        storage_pool = pool_base.ThinStoragePool(
            uuid=sys_uuid.uuid5(self._cluster.uuid, "default"),
            name="default",
            pool_type="rawstor",
            capacity_usable=info.total >> 30,  # GB
            available_actual=(info.total - info.used) >> 30,  # GB
            oversubscription_ratio=1.0,
            speed=self._spec.speed,
            ephemeral=self._spec.ephemeral,
        )

        # capacity_provisioned is derived from real objects at this
        # location, the same way ExordosLocalHyperDriver computes it for
        # a local rawstor pool - self-healing every poll cycle rather
        # than trusting any cumulative counter.
        for target in location:
            storage_pool.allocate_capacity(target.spec().size >> 30)

        return [storage_pool]


class MetaStorageCluster(meta.MetaCoordinatorDataPlaneModel):
    """Storage cluster meta model.

    Much smaller than MetaPool: a storage cluster has no machines/volumes
    of its own to reconcile, just capacity to report periodically.
    """

    __driver_map__ = {}

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
            return self.__driver_map__[driver_key]

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
        if isinstance(driver, RawstorStorageClusterDriver):
            driver.configure()
        self.storage_pools = list(driver.get_capacity())
        self.status = pool_base.MachinePoolStatus.ACTIVE.value

    def dump_to_dp(self, **kwargs) -> None:
        """Configure the core MDS and report its capacity."""
        self.restore_from_dp(**kwargs)

    def delete_from_dp(self, **kwargs) -> None:
        driver = self.load_driver()
        if isinstance(driver, RawstorStorageClusterDriver):
            driver.delete()


class StorageClusterAgentDriver(meta.MetaCoordinatorAgentDriver):
    __model_map__ = {"storage_cluster": MetaStorageCluster}

    __coordinator_map__ = {
        "storage_cluster": {},
    }
