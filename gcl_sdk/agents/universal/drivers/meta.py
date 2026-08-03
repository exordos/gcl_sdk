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

import logging
import typing as tp
import uuid as sys_uuid

from restalchemy.dm import models as ra_models
from restalchemy.dm import properties
from restalchemy.dm import types

from gcl_sdk.agents.universal.dm import models
from gcl_sdk.agents.universal.drivers import base
from gcl_sdk.agents.universal.drivers import exceptions as driver_exc
from gcl_sdk.agents.universal.storage import common as storage_common

LOG = logging.getLogger(__name__)


class MetaDataPlaneModel(ra_models.ModelWithRequiredUUID, models.ResourceMixin):
    """A base model for using in MetaFileStorageAgentDriver.

    Child models should implement methods to work with data plane.
    """

    # Store the resource target fields
    target_fields = properties.property(types.TypedList(types.String()), default=list)

    @classmethod
    def from_ua_resource(cls, resource: models.Resource) -> MetaDataPlaneModel:
        """Build the model from what the control plane sent.

        A name this model does not have is skipped rather than fatal. It
        means an agent older than the core it talks to, which is an
        ordinary state during an upgrade and not a reason for every
        resource of the capability to fail to deserialize -- as it did:
        one field the control plane still sent and the data plane had
        dropped took the whole capability down, and the traceback named
        `get_custom_property_type` rather than the field.

        Loud, though, and for a reason this cannot fix: that field is part
        of the *target* hash the control plane computed, and nothing here
        can put it back. So the resource is applied as far as it is
        understood and then keeps being applied, never settling, until the
        two sides agree on the fields again. A warning naming the field is
        the difference between diagnosing that in a minute and in a day.
        """
        # One pass: the names have to be split anyway, because an unknown one
        # must stay out of `target_fields` as well as out of the model. The
        # warning is the rare half of the same split, so it costs nothing to
        # collect and is only sorted when there is something to say.
        known = set(cls.properties.properties)
        target_fields = []
        unknown = []
        for name in resource.value:
            if name in known:
                target_fields.append(name)
            else:
                unknown.append(name)
        if unknown:
            LOG.warning(
                "%s resource %s carries %s, which this agent does not know. "
                "Applying the rest; the resource will not settle until the "
                "control plane and this agent agree on its fields.",
                resource.kind,
                resource.uuid,
                ", ".join(sorted(unknown)),
            )
        return cls.restore_from_simple_view(
            skip_unknown_fields=True, target_fields=target_fields, **resource.value
        )

    def get_resource_target_fields(self) -> list[str]:
        """Return the list of target fields.

        Refer to the Resource model for more details about target fields.
        """
        return self.target_fields

    def get_resource_ignore_fields(self) -> list[str]:
        """Return fields that should not belong to the resource."""
        return ["target_fields"]

    def get_meta_fields(self) -> set[str] | None:
        """Return a list of meta fields or None.

        Similar to `get_meta_model_fields` but adds addtional service fields.
        """
        fields = self.get_meta_model_fields()
        if fields is None:
            return None
        fields.add("target_fields")
        return fields

    def get_meta_model_fields(self) -> set[str] | None:
        """Return a list of meta fields or None.

        Meta fields are the fields that cannot be fetched from
        the data plane or we just want to save them into the meta file.

        `None` means all fields are meta fields but it doesn't mean they
        won't be updated from the data plane.
        """
        return None

    def dump_to_dp(self) -> None:
        """Save the resource to the data plane."""

    def restore_from_dp(self) -> None:
        """Load the resource from the data plane."""

    def delete_from_dp(self) -> None:
        """Delete the resource from the data plane."""

    def update_on_dp(self) -> None:
        """Update the resource on the data plane."""

    @classmethod
    def start(cls) -> None:
        """Start the meta model for iteration.

        It's called before the iteration starts.
        """

    @classmethod
    def finalize(cls) -> None:
        """Finalize the meta model for iteration.

        It's called after the iteration ends.
        """


class MetaCoordinatorDataPlaneModel(MetaDataPlaneModel):
    """Meta coordinator data plane model.

    The class is similar to MetaDataPlaneModel but it is used for
    coordinator models.
    """

    def dump_to_dp(self, **kwargs) -> None:
        """Save the resource to the data plane."""

    def restore_from_dp(self, **kwargs) -> None:
        """Load the resource from the data plane."""

    def delete_from_dp(self, **kwargs) -> None:
        """Delete the resource from the data plane."""

    def update_on_dp(self, **kwargs) -> None:
        """Update the resource on the data plane."""


class MetaFileStorageAgentDriver(base.AbstractCapabilityDriver):
    """Meta driver. Handles models that partly are placed into the metafile.

    It's not possible to get all necessary information from the data plane
    pretty often. For instance, configuration files. There are hundreds or
    thousands of them in the system. How to know which files should be handled
    by the driver? A `meta file` may be used for this purpose. This file
    contains some meta information such path, uuid and so on but this file
    does not contain the information that can be fetched from the data plane.

    Particular models should be derived from `MetaDataPlaneModel` to work
    properly with the driver.
    """

    __model_map__ = {"model": MetaDataPlaneModel}

    def __init__(self, *args, meta_file: str, **kwargs):
        super().__init__()
        self._meta_file = meta_file
        self._storage = storage_common.JsonFileStorageSingleton.get_instance(
            self._meta_file
        )

        for cap_name, cap_model in self.__model_map__.items():
            # Check the model map is in the correct format
            if not issubclass(cap_model, MetaDataPlaneModel):
                raise TypeError(f"Model {cap_model} is not a MetaDataPlaneModel")

            if (
                cap_name not in self._storage
                # Reset storage with new format
                or "resources" not in self._storage.get(cap_name, {})
            ):
                self._storage[cap_name] = {"resources": {}}

    def _load_from_meta(self, capability: str) -> list[MetaDataPlaneModel]:
        """Load the resources from the meta file.

        It loads only the `meta` part that cannot be fetched from the data plane
        or lightweight models.
        """
        capstor = self._storage[capability]["resources"]

        cap_model = self.__model_map__[capability]
        return [cap_model.restore_from_simple_view(**r) for r in capstor.values()]

    def _delete_from_meta(self, kind: str, uuid: sys_uuid.UUID) -> None:
        """Remove the resource from the meta file.

        It's safe to call this method even if the resource is already
        missing from the meta storage (e.g. duplicate delete calls).
        """

        uuid = str(uuid)

        if self._storage[kind]["resources"].pop(uuid, None) is None:
            LOG.warning("Missing meta storage item for %s(%s)", uuid, kind)
        else:
            LOG.debug("Deleted meta resource %s", uuid)

    def _add_to_meta(self, capability: str, meta_object: MetaDataPlaneModel) -> None:
        """Add the resource from the meta file."""

        view = meta_object.dump_to_simple_view()

        # Save only meta fields
        # TODO(akremenetsky): Handle an empty meta fields list.
        # Actually it means no fields are saved.
        if meta_fields := meta_object.get_meta_fields():
            for k in tuple(view.keys()):
                if k not in meta_fields:
                    view.pop(k)

        self._storage[capability]["resources"][str(meta_object.uuid)] = view
        LOG.debug("Saved meta resource: %s", view)

    def get_capabilities(self) -> list[str]:
        """Returns a list of capabilities supported by the driver."""
        return list(self.__model_map__.keys())

    def get(self, resource: models.Resource) -> models.Resource:
        """Find and return a resource by uuid and kind.

        It returns the resource from the data plane.
        """
        if resource.kind not in self.__model_map__:
            raise TypeError(f"The resource is not {self.__model_map__.keys()}")

        # Check the resource exists
        for r in self._load_from_meta(resource.kind):
            # Find the corresponding resource
            if r.uuid == resource.uuid:
                meta_resource = r
                break
        else:
            raise driver_exc.ResourceNotFound(resource=resource)

        meta_resource.restore_from_dp()
        return meta_resource.to_ua_resource(resource.kind)

    def list(self, capability: str) -> list[models.Resource]:
        """Lists all resources by capability."""
        if capability not in self.__model_map__:
            raise TypeError(f"The resource is not {self.__model_map__.keys()}")

        dp_objects = []
        for obj in self._load_from_meta(capability):
            try:
                obj.restore_from_dp()
                dp_objects.append(obj)
            except driver_exc.InvalidDataPlaneObjectError:
                # TODO(akremenetsky): Seems some destructive actions happened
                # on the data plane. It's not clear what happened but the
                # object is invalid. We need to save this information in the
                # audit journal.

                LOG.error(
                    "Invalid data plane object: %s. It will be recreated.",
                    obj.uuid,
                )
            except driver_exc.ResourceNotFound:
                LOG.error("Resource %s not found on the data plane", obj.uuid)

        return [obj.to_ua_resource(capability) for obj in dp_objects]

    def create(self, resource: models.Resource) -> models.Resource:
        """Creates a resource."""
        if resource.kind not in self.__model_map__:
            raise TypeError(f"The resource is not {self.__model_map__.keys()}")

        # Check the resource does not exist
        try:
            self.get(resource)
        except driver_exc.InvalidDataPlaneObjectError:
            # TODO(akremenetsky): Seems some destructive actions happened
            # on the data plane. It's not clear what happened but the
            # object is invalid. We need to save this information in the
            # audit journal.

            LOG.error(
                "Invalid data plane object: %s. It will be recreated.",
                resource.uuid,
            )
        except driver_exc.ResourceNotFound:
            # Desirable behavior, the resource should not exist
            pass
        else:
            raise driver_exc.ResourceAlreadyExists(resource=resource)

        # Restore object from the resource
        cap_model = self.__model_map__[resource.kind]

        # The object is lightweight since it is not stored in the data plane
        meta_obj = cap_model.from_ua_resource(resource)

        meta_obj.dump_to_dp()
        self._add_to_meta(resource.kind, meta_obj)
        new_resource = meta_obj.to_ua_resource(resource.kind)
        LOG.debug("Created %s resource: %s", new_resource.kind, new_resource.uuid)
        return new_resource

    def update(self, resource: models.Resource) -> models.Resource:
        """Update the resource.

        The simplest implementation. Updating through recreating the resource.
        """
        if resource.kind not in self.__model_map__:
            raise TypeError(f"The resource is not {self.__model_map__.keys()}")

        for meta_obj in self._load_from_meta(resource.kind):
            if meta_obj.uuid == resource.uuid:
                break
        else:
            raise driver_exc.ResourceNotFound(resource=resource)

        meta_obj = self.__model_map__[resource.kind].from_ua_resource(resource)
        meta_obj.update_on_dp()

        # The simplest implementation, just recreate.
        self._delete_from_meta(resource.kind, resource.uuid)
        self._add_to_meta(resource.kind, meta_obj)

        new_resource = meta_obj.to_ua_resource(resource.kind)
        LOG.debug("Updated %s resource: %s", new_resource.kind, new_resource.uuid)
        return new_resource

    def delete(self, resource: models.Resource) -> None:
        """Delete the resource."""
        if resource.kind not in self.__model_map__:
            raise TypeError(f"The resource is not {self.__model_map__.keys()}")

        try:
            self.get(resource)
        except driver_exc.ResourceNotFound:
            # Nothing to do, the resource does not exist either in the
            # meta storage or on the data plane (or both). Proceed anyway:
            # `delete_from_dp`/`_delete_from_meta` are expected to be
            # safe no-ops in that case, so the resource is guaranteed to
            # be gone after this call returns.
            pass
        except driver_exc.InvalidDataPlaneObjectError:
            # TODO(akremenetsky): Seems some destructive actions happened
            # on the data plane. It's not clear what happened but the
            # object is invalid. We need to save this information in the
            # audit journal.
            LOG.error(
                "Invalid data plane object: %s. It will be removed anyway.",
                resource.uuid,
            )

        # Restore object from the resource
        cap_model = self.__model_map__[resource.kind]
        meta_obj = cap_model.from_ua_resource(resource)

        meta_obj.delete_from_dp()
        self._delete_from_meta(resource.kind, resource.uuid)
        LOG.debug("Deleted %s resource: %s", resource.kind, resource.uuid)

    def finalize(self) -> None:
        """Perform some finalization after finishing all operations.

        This method is called once after all other methods like list,
        create, update, delete are called. It can be used to do some
        finalization or cleanups like closing connections, files, etc.

        The driver iteration:
            start -> list -> [create | update | delete]* -> finalize
        """
        self._storage.persist()


class MetaCoordinatorAgentDriver(MetaFileStorageAgentDriver):
    """Meta driver. Handles models that partly are placed into the metafile.

    Similar to the `MetaFileStorageAgentDriver` but the
    `MetaFileStorageAgentDriver` focuses on the independed models while
    the `MetaCoordinatorAgentDriver` focuses on the models that are related
    to each other in some way. To handle relations among models the
    `MetaCoordinatorAgentDriver` uses the `__coordinator_map__` attribute.
    It's a dictionary with simple syntax to describe relations.
    """

    __model_map__ = {"model": MetaCoordinatorDataPlaneModel}

    # The coordinator map is a dictionary allowing to describe relations
    # between models. For example:
    # __coordinator_map__ = {
    #     "pool": {},
    #     "pool_volume": {
    #         "pool": {
    #             "kind": "pool",
    #             "relation": "pool_volume:pool",
    #         },
    #     },
    #     "pool_machine": {
    #         "pool": {
    #             "kind": "pool",
    #             "relation": "pool_machine:pool",
    #         },
    #         "volumes": {
    #             "kind": "pool_volume",
    #             "relation": "pool_volume:machine",
    #         },
    #     },
    # }
    # The first level keys are the model kinds. The value is a dictionary
    # with necessary relations. This relation will be passed to
    # `MetaCoordinatorDataPlaneModel`'s corresponding methods. For instance,
    # The `pool` require no relations and nothing to be passed to
    # `restore_from_dp`, `dump_to_dp` and other methods. The `pool_volume`
    # require the `pool` relation. The dependency type(kind) is `pool`.
    # So we need to pass `pool` object into  `restore_from_dp`, `dump_to_dp`
    # and other method. The second step is to detect which pool has to be
    # passed. For this we use the field `pool` from the `pool_volume` model
    # and match it with available pools objects. The available pools objects
    # are pools fetched earlier for the `pool` kind. As result the signature
    # of `restore_from_dp` for `pool_volume` is:
    # `def restore_from_dp(self, pool: Pool) -> None:`
    __coordinator_map__ = {}

    def __init__(self, *args, meta_file: str, **kwargs):
        super().__init__(*args, meta_file=meta_file, **kwargs)
        self._coordinator_storage = {}

    def _get_dependencies(
        self,
        kind: str,
        requirements: dict[str, tp.Any],
        target: MetaDataPlaneModel,
    ) -> dict[str, tp.Any]:
        """Prepare dependencies for the resource."""
        dependencies = {}

        # Nothing to do if there are no requirements
        if not requirements:
            return dependencies

        # Prepare paramters for the target object
        for param_name, spec in requirements.items():
            all_objects = self._coordinator_storage[spec["kind"]]

            # Apply relations if they are
            # Example of a relations:
            # "pool": {
            #     "kind": "pool",
            #     "relation": "machine:pool",
            # },
            # It means the object with kind `machine` has a field `pool`
            # that points to the object with kind `pool`.
            if relation := spec.get("relation"):
                relation_kind, relation_attr = relation.split(":")

                # A pointer to itselt
                if relation_kind == kind:
                    relation_uuid = getattr(target, relation_attr)

                    # The objects are stored by UUID
                    deps_objects = all_objects.get(relation_uuid)
                else:
                    deps_objects = tuple(
                        obj
                        for obj in all_objects.values()
                        if getattr(obj, relation_attr) == target.uuid
                    )
            else:
                # TODO(akremenetsky): This part requires optimization in the future
                # Copy all objects
                deps_objects = tuple(all_objects.values())

            dependencies[param_name] = deps_objects

        return dependencies

    def get(self, resource: models.Resource) -> models.Resource:
        """Find and return a resource by uuid and kind.

        It returns the resource from the data plane.
        """
        if resource.kind not in self.__model_map__:
            raise TypeError(f"The resource is not {self.__model_map__.keys()}")

        requirements = self.__coordinator_map__.get(resource.kind)

        # Check the resource exists
        for r in self._load_from_meta(resource.kind):
            # Find the corresponding resource
            if r.uuid == resource.uuid:
                meta_obj = r
                break
        else:
            raise driver_exc.ResourceNotFound(resource=resource)

        # Detect which related entities pass for restoration
        deps = self._get_dependencies(resource.kind, requirements, meta_obj)

        meta_obj.restore_from_dp(**deps)

        # Also save the object in the coordinator storage to resolve
        # dependencies for other kinds.
        self._coordinator_storage[resource.kind][meta_obj.uuid] = meta_obj
        return meta_obj.to_ua_resource(resource.kind)

    def list(self, capability: str) -> list[models.Resource]:
        """Lists all resources by capability."""
        if capability not in self.__model_map__:
            raise TypeError(f"The resource is not {self.__model_map__.keys()}")

        requirements = self.__coordinator_map__.get(capability)

        dp_objects = []
        for obj in self._load_from_meta(capability):
            # Detect which related entities pass for restoration
            deps = self._get_dependencies(capability, requirements, obj)
            try:
                obj.restore_from_dp(**deps)
                dp_objects.append(obj)

                # Also save the object in the coordinator storage to resolve
                # dependencies for other kinds.
                self._coordinator_storage[capability][obj.uuid] = obj
            except driver_exc.ResourceNotFound:
                LOG.error("Resource %s not found on the data plane", obj.uuid)

        return [obj.to_ua_resource(capability) for obj in dp_objects]

    def create(self, resource: models.Resource) -> models.Resource:
        """Creates a resource."""
        if resource.kind not in self.__model_map__:
            raise TypeError(f"The resource is not {self.__model_map__.keys()}")

        requirements = self.__coordinator_map__.get(resource.kind)

        # Check the resource does not exist
        try:
            self.get(resource)
        except driver_exc.ResourceNotFound:
            # Desirable behavior, the resource should not exist
            pass
        else:
            raise driver_exc.ResourceAlreadyExists(resource=resource)

        # Restore object from the resource
        cap_model = self.__model_map__[resource.kind]

        # The object is lightweight since it is not stored in the data plane
        meta_obj = cap_model.from_ua_resource(resource)

        # Detect which related entities pass for restoration
        deps = self._get_dependencies(resource.kind, requirements, meta_obj)

        meta_obj.dump_to_dp(**deps)
        self._add_to_meta(resource.kind, meta_obj)

        # Also save the object in the coordinator storage to resolve
        # dependencies for other kinds.
        self._coordinator_storage[resource.kind][meta_obj.uuid] = meta_obj

        LOG.debug("Created resource: %s", meta_obj.uuid)
        return meta_obj.to_ua_resource(resource.kind)

    def update(self, resource: models.Resource) -> models.Resource:
        """Update the resource.

        The simplest implementation. Updating through recreating the resource.
        """
        if resource.kind not in self.__model_map__:
            raise TypeError(f"The resource is not {self.__model_map__.keys()}")

        requirements = self.__coordinator_map__.get(resource.kind)

        for meta_obj in self._load_from_meta(resource.kind):
            if meta_obj.uuid == resource.uuid:
                break
        else:
            raise driver_exc.ResourceNotFound(resource=resource)

        meta_obj = self.__model_map__[resource.kind].from_ua_resource(resource)

        # Detect which related entities pass for restoration
        deps = self._get_dependencies(resource.kind, requirements, meta_obj)

        meta_obj.update_on_dp(**deps)

        # The simplest implementation, just recreate.
        self._delete_from_meta(resource.kind, resource.uuid)
        self._add_to_meta(resource.kind, meta_obj)

        new_resource = meta_obj.to_ua_resource(resource.kind)
        LOG.debug("Updated resource: %s", new_resource.uuid)

        # Also save the object in the coordinator storage to resolve
        # dependencies for other kinds.
        self._coordinator_storage[resource.kind][meta_obj.uuid] = meta_obj

        return new_resource

    def delete(self, resource: models.Resource) -> None:
        """Delete the resource."""
        if resource.kind not in self.__model_map__:
            raise TypeError(f"The resource is not {self.__model_map__.keys()}")

        requirements = self.__coordinator_map__.get(resource.kind)

        try:
            self.get(resource)
        except driver_exc.ResourceNotFound:
            # Nothing to do, the resource does not exist either in the
            # meta storage or on the data plane (or both). Proceed anyway:
            # `delete_from_dp`/`_delete_from_meta` are expected to be
            # safe no-ops in that case, so the resource is guaranteed to
            # be gone after this call returns.
            pass
        except driver_exc.InvalidDataPlaneObjectError:
            # TODO(akremenetsky): Seems some destructive actions happened
            # on the data plane. It's not clear what happened but the
            # object is invalid. We need to save this information in the
            # audit journal.
            LOG.error(
                "Invalid data plane object: %s. It will be removed anyway.",
                resource.uuid,
            )

        # Restore object from the resource
        cap_model = self.__model_map__[resource.kind]
        meta_obj = cap_model.from_ua_resource(resource)

        # Detect which related entities pass for restoration
        deps = self._get_dependencies(resource.kind, requirements, meta_obj)

        meta_obj.delete_from_dp(**deps)
        self._delete_from_meta(resource.kind, resource.uuid)

        # Also remove the object from the coordinator storage
        try:
            del self._coordinator_storage[resource.kind][meta_obj.uuid]
        except KeyError:
            LOG.warning(
                "Resource %s(%s) not found in the coordinator storage",
                resource.uuid,
                resource.kind,
            )
        else:
            LOG.debug("Deleted resource: %s(%s)", resource.uuid, resource.kind)

    def start(self) -> None:
        self._coordinator_storage = {kind: {} for kind in self.__model_map__}

    def finalize(self) -> None:
        """Perform some finalization after finishing all operations.

        This method is called once after all other methods like list,
        create, update, delete are called. It can be used to do some
        finalization or cleanups like closing connections, files, etc.

        The driver iteration:
            start -> list -> [create | update | delete]* -> finalize
        """
        self._storage.persist()
