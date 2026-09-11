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
import typing as tp
import uuid as sys_uuid

import rawstor
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.dm import types_dynamic

from gcl_sdk.agents.universal import constants as c
from gcl_sdk.agents.universal.drivers import meta
from gcl_sdk.agents.universal.drivers import pool as pool_base
from gcl_sdk.common import utils
from gcl_sdk.infra import constants as ic

LOG = logging.getLogger(__name__)


class AbstractStorageClusterDriverSpec(
    types_dynamic.AbstractKindModel,
    models.SimpleViewMixin,
):
    """Base class for all storage cluster driver specs."""


class RawstorStorageClusterDriverSpec(AbstractStorageClusterDriverSpec):
    KIND = "rawstor"

    # Backing store this cluster's rawstor-ost serves, e.g.
    # file:///var/lib/rawstor. Informational only - it's configured on
    # the storage node itself (see `exordos storages init --location`),
    # never resent to a driver.
    location = properties.property(types.String(max_length=2048), required=True)
    # Network address (ost://host:port) other hosts use to reach this
    # cluster - what the scheduler pushes into a remote-scheduled
    # volume's `storage_location` (see gcl_sdk.agents.universal.drivers.
    # exordos_hyper.ExordosLocalHyperDriver._rawstor_address).
    endpoint = properties.property(types.String(max_length=2048), required=True)
    speed = properties.property(
        types.Enum([s.value for s in ic.DiskSpeed]),
        default=ic.DiskSpeed.HOT.value,
    )
    ephemeral = properties.property(types.Boolean(), default=False)


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

    def get_capacity(self) -> tp.List[pool_base.ThinStoragePool]:
        # A single named pool per cluster in this version - see
        # RawstorStorageClusterDriverSpec's speed/ephemeral fields.
        location = rawstor.Location(self._spec.location)
        info = location.info()
        storage_pool = pool_base.ThinStoragePool(
            uuid=sys_uuid.uuid5(self._cluster.uuid, "default"),
            name="default",
            pool_type="rawstor",
            capacity_usable=info.total >> 30,  # GB
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
        driver_key = str(self.driver_spec)

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
        self.storage_pools = list(driver.get_capacity())
        self.status = pool_base.MachinePoolStatus.ACTIVE.value

    def dump_to_dp(self, **kwargs) -> None:
        """Configure the cluster.

        There's nothing to configure - the cluster's driver_spec already
        points at an existing, independently-provisioned rawstor-ost -
        but capacity still needs to be reported the first time too.
        """
        self.restore_from_dp(**kwargs)


class StorageClusterAgentDriver(meta.MetaCoordinatorAgentDriver):
    __model_map__ = {"storage_cluster": MetaStorageCluster}

    __coordinator_map__ = {
        "storage_cluster": {},
    }
