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

import typing as tp
import uuid as sys_uuid

from restalchemy.dm import models as ra_models
from restalchemy.dm import properties
from restalchemy.dm import relationships
from restalchemy.dm import types as ra_types
from restalchemy.dm import types_dynamic
from restalchemy.dm import types_network as ra_nettypes

from gcl_sdk.agents.universal.dm import models as ua_models
from gcl_sdk.common import types as common_types
from gcl_sdk.infra import constants as pc
from gcl_sdk.infra import exceptions as infra_exc


class Volume(
    ua_models.TargetResourceKindAwareMixin,
    ra_models.ModelWithRequiredUUID,
    ra_models.ModelWithProject,
    ra_models.ModelWithNameDesc,
    ra_models.ModelWithTimestamp,
):
    node = properties.property(ra_types.AllowNone(ra_types.UUID()), default=None)
    size = properties.property(ra_types.Integer(min_value=1, max_value=1000000))
    image = properties.property(
        ra_types.AllowNone(ra_types.String(max_length=255)), default=None
    )
    boot = properties.property(ra_types.Boolean(), default=True)
    label = properties.property(
        ra_types.AllowNone(ra_types.String(max_length=127)), default=None
    )
    device_type = properties.property(ra_types.String(max_length=64), default="")
    index = properties.property(
        ra_types.Integer(min_value=0, max_value=4096), default=4096
    )
    status = properties.property(
        ra_types.Enum([s.value for s in pc.VolumeStatus]),
    )

    @classmethod
    def get_resource_kind(cls) -> str:
        """Return the resource kind."""
        return "volume"

    def get_resource_target_fields(self) -> tp.Collection[str]:
        """Return the collection of target fields.

        Refer to the Resource model for more details about target fields.
        """
        return frozenset(
            (
                "uuid",
                "name",
                "node",
                "size",
                "image",
                "boot",
                "index",
                "device_type",
                "project_id",
            )
        )


class AbstractDiskSpec(
    ra_models.SimpleViewMixin,
    types_dynamic.AbstractKindModel,
):
    """The abstract model for disk specification.

    This model is used to represent the disks specification in scope of node.
    For instance, which disk is a root disk and which disks are extra disks.
    Partition tables, mount points, etc. are defined in scope of this model.
    """

    def volumes(
        self, node: Node, project_id: sys_uuid.UUID | None = None
    ) -> tp.Collection[Volume]:
        """Lists all volumes that should be created or modified on the node."""
        self.validate()
        return tuple()

    def validate(self) -> None:
        """Validate the disk spec."""
        pass


class RootDiskSpec(AbstractDiskSpec):
    """The model represents the root disk specification.

    The simplest specification consist of only a single root disk.
    """

    KIND = "root_disk"

    image = properties.property(ra_types.String(max_length=255), required=True)
    size = properties.property(
        ra_types.Integer(min_value=1, max_value=1000000),
        required=True,
        default=pc.DEF_ROOT_DISK_SIZE,
    )

    def volumes(
        self, node: Node, project_id: sys_uuid.UUID | None = None
    ) -> tp.Collection[Volume]:
        """Return a collection of volumes in according to the disk spec.

        Return single volume for the root disk spec.
        """
        self.validate()

        if node.node_type == pc.NodeType.HW:
            return tuple()

        # Use `root-volume` name for backward compatibility
        volume_uuid = sys_uuid.uuid5(node.uuid, "root-volume")
        volume = Volume(
            uuid=volume_uuid,
            name="root-volume",
            node=node.uuid,
            size=self.size,
            image=self.image,
            index=0,
            project_id=project_id or node.project_id,
            status=pc.VolumeStatus.NEW.value,
        )
        return (volume,)


class DisksSpecDiskType(common_types.SchematicType):
    __scheme__ = {
        "size": ra_types.Integer(min_value=1, max_value=1000000),
        "label": ra_types.String(max_length=128),
        "image": ra_types.String(max_length=256),
        "mount_point": ra_types.String(max_length=512),
        "fs": ra_types.String(max_length=256),
    }
    __mandatory__ = {"size"}


class DisksSpec(AbstractDiskSpec):
    """The model represents the collection of disks.

    The collection of disks where the first disk is a
    root disk and the rest are extra disks.

    Example:
    $core.compute.nodes:
      node:
        name: node
        project_id: "12345678-c625-4fee-81d5-f691897b8142"
        cores: 1
        ram: 1024
        disk_spec:
          kind: "disks"
          disks:
            - size: 10
              image: "https://repo.exordos.com/exordos-base/latest/exordos-base.raw.gz"
            - size: 100
              label: "data"
              mount_point: "/var/data"
              fs: "ext4"
            - size: 50
              label: "logs"
              mount_point: "/var/log"
              fs: "ext4"
    """

    KIND = "disks"
    ROOT_LABEL = "root-volume"

    disks = properties.property(ra_types.TypedList(DisksSpecDiskType()), default=list)

    def validate(self) -> None:
        """Validate the disk spec."""
        # FIXME(akremenetsky): It's fine for diskless systems
        if len(self.disks) == 0:
            return

        # Validate the root disk
        root = self.disks[0]
        if root.get("image") is None:
            raise ValueError("Root disk image is not specified")

        if root.get("mount_point") and root["mount_point"] != "/":
            raise ValueError("Root disk mount point should be '/'")

        extra_disks = self.disks[1:]

        if not extra_disks:
            return

        mount_points = set()
        labels = {self.ROOT_LABEL}
        for disk in extra_disks:
            # Check for duplicate mount points
            if mount_point := disk.get("mount_point"):
                if mount_point in mount_points:
                    raise ValueError("Duplicate mount point")
                mount_points.add(mount_point)

            # Check fs or image, but not both
            if disk.get("fs") and disk.get("image"):
                raise ValueError("Disk can have either fs or image, but not both")

            # Check labels, the labels are mandatory and unique for extra disks
            if not disk.get("label") or disk["label"] in labels:
                raise ValueError("Disk label is not specified or duplicate")

            labels.add(disk["label"])

            # TODO(akremenetsky): Add specific validation for fs like `swap`.

    def volumes(
        self, node: Node, project_id: sys_uuid.UUID | None = None
    ) -> tp.Collection[Volume]:
        """Return a collection of volumes in according to the disk spec.

        Return single volume for the root disk spec.
        """
        self.validate()

        if node.node_type == pc.NodeType.HW:
            return tuple()

        # FIXME(akremenetsky): It's fine for diskless systems
        if len(self.disks) == 0:
            return tuple()

        # Prepare the root disk
        root = self.disks[0]

        # Use `root-volume` name for backward compatibility
        root_volume_uuid = sys_uuid.uuid5(node.uuid, "root-volume")
        root_volume = Volume(
            uuid=root_volume_uuid,
            name="root-volume",
            node=node.uuid,
            size=int(root["size"]),
            image=root["image"],
            index=0,
            project_id=project_id or node.project_id,
            status=pc.VolumeStatus.NEW.value,
        )

        extra_disks = self.disks[1:]

        if not extra_disks:
            return (root_volume,)

        volumes = []
        for idx, disk in enumerate(extra_disks):
            volume_uuid = sys_uuid.uuid5(node.uuid, disk["label"])
            volume = Volume(
                uuid=volume_uuid,
                name=disk["label"],
                label=disk["label"],
                node=node.uuid,
                size=int(disk["size"]),
                image=disk.get("image"),
                index=idx + 1,
                project_id=project_id or node.project_id,
                status=pc.VolumeStatus.NEW.value,
            )
            volumes.append(volume)

        return (root_volume, *volumes)


class Node(
    ua_models.TargetResourceKindAwareMixin,
    ra_models.ModelWithRequiredUUID,
    ra_models.ModelWithProject,
    ra_models.ModelWithNameDesc,
    ra_models.ModelWithTimestamp,
):
    """The model represents a node in Exordos Core infrastructure.

    The model represents a virtual machine or a physical machine with
    specified characteristics such as cores, ram, root disk size, image,
    node type, default network, etc.
    """

    __init_resource_status__ = pc.NodeStatus.NEW.value

    cores = properties.property(
        ra_types.Integer(min_value=1, max_value=4096), required=True
    )
    ram = properties.property(ra_types.Integer(min_value=1), required=True)
    status = properties.property(
        ra_types.Enum([s.value for s in pc.NodeStatus]),
    )
    node_type = properties.property(
        ra_types.Enum([t.value for t in pc.NodeType]),
        default=pc.NodeType.VM.value,
    )
    default_network = properties.property(ra_types.Dict(), default=dict)
    disk_spec = properties.property(
        types_dynamic.KindModelSelectorType(
            types_dynamic.KindModelType(RootDiskSpec),
            types_dynamic.KindModelType(DisksSpec),
        ),
        required=True,
    )

    placement_policies = properties.property(
        ra_types.TypedList(ra_types.UUID()), default=list
    )
    hostname = properties.property(
        ra_types.AllowNone(ra_nettypes.Hostname()), default=None
    )

    @classmethod
    def get_resource_kind(cls) -> str:
        """Return the resource kind."""
        return "node"

    def get_resource_target_fields(self) -> tp.Collection[str]:
        """Return the collection of target fields.

        Refer to the Resource model for more details about target fields.
        """
        return frozenset(
            (
                "uuid",
                "name",
                "cores",
                "ram",
                "node_type",
                "project_id",
                "placement_policies",
                "disk_spec",
                "hostname",
            )
        )


class AbstractSetDiskSpec(
    ra_models.SimpleViewMixin,
    types_dynamic.AbstractKindModel,
):
    """The abstract model for disk specification of node set.

    This model is used to represent the disks specification in scope of
    node sets. For instance, which disk is a root disk and which disks
    are extra disks. Partition tables, mount points, etc. are defined in
    scope of this model.
    """

    def volumes(
        self, node_set: NodeSet, project_id: sys_uuid.UUID | None = None
    ) -> tp.Collection[Volume]:
        """Lists all volumes that should be created or modified on the node."""
        self.validate()
        return tuple()

    def validate(self) -> None:
        """Validate the disk spec."""
        pass

    def node_spec(self, node_set: NodeSet, node: sys_uuid.UUID) -> AbstractDiskSpec:
        """Lists all volumes that should be created or modified on the node."""
        raise NotImplementedError("Subclasses must implement this method.")


class SetRootDiskSpec(RootDiskSpec, AbstractSetDiskSpec):
    """The model represents the root disk specification.

    The simplest specification consist of only a single root disk.
    """

    def volumes(
        self, node_set: NodeSet, project_id: sys_uuid.UUID | None = None
    ) -> tp.Collection[Volume]:
        """Lists all volumes that should be created or modified on the node."""
        self.validate()
        return tuple()

    def node_spec(self, node_set: NodeSet, node: sys_uuid.UUID) -> AbstractDiskSpec:
        """Return the disk specification for the node."""
        return RootDiskSpec(
            image=self.image,
            size=self.size,
        )


class SetDisksSpec(DisksSpec, AbstractSetDiskSpec):
    """The model represents the collection of disks.

    The collection of disks where the first disk is a
    root disk and the rest are extra disks.

    Example:
    $core.compute.sets:
      node_set:
        name: node_set
        project_id: "12345678-c625-4fee-81d5-f691897b8142"
        cores: 1
        ram: 1024
        replicas: 3
        disk_spec:
          kind: "disks"
          disks:
            - size: 10
              image: "https://repo.exordos.com/exordos-base/latest/exordos-base.raw.gz"
            - size: 100
              label: "data"
              mount_point: "/var/data"
              fs: "ext4"
            - size: 50
              label: "logs"
              mount_point: "/var/log"
              fs: "ext4"
    """

    def volumes(
        self, node_set: NodeSet, project_id: sys_uuid.UUID | None = None
    ) -> tp.Collection[Volume]:
        """Lists all volumes that should be created or modified on the node."""
        self.validate()
        return tuple()

    def node_spec(self, node_set: NodeSet, node: sys_uuid.UUID) -> AbstractDiskSpec:
        """Return the disk specification for the node."""
        return DisksSpec(disks=self.disks)


class NodeSet(
    ra_models.ModelWithRequiredUUID,
    ra_models.ModelWithProject,
    ra_models.ModelWithNameDesc,
    ra_models.ModelWithTimestamp,
    ua_models.TargetResourceKindAwareMixin,
):
    """The model represents a node set in Exordos Core infrastructure.

    The node set is a group of nodes with the same characteristics. See the
    `Node` model for more details about node characteristics. The key field of
    the node set model is `replicas`. The different `set_type` interpretate this
    field in different ways. The the simplest `set_type` is `SET` where the
    `replicas` field is the number of nodes in the set.
    """

    __init_resource_status__ = pc.NodeStatus.NEW.value

    replicas = properties.property(
        ra_types.Integer(min_value=0, max_value=4096), default=1
    )
    cores = properties.property(
        ra_types.Integer(min_value=0, max_value=4096), required=True
    )
    ram = properties.property(ra_types.Integer(min_value=0), required=True)
    status = properties.property(
        ra_types.Enum([s.value for s in pc.NodeStatus]),
    )
    node_type = properties.property(
        ra_types.Enum([t.value for t in pc.NodeType]),
        default=pc.NodeType.VM.value,
    )
    default_network = properties.property(ra_types.Dict(), default=dict)
    disk_spec = properties.property(
        types_dynamic.KindModelSelectorType(
            types_dynamic.KindModelType(SetRootDiskSpec),
            types_dynamic.KindModelType(SetDisksSpec),
        ),
        required=True,
    )

    set_type = properties.property(
        ra_types.Enum([type_.value for type_ in pc.NodeSetType]),
        default=pc.NodeSetType.SET.value,
    )
    nodes = properties.property(ra_types.Dict(), default=dict)

    @classmethod
    def get_resource_kind(cls) -> str:
        """Return the resource kind."""
        return "node_set"

    def get_resource_target_fields(self) -> tp.Collection[str]:
        """Return the collection of target fields.

        Refer to the Resource model for more details about target fields.
        """
        return frozenset(
            (
                "uuid",
                "name",
                "replicas",
                "cores",
                "ram",
                "node_type",
                "set_type",
                "project_id",
                "disk_spec",
            )
        )


class AbstractTarget(types_dynamic.AbstractKindModel, ra_models.SimpleViewMixin):
    def target_nodes(self) -> tp.List[sys_uuid.UUID]:
        """Returns list of target nodes where config should be deployed."""
        return []

    def owners(self) -> tp.List[sys_uuid.UUID]:
        """Return list of owners objects where config bind to.

        For instance, the simplest case if an ordinary node config.
        In that case, the owner and target is the node itself.
        A more complex case is when a config is bound to a node set.
        In this case the owner is the set and the targets are all nodes
        in this set.
        """
        return []

    def are_owners_alive(self) -> bool:
        raise NotImplementedError()


class AbstractContentor(types_dynamic.AbstractKindModel, ra_models.SimpleViewMixin):
    def render(self) -> str:
        return ""


class NodeTarget(AbstractTarget):
    KIND = "node"

    node = properties.property(ra_types.UUID(), required=True)

    @classmethod
    def from_node(cls, node: sys_uuid.UUID) -> "NodeTarget":
        return cls(node=node)

    def target_nodes(self) -> tp.List[sys_uuid.UUID]:
        return [self.node]

    def owners(self) -> tp.List[sys_uuid.UUID]:
        """It's the simplest case with an ordinary node config.

        In that case, the owner and target is the node itself.
        If owners are deleted, the config will be deleted as well.
        """
        return [self.node]


class TextBodyConfig(AbstractContentor):
    KIND = "text"

    content = properties.property(ra_types.String(), required=True, default="")

    @classmethod
    def from_text(cls, text: str) -> "TextBodyConfig":
        return cls(content=text)

    def render(self) -> str:
        return self.content


class TemplateBodyConfig(AbstractContentor):
    KIND = "template"

    template = properties.property(ra_types.String(), required=True, default="")
    variables = properties.property(ra_types.Dict(), default=dict)

    def render(self) -> str:
        # TODO(akremenetsky): Will be added later
        raise NotImplementedError()


class OnChangeNoAction(types_dynamic.AbstractKindModel, ra_models.SimpleViewMixin):
    KIND = "no_action"


class OnChangeShell(types_dynamic.AbstractKindModel, ra_models.SimpleViewMixin):
    KIND = "shell"

    command = properties.property(
        ra_types.String(max_length=262144), required=True, default=""
    )

    @classmethod
    def from_command(cls, command: str) -> "OnChangeShell":
        return cls(command=command)


class Config(
    ra_models.ModelWithRequiredUUID,
    ra_models.ModelWithProject,
    ra_models.ModelWithNameDesc,
    ra_models.ModelWithTimestamp,
    ua_models.TargetResourceKindAwareMixin,
):
    __init_resource_status__ = pc.NodeStatus.NEW.value

    path = properties.property(
        ra_types.String(min_length=1, max_length=255),
        required=True,
    )
    status = properties.property(
        ra_types.Enum([s.value for s in pc.InstanceStatus]),
    )
    target = properties.property(
        types_dynamic.KindModelSelectorType(
            types_dynamic.KindModelType(NodeTarget),
        ),
        required=True,
    )
    body = properties.property(
        types_dynamic.KindModelSelectorType(
            types_dynamic.KindModelType(TextBodyConfig),
            types_dynamic.KindModelType(TemplateBodyConfig),
        ),
        required=True,
    )
    on_change = properties.property(
        types_dynamic.KindModelSelectorType(
            types_dynamic.KindModelType(OnChangeNoAction),
            types_dynamic.KindModelType(OnChangeShell),
        ),
        default=OnChangeNoAction,
    )
    mode = properties.property(ra_types.String(max_length=4), default="0600")
    owner = properties.property(
        ra_types.String(max_length=128),
        default="root",
    )
    group = properties.property(
        ra_types.String(max_length=128),
        default="root",
    )

    @classmethod
    def get_resource_kind(cls) -> str:
        """Return the resource kind."""
        return "config"

    def get_resource_target_fields(self) -> tp.Collection[str]:
        """Return the collection of target fields.

        Refer to the Resource model for more details about target fields.
        """
        return frozenset(
            (
                "uuid",
                "name",
                "path",
                "target",
                "body",
                "on_change",
                "mode",
                "owner",
                "group",
                "project_id",
            )
        )


class Profile(
    ua_models.TargetResourceKindAwareMixin,
    ra_models.ModelWithRequiredUUID,
    ra_models.ModelWithProject,
    ra_models.ModelWithNameDesc,
    ra_models.ModelWithTimestamp,
):
    """The model represents a profile in scope of ValuesStore.

    Profiles allow to adapt installation for various environments
    such as local development, stage, production.

    Example:
      $core.vs.profiles:
        custom:
          name: "custom"
          profile_type: "ELEMENT"
          project_id: "12345678-c625-4fee-81d5-f691897b8142"
          description: "The custom profile"
    """

    __init_resource_status__ = pc.AlwaysActiveStatus.ACTIVE.value

    profile_type = properties.property(
        ra_types.Enum(tuple(pt.value for pt in pc.ProfileType)),
        default=pc.ProfileType.ELEMENT.value,
    )
    active = properties.property(
        ra_types.Boolean(),
        default=False,
    )
    status = properties.property(
        ra_types.Enum(tuple(s.value for s in pc.AlwaysActiveStatus)),
        default=pc.AlwaysActiveStatus.ACTIVE.value,
    )

    @classmethod
    def get_resource_kind(cls) -> str:
        """Return the resource kind."""
        return "vs_profile"

    def get_resource_target_fields(self) -> tp.Collection[str]:
        """Return the collection of target fields.

        Refer to the Resource model for more details about target fields.
        """
        return frozenset(
            (
                "uuid",
                "name",
                "profile_type",
                "active",
                "project_id",
            )
        )


class AbstractVariableSetter(
    ra_models.SimpleViewMixin,
    types_dynamic.AbstractKindModel,
):
    """The abstract model for variable setter."""

    def set_value(self, variable: "Variable") -> None:
        """Determine a value for the variable and set it.

        If the value cannot be determined, the method raises an exception.
        """
        raise infra_exc.VariableCannotFindValue(variable=variable.uuid)


class ProfileVariableSetterItem(common_types.SchematicType):
    __scheme__ = {
        "profile": ra_types.UUID(),
        "value": ra_types.AnySimpleType(),
    }
    __mandatory__ = {"profile", "value"}


class ProfileVariableSetter(AbstractVariableSetter):
    """The setter based on profiles.

    Example:
    $core.vs.variables:
      var_profile:
        name: "var_profile"
        project_id: "12345678-c625-4fee-81d5-f691897b8142"
        setter:
          kind: profile
          fallback_strategy: ignore
          profiles:
            - profile: "$core.vs.profiles.$default:uuid"
              value: 1
            - profile: "$core.vs.profiles.$develop:uuid"
              value: 1
            - profile: "$core.vs.profiles.$medium:uuid"
              value: 2
            - profile: "$core.vs.profiles.$custom:uuid"
              value: 3
    """

    KIND = "profile"

    fallback_strategy = properties.property(
        ra_types.Enum(("ignore",)),
        default="ignore",
    )
    profiles = properties.property(
        ra_types.TypedList(ProfileVariableSetterItem()), default=list
    )
    element = properties.property(
        ra_types.AllowNone(ra_types.UUID()),
        default=None,
    )


class SelectorVariableSetter(AbstractVariableSetter):
    """The selector setter allowing to select a value for the variable.

    Example:
    $core.vs.variables:
      var_profile:
        name: "var_profile"
        project_id: "12345678-c625-4fee-81d5-f691897b8142"
        setter:
          kind: selector
          selector_strategy: latest
    """

    KIND = "selector"

    selector_strategy = properties.property(
        ra_types.Enum(("latest",)),
        default="latest",
    )


class Variable(
    ua_models.TargetResourceKindAwareMixin,
    ra_models.ModelWithRequiredUUID,
    ra_models.ModelWithProject,
    ra_models.ModelWithNameDesc,
    ra_models.ModelWithTimestamp,
):
    """The model represents a variable in scope of ValuesStore.

    Variables are the entities with internal logic to calculate
    value based on profiles or values.

    Examples:

    $core.vs.variables:
      var_profile:
        name: "var_profile"
        project_id: "12345678-c625-4fee-81d5-f691897b8142"
        setter:
          kind: profile
          fallback_strategy: ignore
          profiles:
            - name: $core.vs.profiles.develop:uuid
              value: 1
            - name: $core.vs.profiles.medium:uuid
              value: 2
            - name: $core.vs.profiles.custom:uuid
              value: 3
      var_selector:
        name: "var_selector"
        project_id: "12345678-c625-4fee-81d5-f691897b8142"
        setter:
          kind: selector
          selector_strategy: latest
    """

    __init_resource_status__ = pc.VariableStatus.NEW.value

    setter = properties.property(
        types_dynamic.KindModelSelectorType(
            types_dynamic.KindModelType(ProfileVariableSetter),
            types_dynamic.KindModelType(SelectorVariableSetter),
        ),
        required=True,
    )
    status = properties.property(
        ra_types.Enum(tuple(s.value for s in pc.VariableStatus)),
        default=pc.VariableStatus.NEW.value,
    )
    value = properties.property(
        ra_types.AllowNone(ra_types.AnySimpleType()),
        default=None,
    )

    @classmethod
    def get_resource_kind(cls) -> str:
        """Return the resource kind."""
        return "vs_variable"

    def get_resource_target_fields(self) -> tp.Collection[str]:
        """Return the collection of target fields.

        Refer to the Resource model for more details about target fields.
        """
        return frozenset(
            (
                "uuid",
                "name",
                "setter",
                "project_id",
            )
        )


class Value(
    ua_models.TargetResourceKindAwareMixin,
    ra_models.ModelWithRequiredUUID,
    ra_models.ModelWithProject,
    ra_models.ModelWithNameDesc,
    ra_models.ModelWithTimestamp,
):
    """The model represents a value in scope of ValuesStore.

    It's a value of a simple type: int, float, list, dict, bool, str.
    Manifest example:

    $core.vs.values:
      int_value:
        name: "int value"
        project_id: "12345678-c625-4fee-81d5-f691897b8142"
        value: 1
    """

    __init_resource_status__ = pc.AlwaysActiveStatus.ACTIVE.value

    value = properties.property(
        ra_types.AllowNone(ra_types.AnySimpleType()),
        default=None,
    )
    read_only = properties.property(
        ra_types.Boolean(),
        default=False,
    )
    variable = relationships.relationship(Variable)
    manual_selected = properties.property(
        ra_types.Boolean(),
        default=False,
    )
    status = properties.property(
        ra_types.Enum(tuple(s.value for s in pc.AlwaysActiveStatus)),
        default=pc.AlwaysActiveStatus.ACTIVE.value,
    )

    @classmethod
    def get_resource_kind(cls) -> str:
        """Return the resource kind."""
        return "vs_value"

    def get_resource_target_fields(self) -> tp.Collection[str]:
        """Return the collection of target fields.

        Refer to the Resource model for more details about target fields.
        """
        return frozenset(
            (
                "uuid",
                "name",
                "value",
                "read_only",
                "manual_selected",
                "project_id",
            )
        )
