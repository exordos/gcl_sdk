#    Copyright 2025 Genesis Corporation.
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
import enum
import logging
import os
import random
import typing as tp
import uuid as sys_uuid

import netaddr
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.dm import types_dynamic
from restalchemy.dm import types_network as types_net

from gcl_sdk.agents.universal import constants as c
from gcl_sdk.agents.universal.dm import models as ua_models
from gcl_sdk.agents.universal.drivers import exceptions as ua_driver_exc
from gcl_sdk.agents.universal.drivers import meta
from gcl_sdk.common import exceptions
from gcl_sdk.common import types as common_types
from gcl_sdk.common import utils

LOG = logging.getLogger(__name__)

DRY_RUN_ENV = "EXO_AGENTS_DRY_RUN"

# Placeholder project for data plane entities that don't belong to
# a particular tenant (ports/volumes/machines reconstructed from the
# hypervisor rather than loaded from a project-scoped store).
SYSTEM_PROJECT_ID = sys_uuid.UUID("00000000-0000-0000-0000-000000000000")

BootType = tp.Literal["hd", "network", "cdrom"]


class NodeType(str, enum.Enum):
    VM = "VM"
    HW = "HW"


class MachineStatus(str, enum.Enum):
    NEW = "NEW"
    SCHEDULED = "SCHEDULED"
    IN_PROGRESS = "IN_PROGRESS"
    STARTED = "STARTED"
    ACTIVE = "ACTIVE"
    IDLE = "IDLE"
    ERROR = "ERROR"
    FLASHED = "FLASHED"
    NEED_RESCHEDULE = "NEED_RESCHEDULE"


class VolumeStatus(str, enum.Enum):
    NEW = "NEW"
    IN_PROGRESS = "IN_PROGRESS"
    ACTIVE = "ACTIVE"
    ERROR = "ERROR"


class DiskSpeed(str, enum.Enum):
    COLD = "cold"
    WARM = "warm"
    HOT = "hot"


class PortStatus(str, enum.Enum):
    NEW = "NEW"
    IN_PROGRESS = "IN_PROGRESS"
    ACTIVE = "ACTIVE"
    ERROR = "ERROR"


class MachinePoolStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    MAINTENANCE = "MAINTENANCE"
    IN_PROGRESS = "IN_PROGRESS"


class BootAlternative(str, enum.Enum):
    hd0 = "hd0"
    hd1 = "hd1"
    hd2 = "hd2"
    hd3 = "hd3"
    hd4 = "hd4"
    hd5 = "hd5"
    hd6 = "hd6"
    hd7 = "hd7"
    cdrom = "cdrom"
    network = "network"

    @property
    def hd_prefix(self) -> str:
        return "hd"

    @property
    def boot_from_hd(self) -> bool:
        return self.value.startswith(self.hd_prefix)

    @property
    def boot_type(self) -> BootType:
        if self.boot_from_hd:
            return self.hd_prefix
        elif self.value == "cdrom":
            return "cdrom"
        elif self.value == "network":
            return "network"

        raise ValueError(f"Invalid boot alternative: {self.value}")


class MachineAlreadyExistsError(exceptions.UniversalAgentException):
    __template__ = "The machine {machine} already exists."
    machine: sys_uuid.UUID


class MachineNotFoundError(exceptions.UniversalAgentException):
    __template__ = "The machine {machine} not found."
    machine: sys_uuid.UUID


class VolumeAlreadyExistsError(exceptions.UniversalAgentException):
    __template__ = "The volume {volume} already exists."
    volume: sys_uuid.UUID


class VolumeNotFoundError(exceptions.UniversalAgentException):
    __template__ = "The volume {volume} not found."
    volume: sys_uuid.UUID


class VolumeAlreadyAttachedError(exceptions.UniversalAgentException):
    __template__ = "The volume {volume} is already attached to machine {machine}."
    volume: sys_uuid.UUID
    machine: sys_uuid.UUID


class VolumeNotAttachedError(exceptions.UniversalAgentException):
    __template__ = "The volume {volume} is not attached to machine {machine}."
    volume: sys_uuid.UUID
    machine: sys_uuid.UUID


class PortAlreadyAttachedError(exceptions.UniversalAgentException):
    __template__ = "The port {port} is already attached to machine {machine}."
    port: sys_uuid.UUID
    machine: sys_uuid.UUID


class PortNotAttachedError(exceptions.UniversalAgentException):
    __template__ = "The port {port} is not attached to machine {machine}."
    port: sys_uuid.UUID
    machine: sys_uuid.UUID


class RootVolumeNotFound(ua_driver_exc.AgentDriverException):
    __template__ = "Root volume not found for machine {machine}."
    machine: sys_uuid.UUID


class Port(models.ModelWithUUID, models.ModelWithProject, models.SimpleViewMixin):
    """Data plane representation of a machine's network port."""

    subnet = properties.property(types.AllowNone(types.UUID()), default=None)
    machine = properties.property(types.AllowNone(types.UUID()), default=None)
    ipv4 = properties.property(types.AllowNone(types_net.IPAddress()), default=None)
    mask = properties.property(types.AllowNone(types_net.IPAddress()), default=None)
    mac = properties.property(types.AllowNone(types.Mac()), default=None)
    status = properties.property(
        types.Enum([s.value for s in PortStatus]),
        default=PortStatus.NEW.value,
    )
    source = properties.property(
        types.AllowNone(types.String(max_length=128)),
        default=None,
    )

    @staticmethod
    def generate_mac(virtual_machine: bool = True) -> str:
        octets = tuple(random.randint(0, 255) for _ in range(5))

        if virtual_machine:
            return "52:54:00:%02x:%02x:%02x" % octets[2:]

        return "a9:%02x:%02x:%02x:%02x:%02x" % octets


class Machine(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithNameDesc,
    models.SimpleViewMixin,
):
    """Data plane representation of a pool machine (VM)."""

    cores = properties.property(
        types.Integer(min_value=0, max_value=4096), required=True
    )
    ram = properties.property(types.Integer(min_value=0), required=True)
    status = properties.property(
        types.Enum([s.value for s in MachineStatus]),
        default=MachineStatus.NEW.value,
    )
    machine_type = properties.property(
        types.Enum([t.value for t in NodeType]),
        default=NodeType.VM.value,
    )
    node = properties.property(types.AllowNone(types.UUID()), default=None)
    pool = properties.property(types.AllowNone(types.UUID()), default=None)
    boot = properties.property(
        types.Enum([b.value for b in BootAlternative]),
        default=BootAlternative.network.value,
    )
    image = properties.property(
        types.AllowNone(types.String(max_length=255)), default=None
    )


class MachineVolume(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithNameDesc,
    models.SimpleViewMixin,
):
    """Data plane representation of a machine volume (disk)."""

    pool = properties.property(types.AllowNone(types.UUID()), default=None)
    machine = properties.property(types.AllowNone(types.UUID()), default=None)
    size = properties.property(types.Integer(min_value=1, max_value=1000000))
    image = properties.property(
        types.AllowNone(types.String(max_length=255)), default=None
    )
    boot = properties.property(types.Boolean(), default=True)
    label = properties.property(
        types.AllowNone(types.String(max_length=127)), default=None
    )
    device_type = properties.property(types.String(max_length=64), default="")
    speed = properties.property(
        types.Enum([s.value for s in DiskSpeed]),
        default=DiskSpeed.WARM.value,
    )
    ephemeral = properties.property(types.Boolean(), default=False)
    # Name of the storage pool (see AbstractPoolDriverSpec.storage_pool) the
    # volume was scheduled onto. Set by MetaVolume when it's first created;
    # left as-is afterwards (see get_meta_model_fields).
    storage_pool = properties.property(
        types.AllowNone(types.String(max_length=255)), default=None
    )
    status = properties.property(
        types.Enum([s.value for s in VolumeStatus]),
        default=VolumeStatus.NEW.value,
    )
    index = properties.property(
        types.Integer(min_value=0, max_value=4096), default=4096
    )


class AbstractStoragePool(
    models.SimpleViewMixin,
    types_dynamic.AbstractKindModel,
):
    """The abstract model for storage pool.

    This model is used to represent the storage pool and determine
    the its interfaces.
    """

    uuid = properties.property(
        types.UUID(),
        read_only=True,
        id_property=True,
        default=lambda: sys_uuid.uuid4(),
    )
    pool_type = properties.property(types.String(), required=True)
    speed = properties.property(
        types.Enum([s.value for s in DiskSpeed]),
        default=DiskSpeed.WARM.value,
    )
    ephemeral = properties.property(types.Boolean(), default=False)

    @property
    def capacity(self) -> int:
        """Storage pool capacity."""
        return 0

    @property
    def available(self) -> int:
        """Storage pool available space."""
        return 0

    def allocate_capacity(self, size: int) -> None:
        """Allocate capacity."""
        raise NotImplementedError()

    def free_capacity(self, size: int) -> None:
        """Free capacity."""
        raise NotImplementedError()

    def has_capacity(self, size: int) -> bool:
        """Check if the storage pool has enough capacity."""
        return self.available >= size


class ThinStoragePool(
    AbstractStoragePool,
    models.ModelWithNameDesc,
):
    """The model represents thin provisioned storage pool."""

    KIND = "thin_storage_pool"

    capacity_usable = properties.property(types.Integer(min_value=0), default=0)
    capacity_provisioned = properties.property(types.Integer(min_value=0), default=0)
    oversubscription_ratio = properties.property(
        types.Float(min_value=0.0), default=1.0
    )
    available_actual = properties.property(types.Integer(min_value=0), default=0)

    @property
    def capacity(self) -> int:
        """Storage pool capacity."""
        return int(self.capacity_usable * self.oversubscription_ratio)

    @property
    def available(self) -> int:
        """Storage pool available space."""
        return self.capacity - self.capacity_provisioned

    def allocate_capacity(self, size: int) -> None:
        """Allocate capacity."""
        self.capacity_provisioned += size

    def free_capacity(self, size: int) -> None:
        """Free capacity."""
        self.capacity_provisioned -= size


class AbstractPoolDriverSpec(
    types_dynamic.AbstractKindModel,
    models.SimpleViewMixin,
):
    """Base class for all pool driver specs."""


class LibvirtPoolDriverSpec(AbstractPoolDriverSpec):
    KIND = "libvirt"

    connection_uri = properties.property(
        types.String(max_length=2048),
        required=True,
    )
    network = properties.property(
        types.AllowNone(types.String(max_length=255)),
        default=None,
    )
    storage_pool = properties.property(
        types.AllowNone(types.String(max_length=255)),
        default=None,
    )
    machine_prefix = properties.property(
        types.AllowNone(types.String(max_length=255)),
        default=None,
    )
    network_type = properties.property(
        types.Enum(["network", "bridge"]),
        default="network",
    )
    iface_rom_file = properties.property(
        types.AllowNone(types.String(max_length=255)),
        default=None,
    )
    iface_mtu = properties.property(
        types.Integer(min_value=0, max_value=65536),
        default=1500,
    )
    iface_source = properties.property(
        types.AllowNone(types.String(max_length=255)),
        default=None,
    )


class StoragePoolEntry(common_types.SchematicType):
    __scheme__ = {
        "name": types.String(max_length=255),
        "speed": types.Enum([s.value for s in DiskSpeed]),
        "ephemeral": types.Boolean(),
    }
    __mandatory__ = {"name"}


class ExordosLocalHyperDriverSpec(LibvirtPoolDriverSpec):
    KIND = "exordos_local_hyper"

    node = properties.property(types.UUID(), required=True)

    # Overrides LibvirtPoolDriverSpec.storage_pool (a single pool name)
    # with a list of named pools, each independently tagged with
    # speed/ephemeral so the scheduler can place a disk on the right one.
    storage_pool = properties.property(
        types.TypedList(StoragePoolEntry()),
        default=list,
    )


class DummyPoolDriverSpec(AbstractPoolDriverSpec):
    KIND = "dummy"


class MachinePool(
    models.ModelWithUUID,
    models.ModelWithNameDesc,
    models.SimpleViewMixin,
):
    """Data plane representation of a machine pool."""

    driver_spec = properties.property(
        types_dynamic.KindModelSelectorType(
            types_dynamic.KindModelType(LibvirtPoolDriverSpec),
            types_dynamic.KindModelType(ExordosLocalHyperDriverSpec),
            types_dynamic.KindModelType(DummyPoolDriverSpec),
        ),
        required=True,
    )
    machine_type = properties.property(
        types.Enum([t.value for t in NodeType]),
        default=NodeType.VM.value,
    )
    status = properties.property(
        types.Enum([s.value for s in MachinePoolStatus]),
        default=MachinePoolStatus.DISABLED.value,
    )
    avail_cores = properties.property(types.Integer(), default=0)
    avail_ram = properties.property(types.Integer(), default=0)
    all_cores = properties.property(types.Integer(), default=0)
    all_ram = properties.property(types.Integer(), default=0)
    cores_ratio = properties.property(types.Float(min_value=0.0), default=1.0)
    ram_ratio = properties.property(types.Float(min_value=0.0), default=1.0)
    storage_pools = properties.property(
        types.TypedList(
            types_dynamic.KindModelSelectorType(
                types_dynamic.KindModelType(ThinStoragePool),
            ),
        ),
        default=list,
    )


class AbstractPoolDriver(abc.ABC):
    def __init__(self, dry_run: bool = False) -> None:
        super().__init__()
        self._dry_run = dry_run

    @abc.abstractmethod
    def get_pool_info(self) -> MachinePool:
        """Get pool info."""

    @abc.abstractmethod
    def list_pool_resources(
        self,
    ) -> tp.Tuple[
        MachinePool,
        tp.Collection[AbstractStoragePool],
        tp.Collection[tp.Tuple[Machine, tp.Tuple[Port, ...]]],
        tp.Collection[MachineVolume],
    ]:
        """List pool resources."""

    @abc.abstractmethod
    def list_machines(
        self,
    ) -> tp.List[tp.Tuple[Machine, tp.Tuple[Port, ...]]]:
        """Return machine list from data plane."""

    @abc.abstractmethod
    def create_machine(
        self,
        machine: Machine,
        volumes: tp.Iterable[MachineVolume],
        ports: tp.Iterable[Port],
    ) -> tp.Tuple[Machine, tp.Tuple[Port, ...]]:
        """Create a new machine."""

    @abc.abstractmethod
    def delete_machine(self, machine: Machine, delete_volumes: bool = True) -> None:
        """Delete the machine from data plane."""

    @abc.abstractmethod
    def get_machine(
        self, machine: sys_uuid.UUID
    ) -> tp.Tuple[Machine, tp.Tuple[Port, ...]]:
        """Get machine from data plane."""

    @abc.abstractmethod
    def create_volume(self, volume: MachineVolume) -> MachineVolume:
        """Create a new volume."""

    @abc.abstractmethod
    def delete_volume(self, volume: MachineVolume) -> None:
        """Delete the volume from data plane."""

    @abc.abstractmethod
    def resize_volume(self, volume: MachineVolume) -> None:
        """Resize the volume."""

    @abc.abstractmethod
    def attach_volume(self, volume: MachineVolume) -> None:
        """Attach the volume."""

    @abc.abstractmethod
    def detach_volume(self, volume: MachineVolume) -> None:
        """Detach the volume."""

    @abc.abstractmethod
    def attach_port(self, machine: Machine, port: Port) -> None:
        """Attach the port."""

    @abc.abstractmethod
    def detach_port(self, machine: Machine, port: Port) -> None:
        """Detach the port."""

    @abc.abstractmethod
    def list_volumes(
        self, machine: tp.Optional[Machine] = None
    ) -> tp.Iterable[MachineVolume]:
        """Return volume list from data plane."""

    @abc.abstractmethod
    def get_volume(self, volume: sys_uuid.UUID) -> MachineVolume:
        """Get the volume by uuid."""

    @abc.abstractmethod
    def set_machine_cores(self, machine: Machine, cores: int) -> None:
        """Set machine cores."""

    @abc.abstractmethod
    def set_machine_ram(self, machine: Machine, ram: int) -> None:
        """Set machine ram."""

    @abc.abstractmethod
    def reset_machine(self, machine: Machine) -> None:
        """Reset the machine."""

    @abc.abstractmethod
    def recreate_machine(
        self,
        machine: Machine,
        ports: tp.Optional[tp.Collection[Port]] = None,
    ) -> None:
        """Recreate the machine."""

    @abc.abstractmethod
    def rename_machine(self, machine: Machine, name: str) -> None:
        """Rename the machine."""

    @abc.abstractmethod
    def shutdown_machine(self, machine: Machine, force: bool = False) -> None:
        """Shutdown the machine."""

    @abc.abstractmethod
    def start_machine(self, machine: Machine) -> None:
        """Start the machine."""

    @abc.abstractmethod
    def list_storage_pools(self) -> tp.Collection[AbstractStoragePool]:
        """List storage pools."""


class DummyPoolDriver(AbstractPoolDriver):
    def __init__(self, pool: MachinePool, dry_run: bool = False):
        if pool.driver_spec is None or pool.driver_spec.KIND != "dummy":
            raise ValueError(
                f"Unsupported driver spec kind: "
                f"{pool.driver_spec.KIND if pool.driver_spec else None!r}"
            )
        super().__init__(dry_run=dry_run)

    def get_pool_info(self) -> MachinePool:
        """Get pool info."""
        return MachinePool(driver_spec=DummyPoolDriverSpec())

    def list_pool_resources(
        self,
    ) -> tp.Tuple[
        MachinePool,
        tp.Collection[AbstractStoragePool],
        tp.Collection[tp.Tuple[Machine, tp.Tuple[Port, ...]]],
        tp.Collection[MachineVolume],
    ]:
        """List pool resources."""
        return (
            MachinePool(driver_spec=DummyPoolDriverSpec()),
            [],
            [],
            [],
        )

    def list_machines(
        self,
    ) -> tp.Collection[tp.Tuple[Machine, tp.Tuple[Port, ...]]]:
        """List machines."""
        return []

    def create_machine(
        self,
        machine: Machine,
        volumes: tp.Iterable[MachineVolume],
        ports: tp.Iterable[Port],
    ) -> tp.Tuple[Machine, tp.Tuple[Port, ...]]:
        """Create a machine."""
        return machine, tuple(ports)

    def delete_machine(self, machine: Machine, delete_volumes: bool = True) -> None:
        pass

    def get_machine(
        self, machine: sys_uuid.UUID
    ) -> tp.Tuple[Machine, tp.Tuple[Port, ...]]:
        """Get machine from data plane."""
        return (
            Machine(
                uuid=machine,
                name="dummy-machine",
                cores=1,
                ram=1024,
                # "running" is not a MachineStatus, so building this model
                # raised instead of returning a machine.
                status=MachineStatus.ACTIVE.value,
                project_id=SYSTEM_PROJECT_ID,
            ),
            tuple(),
        )

    def create_volume(self, volume: MachineVolume) -> MachineVolume:
        """Create a new volume."""

    def delete_volume(self, volume: MachineVolume) -> None:
        """Delete the volume from data plane."""

    def list_volumes(
        self, machine: tp.Optional[Machine] = None
    ) -> tp.Iterable[MachineVolume]:
        """Return volume list from data plane."""
        return []

    def get_volume(self, volume: sys_uuid.UUID) -> MachineVolume:
        """Get the volume by uuid."""

    def resize_volume(self, volume: MachineVolume) -> None:
        """Resize the volume."""

    def attach_volume(self, volume: MachineVolume) -> None:
        """Attach the volume."""

    def detach_volume(self, volume: MachineVolume) -> None:
        """Detach the volume."""

    def attach_port(self, machine: Machine, port: Port) -> None:
        """Attach the port."""

    def detach_port(self, machine: Machine, port: Port) -> None:
        """Detach the port."""

    def set_machine_cores(self, machine: Machine, cores: int) -> None:
        """Set machine cores."""

    def set_machine_ram(self, machine: Machine, ram: int) -> None:
        """Set machine ram."""

    def reset_machine(self, machine: Machine) -> None:
        """Reset the machine."""

    def recreate_machine(
        self,
        machine: Machine,
        ports: tp.Optional[tp.Collection[Port]] = None,
    ) -> None:
        """Recreate the machine."""

    def rename_machine(self, machine: Machine, name: str) -> None:
        """Rename the machine."""

    def shutdown_machine(self, machine: Machine, force: bool = False) -> None:
        """Shutdown the machine."""

    def start_machine(self, machine: Machine) -> None:
        """Start the machine."""

    def list_storage_pools(self) -> tp.Collection[AbstractStoragePool]:
        """List storage pools."""
        return []


class MetaPool(meta.MetaCoordinatorDataPlaneModel):
    """Machine pool meta model."""

    __driver_map__ = {}

    driver_spec = properties.property(
        types_dynamic.KindModelSelectorType(
            types_dynamic.KindModelType(LibvirtPoolDriverSpec),
            types_dynamic.KindModelType(ExordosLocalHyperDriverSpec),
            types_dynamic.KindModelType(DummyPoolDriverSpec),
        ),
        required=True,
    )
    machine_type = properties.property(
        types.Enum([t.value for t in NodeType]),
        default=NodeType.VM.value,
    )
    all_cores = properties.property(types.Integer(), default=0)
    all_ram = properties.property(types.Integer(), default=0)
    avail_cores = properties.property(types.Integer(), default=0)
    avail_ram = properties.property(types.Integer(), default=0)
    cores_ratio = properties.property(types.Float(min_value=0.0), default=1.0)
    ram_ratio = properties.property(types.Float(min_value=0.0), default=1.0)
    status = properties.property(
        types.Enum([s.value for s in MachinePoolStatus]),
        default=MachinePoolStatus.ACTIVE.value,
    )
    storage_pools = properties.property(
        types.TypedList(
            types_dynamic.KindModelSelectorType(
                types_dynamic.KindModelType(ThinStoragePool),
            ),
        ),
        default=list,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dp_machine_map = {}
        self.dp_port_map = {}
        self.dp_volume_map = {}
        self.dp_storage_pool_map = {}

    def load_driver(self) -> AbstractPoolDriver:
        """
        Load the driver for the machine pool.

        The driver is restored from the cache if it is already loaded.
        """
        driver_key = str(self.driver_spec)

        if driver_key in self.__driver_map__:
            return self.__driver_map__[driver_key]

        driver_kind = self.driver_spec.KIND

        class_ = utils.load_from_entry_point(c.EP_MACHINE_POOL_DRIVERS, driver_kind)

        # NOTE(akremenetsky): We should use command approach for dry_run in agents,
        # but it hasn't implemented yet so use environment variable.
        # https://github.com/exordos/gcl_sdk/issues/124
        # Check for dry run mode based on environment variable
        dry_run = str(os.getenv(DRY_RUN_ENV)).lower() in {"1", "true", "yes"}

        driver = class_(self, dry_run=dry_run)
        self.__driver_map__[driver_key] = driver
        return driver

    def get_meta_model_fields(self) -> tp.Optional[tp.Set[str]]:
        """Return a list of meta fields or None.

        Meta fields are the fields that cannot be fetched from
        the data plane or we just want to save them into the meta file.

        `None` means all fields are meta fields but it doesn't mean they
        won't be updated from the data plane.
        """
        return {"uuid", "driver_spec", "machine_type", "cores_ratio", "ram_ratio"}

    def restore_from_dp(self, **kwargs) -> None:
        """Load the pool information."""
        driver = self.load_driver()
        pool_info, storage_pools, machines, volumes = driver.list_pool_resources()
        self.dp_machine_map = {m.uuid: m for m, _ in machines}
        self.dp_port_map = {m.uuid: ports for m, ports in machines}
        self.dp_volume_map = {v.uuid: v for v in volumes}
        self.dp_storage_pool_map = {p.uuid: p for p in storage_pools}

        # NOTE(akremenetsky): Use only the storage pool specified in the
        # driver spec.
        self.storage_pools = list(storage_pools)

        self.all_cores = int(pool_info.all_cores * self.cores_ratio)
        self.all_ram = int(pool_info.all_ram * self.ram_ratio)
        self.avail_cores = self.all_cores - sum(
            m.cores for m in self.dp_machine_map.values()
        )
        self.avail_ram = self.all_ram - sum(m.ram for m in self.dp_machine_map.values())

    def dump_to_dp(self, **kwargs) -> None:
        """Configure the pool."""
        # Actually we do nothing to configure or dump to pool at the moment
        # but we need to synchronize the pool state since it will be used by
        # machines and volumes as their dependencies.
        self.restore_from_dp(**kwargs)


class MetaVolume(meta.MetaCoordinatorDataPlaneModel):
    """Volume meta model."""

    pool = properties.property(types.UUID(), required=True)
    name = properties.property(types.String(max_length=255), default=None)
    image = properties.property(
        types.AllowNone(types.String(max_length=255)), default=None
    )

    size = properties.property(types.Integer(min_value=1, max_value=1000000))
    boot = properties.property(types.Boolean(), default=True)
    label = properties.property(
        types.AllowNone(types.String(max_length=127)), default=None
    )
    device_type = properties.property(types.String(max_length=64), default="")
    speed = properties.property(
        types.Enum([s.value for s in DiskSpeed]),
        default=DiskSpeed.WARM.value,
    )
    ephemeral = properties.property(types.Boolean(), default=False)
    # Name of the storage pool this volume was scheduled onto. None until
    # dump_to_dp() first creates it; fixed from then on (see
    # get_meta_model_fields) so later cycles don't reshuffle existing
    # volumes across pools.
    storage_pool = properties.property(
        types.AllowNone(types.String(max_length=255)), default=None
    )
    index = properties.property(
        types.Integer(min_value=0, max_value=4096),
        default=4096,
    )
    machine = properties.property(types.AllowNone(types.UUID()), default=None)
    status = properties.property(
        types.Enum([s.value for s in VolumeStatus]),
        default=VolumeStatus.NEW.value,
    )
    project_id = properties.property(types.UUID(), required=True, read_only=True)

    def _from_dp_volume(self, dp_volume: MachineVolume) -> None:
        self.name = dp_volume.name
        self.size = dp_volume.size
        self.index = dp_volume.index
        self.machine = dp_volume.machine
        self.device_type = dp_volume.device_type
        self.status = dp_volume.status

    def _actualize_attachment(self, pool: MetaPool, dp_volume: MachineVolume) -> None:
        """Actualize the attachment of the volume."""
        # Nothing to do if the volume's machine is correct
        if self.machine == dp_volume.machine:
            return

        driver = pool.load_driver()

        # Detach the volume
        if self.machine is None:
            self._detach_volume(pool, driver, dp_volume)
            return

        # Attach the volume
        if dp_volume.machine is None:
            dp_volume.machine = self.machine
            self._attach_volume(pool, driver, dp_volume)
            return

        # Reattach from one machine to another
        self._detach_volume(pool, driver, dp_volume)
        dp_volume.machine = self.machine
        self._attach_volume(pool, driver, dp_volume)

    def _create_volume(
        self,
        pool: MetaPool,
        driver: AbstractPoolDriver,
        dp_volume: MachineVolume,
    ) -> None:
        dp_volume = driver.create_volume(dp_volume)
        self.status = dp_volume.status
        LOG.info("The volume %s created", self.uuid)

    def _delete_volume(
        self,
        pool: MetaPool,
        driver: AbstractPoolDriver,
        dp_volume: MachineVolume,
    ) -> None:
        driver.delete_volume(dp_volume)
        LOG.info("The volume %s deleted", self.uuid)

    def _attach_volume(
        self,
        pool: MetaPool,
        driver: AbstractPoolDriver,
        dp_volume: MachineVolume,
    ) -> None:
        try:
            driver.attach_volume(dp_volume)
        except VolumeAlreadyAttachedError:
            # Volume is already attached, do nothing
            LOG.warning(
                "The volume %s is already attached, do nothing",
                self.uuid,
            )
        else:
            LOG.info(
                "The volume %s attached to the machine %s",
                self.uuid,
                self.machine,
            )

    def _detach_volume(
        self,
        pool: MetaPool,
        driver: AbstractPoolDriver,
        dp_volume: MachineVolume,
    ) -> None:
        if dp_volume.machine is None:
            LOG.debug(
                "The volume %s doesn't have a machine, skip detaching",
                self.uuid,
            )
            return

        try:
            driver.detach_volume(dp_volume)
        except VolumeNotAttachedError:
            # Volume is already detached, do nothing
            LOG.warning(
                "The volume %s is already detached, do nothing",
                self.uuid,
            )
        else:
            LOG.info(
                "The volume %s detached from the machine %s",
                self.uuid,
                dp_volume.machine,
            )

    def _to_dp_volume(self) -> MachineVolume:
        """Convert the volume to the data plane."""
        return MachineVolume(
            uuid=self.uuid,
            name=self.name,
            image=self.image,
            size=self.size,
            boot=self.boot,
            label=self.label,
            device_type=self.device_type,
            speed=self.speed,
            ephemeral=self.ephemeral,
            storage_pool=self.storage_pool,
            index=self.index,
            machine=self.machine,
            project_id=self.project_id,
        )

    def _is_root_volume(self) -> bool:
        return self.machine and self.index == 0

    def _find_storage_pool(
        self, pool: MetaPool, size: int
    ) -> tp.Optional[AbstractStoragePool]:
        """Find the storage pool to use for this volume.

        If the volume has already been scheduled (self.storage_pool is
        set), only that pool is considered - the check here is purely
        about whether it still has room, e.g. for a resize. Otherwise
        every pool matching this volume's speed/ephemeral request
        (exact match) is a candidate.
        """
        if self.storage_pool is not None:
            candidates = (
                sp for sp in pool.storage_pools if sp.name == self.storage_pool
            )
        else:
            candidates = (
                sp
                for sp in pool.storage_pools
                if sp.speed == self.speed and sp.ephemeral == self.ephemeral
            )

        return next((sp for sp in candidates if sp.has_capacity(size)), None)

    def _has_storage_capacity(
        self, pool: MetaPool, size: tp.Optional[int] = None
    ) -> bool:
        if not pool.storage_pools:
            return False

        size = size if size is not None else self.size
        return self._find_storage_pool(pool, size) is not None

    def _allocate_capacity(self, pool: MetaPool, size: tp.Optional[int] = None) -> None:
        size = size if size is not None else self.size
        storage_pool = self._find_storage_pool(pool, size)
        storage_pool.allocate_capacity(size)

    def get_meta_model_fields(self) -> tp.Optional[tp.Set[str]]:
        """Return a list of meta fields or None.

        Meta fields are the fields that cannot be fetched from
        the data plane or we just want to save them into the meta file.

        `None` means all fields are meta fields but it doesn't mean they
        won't be updated from the data plane.
        """
        return {
            "uuid",
            "pool",
            "image",
            "boot",
            "label",
            "device_type",
            "speed",
            "ephemeral",
            "storage_pool",
            "project_id",
        }

    def dump_to_dp(self, pool: MetaPool) -> None:
        """Create the volume to the data plane."""
        driver: AbstractPoolDriver = pool.load_driver()

        if self.uuid in pool.dp_volume_map:
            # The volume already exists in the data plane
            # Reuse it
            dp_volume = pool.dp_volume_map[self.uuid]
        else:
            # Find a storage pool matching this volume's speed/ephemeral
            # request with enough room for it.
            storage_pool = self._find_storage_pool(pool, self.size)
            if storage_pool is None:
                self.status = VolumeStatus.ERROR.value
                return

            self.storage_pool = storage_pool.name

            dp_volume = MachineVolume(
                uuid=self.uuid,
                name=self.name,
                image=self.image,
                size=self.size,
                boot=self.boot,
                label=self.label,
                device_type=self.device_type,
                speed=self.speed,
                ephemeral=self.ephemeral,
                storage_pool=self.storage_pool,
                index=self.index,
                # TODO(akremenetsky): Detect machine without volume name
                machine=self.machine,
                project_id=self.project_id,
            )
            self._create_volume(pool, driver, dp_volume)
            storage_pool.allocate_capacity(self.size)

        self._from_dp_volume(dp_volume)

        # The volume without machine, just create and exit.
        if self.machine is None:
            return

        # Don't attach volumes if they belongs to a machine
        # but the machine doesn't exist.
        if self.machine not in pool.dp_machine_map:
            LOG.debug("The machine %s doesn't exist, skip attaching", self.machine)
            return

        # It's a root volume. It will be attached in the machine model.
        if self._is_root_volume():
            return

        self._attach_volume(pool, driver, dp_volume)

    def restore_from_dp(self, pool: tp.Optional[MetaPool]) -> None:
        """Load the pool information."""
        # Prevent actualization when pool is not provided
        if pool is None:
            raise ValueError(f"The pool is not provided for volume {self.uuid}")

        if self.uuid not in pool.dp_volume_map:
            raise ua_driver_exc.ResourceNotFound(resource=self)

        dp_volume = pool.dp_volume_map[self.uuid]
        self._from_dp_volume(dp_volume)

    def delete_from_dp(self, pool: MetaPool) -> None:
        """Delete the resource from the data plane."""
        if self.uuid not in pool.dp_volume_map:
            raise ua_driver_exc.ResourceNotFound(resource=self)

        driver: AbstractPoolDriver = pool.load_driver()
        dp_volume = pool.dp_volume_map[self.uuid]
        self._detach_volume(pool, driver, dp_volume)
        self._delete_volume(pool, driver, dp_volume)

    def update_on_dp(self, pool: MetaPool) -> None:
        """Update the resource on the data plane."""
        if self.uuid not in pool.dp_volume_map:
            raise ua_driver_exc.ResourceNotFound(resource=self)

        driver: AbstractPoolDriver = pool.load_driver()
        dp_volume: MachineVolume = pool.dp_volume_map[self.uuid]
        machine = dp_volume.machine
        unknown_action = True

        # A special case for root volumes. If the condition is true, it
        # means the machine failed to be created on previous iteration.
        # So do nothing, just give another chance to the agent create
        # the machine.
        if self._is_root_volume() and self.machine not in pool.dp_machine_map:
            return

        # Resize the volume
        if self.size != dp_volume.size:
            # Take the delta before `dp_volume.size` is overwritten below:
            # computing it afterwards always yields 0, so the growth was
            # never charged to the storage pool and `capacity_provisioned`
            # drifted below what the volumes actually occupy.
            size_delta = self.size - dp_volume.size

            # Check the storage pool has enough capacity
            if not self._has_storage_capacity(pool, size_delta):
                self.status = VolumeStatus.ERROR.value
                return

            unknown_action = False
            dp_volume.size = self.size
            driver.resize_volume(dp_volume)
            self._allocate_capacity(pool, size_delta)
            LOG.info("The volume %s resized.", self.uuid)

        # Attachments
        if self.machine != dp_volume.machine:
            unknown_action = False
            self._actualize_attachment(pool, dp_volume)

        # TODO(akremenetsky): Add image actualization for volumes

        if unknown_action:
            LOG.error("Unknown volume action")

        dp_volume = driver.get_volume(self.uuid)
        self._from_dp_volume(dp_volume)

        # Not all drivers support machine field on `get` operation
        self.machine = machine


class MetaMachine(meta.MetaCoordinatorDataPlaneModel):
    """Machine meta model."""

    name = properties.property(types.String(max_length=255), default="")
    cores = properties.property(types.Integer(min_value=0, max_value=4096), default=0)
    ram = properties.property(types.Integer(min_value=0), default=0)
    status = properties.property(
        types.Enum([s.value for s in MachineStatus]),
        default=MachineStatus.NEW.value,
    )
    machine_type = properties.property(
        types.Enum([t.value for t in NodeType]),
        default=NodeType.VM.value,
    )
    node = properties.property(types.AllowNone(types.UUID()), default=None)
    pool = properties.property(types.AllowNone(types.UUID()))
    boot = properties.property(
        types.Enum([b.value for b in BootAlternative]),
        default=BootAlternative.network.value,
    )
    image = properties.property(
        types.AllowNone(types.String(max_length=512)), default=None
    )
    project_id = properties.property(types.UUID(), required=True, read_only=True)
    port_info = properties.property(types.Dict(), default=dict)

    @property
    def _is_core_machine(self) -> bool:
        """Determine if the machine belongs to the core set."""
        # NOTE(akremenetsky): We don't have any metadata information in
        # nodes/machines except the name and description. So the first
        # implementation is pretty straightforward and just checks the
        # machine name. In the future versions we need to associate metadata
        # to the machine and keep this info there.
        core_machine_name_prefix = "core-set-node"
        return self.name.startswith(core_machine_name_prefix)

    def _port(self) -> Port:
        ipv4 = (
            netaddr.IPAddress(self.port_info["ipv4"])
            if self.port_info.get("ipv4")
            else None
        )
        mask = (
            netaddr.IPAddress(self.port_info["mask"])
            if self.port_info.get("mask")
            else None
        )

        return Port(
            subnet=sys_uuid.uuid4(),
            ipv4=ipv4,
            mask=mask,
            mac=self.port_info["mac"],
            status=PortStatus.ACTIVE,
            project_id=self.project_id,
            source=self.port_info.get("source"),
        )

    def _create_machine(
        self,
        driver: AbstractPoolDriver,
        dp_machine: Machine,
        volumes: tp.Collection[MachineVolume],
        ports: tp.Collection[Port],
    ) -> None:
        dp_machine, _ = driver.create_machine(dp_machine, volumes, ports)
        self.status = dp_machine.status
        LOG.info("The machine %s created", self.uuid)

    def _delete_machine(self, driver: AbstractPoolDriver, dp_machine: Machine) -> None:
        driver.delete_machine(dp_machine)
        LOG.info("The machine %s deleted", self.uuid)

    def _from_dp_machine(self, dp_machine: Machine, ports: tp.Collection[Port]) -> None:
        self.cores = dp_machine.cores
        self.ram = dp_machine.ram
        self.status = dp_machine.status
        self.boot = dp_machine.boot

        for port in ports:
            # Only MAC and source are available in the data plane
            self.port_info = {
                "ipv4": self.port_info.get("ipv4"),
                "mask": self.port_info.get("mask"),
                "mac": port.mac,
                "source": port.source,
            }

            # TODO(akremenetsky): Support multiple interfaces
            break

        # Ignore the image of core machines to perform update procedure via
        # the guest machine driver and SeedOS in autonomous mode
        if self._is_core_machine:
            return

        # Don't try to restore image from legacy machine since it is not
        # available in the data plane. So to avoid recreation of such
        # machines just take the image from meta. For legacy machines
        # we need to fit data plane to have ability for machine update.
        self.image = dp_machine.image or self.image

    def _has_enough_resources(
        self,
        pool: MetaPool,
        cores: tp.Optional[int] = None,
        ram: tp.Optional[int] = None,
    ) -> bool:
        if cores is not None and pool.avail_cores < cores:
            return False

        if ram is not None and pool.avail_ram < ram:
            return False

        return True

    def _allocate_resources(
        self,
        pool: MetaPool,
        cores: tp.Optional[int] = None,
        ram: tp.Optional[int] = None,
    ) -> None:
        if cores is not None:
            pool.avail_cores -= cores

        if ram is not None:
            pool.avail_ram -= ram

    def get_meta_model_fields(self) -> tp.Optional[tp.Set[str]]:
        """Return a list of meta fields or None.

        Meta fields are the fields that cannot be fetched from
        the data plane or we just want to save them into the meta file.

        `None` means all fields are meta fields but it doesn't mean they
        won't be updated from the data plane.
        """
        return {
            "uuid",
            "machine_type",
            "node",
            "pool",
            "project_id",
            "port_info",
            "image",
            "name",
        }

    def dump_to_dp(self, pool: MetaPool, volumes: tp.Collection[MetaVolume]) -> None:
        """Create the machine in the pool."""
        driver: AbstractPoolDriver = pool.load_driver()

        # The machine is already present in the data plane.
        # It's not ordinary behavior but there is a couple of cases
        # where this can happen during recovery or migration.
        # So do nothing and let the `update_on_dp` handle it next iteration.
        # The iteration is skipped intentionally to have a chance to stop
        # the service during migration if something goes wrong.
        if self.uuid in pool.dp_machine_map:
            LOG.warning(
                "Machine %s already exists in pool %s. "
                "It will be actualized on the next iteration.",
                self.uuid,
                pool.uuid,
            )
            return

        # Validation all resources are ready for the machine
        volumes = sorted(volumes, key=lambda v: v.index)

        # Root volume must be the first
        if not volumes or volumes[0].index != 0:
            raise RootVolumeNotFound(machine=self.uuid)

        # Seems something went wrong with the root volume
        # Mark the machine is in error state as well.
        if volumes[0].status == VolumeStatus.ERROR:
            self.status = MachineStatus.ERROR.value
            return

        # Check the pool has enough resources.
        # If the pool doesn't have enough resources, mark the machine
        # as `NEED_RESCHEDULE` and return.
        if not self._has_enough_resources(pool, self.cores, self.ram):
            self.status = MachineStatus.NEED_RESCHEDULE.value
            return

        dp_machine = Machine(
            uuid=self.uuid,
            name=self.name,
            cores=self.cores,
            ram=self.ram,
            machine_type=self.machine_type,
            node=self.node,
            boot=self.boot,
            image=self.image,
            project_id=self.project_id,
        )

        # Find the related entities
        pool_volumes = tuple(v._to_dp_volume() for v in volumes)

        # TODO(akremenetsky): This simplest implementation is fine while
        # we have only single flat network.
        ports = (self._port(),)

        self._create_machine(driver, dp_machine, pool_volumes, ports)
        self._allocate_resources(pool, self.cores, self.ram)

    def restore_from_dp(
        self, pool: tp.Optional[MetaPool], volumes: tp.Collection[MetaVolume]
    ) -> None:
        """Load the machine from the data plane."""
        # Prevent actualization when pool is not provided
        if pool is None:
            raise ValueError(f"The pool is not provided for machine {self.uuid}")

        if self.uuid not in pool.dp_machine_map:
            raise ua_driver_exc.ResourceNotFound(resource=self)

        dp_machine = pool.dp_machine_map[self.uuid]
        dp_ports = pool.dp_port_map[self.uuid]

        # NOTE(akremenetsky): The current implementation support single connection
        # but machines in core set have two connections. They are for main and boot
        # network. So just trunk the second port due to current limitation.
        dp_ports = dp_ports[:1]

        self._from_dp_machine(dp_machine, dp_ports)

    def delete_from_dp(
        self, pool: MetaPool, volumes: tp.Collection[MetaVolume]
    ) -> None:
        """Delete the machine from the data plane."""
        if self.uuid not in pool.dp_machine_map:
            raise ua_driver_exc.ResourceNotFound(resource=self)

        driver: AbstractPoolDriver = pool.load_driver()
        dp_machine = pool.dp_machine_map[self.uuid]
        self._delete_machine(driver, dp_machine)

    def update_on_dp(self, pool: MetaPool, volumes: tp.Collection[MetaVolume]) -> None:
        """Update the machine on the data plane."""
        if self.uuid not in pool.dp_machine_map:
            raise ua_driver_exc.ResourceNotFound(resource=self)

        dp_machine: Machine = pool.dp_machine_map[self.uuid]
        driver: AbstractPoolDriver = pool.load_driver()
        unknown_action = True

        # Cores
        if self.cores != dp_machine.cores:
            unknown_action = False

            # Mark the machine as error if the pool doesn't have enough
            # resources to update the machine. In the `create` case
            # the machine is marked as `NEED_RESCHEDULE` but it's not
            # possible in this case as we need to migrate the machine.
            # Such functionality is not implemented yet.
            need_cores = self.cores - dp_machine.cores
            if not self._has_enough_resources(pool, cores=need_cores):
                self.status = MachineStatus.ERROR.value
                LOG.error("Not enough Cores to update the machine %s", self.uuid)
                return

            # NOTE(akremenetsky): Legacy machines always have image=None.
            # Therefore we cannot update the image without modifying XML.
            # To make things simpler, the image is enriched when cores
            # are changed. This avoids the need to modify XML directly.
            # This "helper" has to be removed after full migration.
            if dp_machine.image is None:
                dp_machine.image = self.image
                LOG.info(
                    "Enriched legacy machine %s with image %s.",
                    self.uuid,
                    self.image,
                )

            driver.set_machine_cores(dp_machine, self.cores)
            self._allocate_resources(pool, cores=need_cores)
            LOG.info("The machine %s cores updated.", self.uuid)

        # Ram
        if self.ram != dp_machine.ram:
            unknown_action = False

            # Mark the machine as error if the pool doesn't have enough
            # resources to update the machine. In the `create` case
            # the machine is marked as `NEED_RESCHEDULE` but it's not
            # possible in this case as we need to migrate the machine.
            # Such functionality is not implemented yet.
            need_ram = self.ram - dp_machine.ram
            if not self._has_enough_resources(pool, ram=need_ram):
                self.status = MachineStatus.ERROR.value
                LOG.error("Not enough RAM to update the machine %s", self.uuid)
                return

            driver.set_machine_ram(dp_machine, self.ram)
            self._allocate_resources(pool, ram=need_ram)
            LOG.info("The machine %s ram updated.", self.uuid)

        # TODO(akremenetsky): Actually update image logic is more suitable for
        # volumes update but for backward compatibility we keep it here.
        # Image
        if dp_machine.image and self.image != dp_machine.image:
            unknown_action = False

            if not self._is_core_machine:
                # Recreate the machine, Seed OS flashes the new image
                dp_machine.image = self.image
                dp_machine.boot = self.boot

                # The node is going to be updated. The update process requires
                # to switch the node into the boot network.
                driver.recreate_machine(dp_machine, ports=(self._port(),))
                LOG.info("The machine %s image updated.", self.uuid)
            else:
                # TODO(akremenetsky): Update machine meta/description but don't recreate
                # the machine.
                pass

        # Boot (Finished update process)
        # Switching boot mode means the node finished the update process.
        # Need to switch back the node into the main network.
        if dp_machine.boot != self.boot:
            unknown_action = False

            dp_machine.boot = self.boot
            # The node has been updated.
            # Switch back the node into the main network.
            driver.recreate_machine(dp_machine, ports=(self._port(),))
            LOG.info("The machine %s boot mode updated.", self.uuid)

        if unknown_action:
            LOG.error("Unknown update action for machine %s", self.uuid)

        # Get the updated machine state from the driver
        updated_machine, ports = driver.get_machine(self.uuid)
        self._from_dp_machine(updated_machine, ports)


class PoolAgentDriver(meta.MetaCoordinatorAgentDriver):
    # Order matters
    __model_map__ = {
        "pool": MetaPool,
        "pool_volume": MetaVolume,
        "pool_machine": MetaMachine,
    }

    __coordinator_map__ = {
        "pool": {},
        "pool_volume": {
            "pool": {
                "kind": "pool",
                "relation": "pool_volume:pool",
            },
        },
        "pool_machine": {
            "pool": {
                "kind": "pool",
                "relation": "pool_machine:pool",
            },
            "volumes": {
                "kind": "pool_volume",
                "relation": "pool_volume:machine",
            },
        },
    }


class LocalPoolAgentDriver(PoolAgentDriver):
    def get_capabilities(self) -> list[str]:
        """Returns a list of capabilities supported by the driver."""
        return super().get_capabilities() + ["local_pool"]

    def list(self, capability: str) -> list[ua_models.Resource]:
        # "local_pool" is a scheduling-only marker capability (matches
        # this agent to exordos_local_hyper pools pinned to its node), not
        # an actualizable resource kind - there's nothing to list for it.
        if capability == "local_pool":
            return []
        return super().list(capability)
