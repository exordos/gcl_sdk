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

from gcl_sdk.agents.universal.dm import models as ua_models
from gcl_sdk.agents.universal.services import builder

LOG = logging.getLogger(__name__)


class PaaSResourcePair(tp.NamedTuple):
    target: ua_models.TargetResourceKindAwareMixin
    actual: ua_models.TargetResourceKindAwareMixin | None


class PaaSCollection(tp.NamedTuple):
    paas_objects: tp.Collection[PaaSResourcePair]

    def targets(self) -> tp.Collection[ua_models.TargetResourceKindAwareMixin]:
        return tuple(p.target for p in self.paas_objects)

    def actuals(self) -> tp.Collection[ua_models.TargetResourceKindAwareMixin]:
        return tuple(p.actual for p in self.paas_objects)


class PaaSBuilder(builder.UniversalBuilderService):
    def create_paas_objects(
        self, instance: ua_models.InstanceWithDerivativesMixin
    ) -> tp.Collection[ua_models.TargetResourceKindAwareMixin]:
        """Create a list of PaaS objects.

        The method returns a list of PaaS objects that are required
        for the instance.
        """
        raise NotImplementedError

    def actualize_paas_objects(
        self,
        instance: ua_models.InstanceWithDerivativesMixin,
        paas_collection: PaaSCollection,
    ) -> tp.Collection[ua_models.TargetResourceKindAwareMixin]:
        """Update a list of PaaS objects.

        The method processes existing resources, actualize them
        if needed, creates new objects if needed, and returns only actual
        and updated PaaS objects for the instance.

        Basic implementation ignores existing objects and just recreates them.
        """
        return self.create_paas_objects(instance)

    def schedule_paas_objects(
        self,
        instance: ua_models.InstanceWithDerivativesMixin,
        paas_objects: tp.Collection[ua_models.TargetResourceKindAwareMixin],
    ) -> dict[sys_uuid.UUID, tp.Collection[ua_models.TargetResourceKindAwareMixin]]:
        """Schedule the PaaS objects.

        The method schedules the PaaS objects. The result is a dictionary
        where the key is a UUID of a agent and the value is a list of PaaS
        objects that should be scheduled on this agent.
        """
        return {}

    def enable_schedule_paas_objects(self) -> bool:
        """Enable schedule PaaS objects."""
        return True

    def actualize_paas_objects_source_data_plane(
        self,
        instance: ua_models.InstanceWithDerivativesMixin,
        paas_collection: PaaSCollection,
    ) -> tp.Collection[ua_models.TargetResourceKindAwareMixin]:
        """Actualize the PaaS objects. Changes from the data plane.

        The method is called when the instance is outdated. For example,
        the instance `Database` has derivative `PGDatabase`. Single `Database`
        may have multiple `PGDatabase` derivatives. If any of the derivatives
        is outdated, this method is called to reactualize this PaaS objects.

        Args:
            instance: The instance to actualize.
            paas_objects: The actual PaaS objects.
        """
        return self.actualize_paas_objects(instance, paas_collection)

    def actualize_paas_objects_source_master(
        self,
        instance: ua_models.InstanceWithDerivativesMixin,
        master_instance: ua_models.InstanceWithDerivativesMixin,
        paas_collection: PaaSCollection,
    ) -> tp.Collection[ua_models.TargetResourceKindAwareMixin]:
        """Actualize the PaaS objects. Changes from the master instance.

        The method is called when the instance is outdated from master
        instance point of view. For example, the instance `Database` is linked to the
        `NodeSet` instance. If the `NodeSet` is outdated, this method is called
        to reactualize the `Database` instance.

        Args:
            instance: The instance to actualize.
            master_instance: The master instance.
            paas_collection: The actual PaaS objects.
        """
        return paas_collection.targets()

    def create_instance_derivatives(
        self, instance: ua_models.InstanceWithDerivativesMixin
    ) -> tp.Collection[ua_models.TargetResourceKindAwareMixin]:
        """Create the instance.

        The result is an iterable object of derivative objects that are
        required for the instance. For example, the main instance is a
        `Config` so the derivative objects for the the config is a list
        of `Render`. The result is a tuple/list/set/... of render objects.
        The derivative objects should inherit from the `TargetResourceMixin`.
        """
        return self.create_paas_objects(instance)

    def update_instance_derivatives(
        self,
        instance: ua_models.InstanceWithDerivativesMixin,
        resource: ua_models.TargetResourceKindAwareMixin,
        derivative_pairs: tp.Collection[
            tuple[
                ua_models.TargetResourceKindAwareMixin,  # The target resource
                ua_models.TargetResourceKindAwareMixin | None,  # The actual resource
            ]
        ],
    ) -> tp.Collection[ua_models.TargetResourceKindAwareMixin]:
        """The hook to update instance derivatives.

        The hook is called when an initiator of updating is an user or
        software from control plane side.
        The default behavior is to send the same list as on instance creation.
        """
        paas_collection = PaaSCollection(
            paas_objects=tuple(PaaSResourcePair(*p) for p in derivative_pairs),
        )

        return self.actualize_paas_objects(instance, paas_collection)

    def actualize_outdated_instance_derivatives(
        self,
        instance: ua_models.InstanceWithDerivativesMixin,
        derivative_pairs: tp.Collection[
            tuple[
                ua_models.TargetResourceKindAwareMixin,  # The target resource
                ua_models.TargetResourceKindAwareMixin | None,  # The actual resource
            ]
        ],
    ) -> tp.Collection[ua_models.TargetResourceKindAwareMixin]:
        """Actualize outdated instance with derivatives.

        It means some changes occurred on the data plane and the instance
        is outdated now. For example, the instance `Config` has derivative
        `Render`. Single `Config` may have multiple `Render` derivatives.
        If any of the derivatives is outdated, this method is called to
        reactualize the derivatives. The method returns the list of `updated`
        derivatives. If nothing needs to be updated, the method returns the
        same list of target derivatives as it received. Otherwise, the method
        should return the list of updated derivatives. It also can add new or
        remove old derivatives.
        Depends on the `fetch_all_derivatives_on_outdate` the behavior of the
        method is different:

        fetch_all_derivatives_on_outdate == True:
        The method receives the list of all derivatives currently available
        for the instance even though the derivatives aren't outdated.

        fetch_all_derivatives_on_outdate == False:
        The method receives the list only changed derivatives from the last
        actualization. For example, a config has two renders. Only one of
        them is outdated. The method receives the list of only one outdated
        render.

        Args:
            instance: The instance to actualize.
            derivative_pairs: Changed or all derivatives of the instance.
        """
        paas_collection = PaaSCollection(
            paas_objects=tuple(PaaSResourcePair(*p) for p in derivative_pairs),
        )

        return self.actualize_paas_objects_source_data_plane(instance, paas_collection)

    def track_outdated_master_full_hash_instances(self) -> bool:
        """Track outdated master full hash instances."""
        return True

    def actualize_outdated_master_full_hash_instance(
        self,
        instance: ua_models.InstanceWithDerivativesMixin,
        master_instance: ua_models.InstanceWithDerivativesMixin,
        derivatives: tp.Collection[
            tuple[
                ua_models.TargetResourceKindAwareMixin,  # The target resource
                ua_models.TargetResourceKindAwareMixin | None,  # The actual resource
            ]
        ],
    ) -> tp.Collection[ua_models.TargetResourceKindAwareMixin]:
        """Actualize outdated master full hash instance.

        The logic is quite similar to `actualize_outdated_instance_derivatives`.
        But the reason when this method is called is different. The
        `actualize_outdated_instance_derivatives` allows to track changes on the
        data plane but this method allows to track changes on a related master
        instance. For example, the instance model is `Database`, the related master
        for this instance is `NodeSet`. If the `NodeSet` is updated, this method
        is called for all `Database` instances that are related to this `NodeSet`
        to reactualize them.
        This method tracks changes for all fields of the master instance.

        Args:
            instance: The instance to actualize.
            master_instance: The master instance.
            derivatives: All derivatives of the instance.
        """
        paas_collection = PaaSCollection(
            paas_objects=tuple(PaaSResourcePair(*p) for p in derivatives),
        )

        return self.actualize_paas_objects_source_master(
            instance, master_instance, paas_collection
        )

    def post_create_instance_resource(
        self,
        instance: (ua_models.InstanceMixin | ua_models.InstanceWithDerivativesMixin),
        resource: ua_models.TargetResource,
        derivatives: tp.Collection[ua_models.TargetResource] = (),
    ) -> None:
        """The hook is performed after saving instance resource.

        The hook is called only for new instances.
        """
        super().post_create_instance_resource(instance, resource, derivatives)

        if not self.enable_schedule_paas_objects():
            return

        # Prepare PaaS objects for scheduling
        paas_objects = []
        for derivative in derivatives:
            class_ = self._instance_model.derivative_model(derivative.kind)
            paas_objects.append(class_.from_ua_resource(derivative))

        schedule_map = self.schedule_paas_objects(
            instance,
            paas_objects,
        )

        # Schedule PaaS objects
        # We don't expect to have a lot of derivatives.
        # So it's not a big problem to have nested loop here
        for agent_uuid, paas_objects in schedule_map.items():
            for paas_object in paas_objects:
                for derivative in derivatives:
                    if derivative.uuid == paas_object.uuid:
                        derivative.agent = agent_uuid

    def post_update_instance_resource(
        self,
        instance: ua_models.InstanceMixin,
        resource: ua_models.TargetResource,
        derivatives: tp.Collection[ua_models.TargetResource] = (),
    ) -> None:
        """The hook is performed after updating instance resource."""
        super().post_update_instance_resource(instance, resource, derivatives)

        if not self.enable_schedule_paas_objects():
            return

        # Choose paas objects to schedule
        paas_objects = []
        for derivative in derivatives:
            if derivative.agent is not None:
                continue
            class_ = self._instance_model.derivative_model(derivative.kind)
            paas_objects.append(class_.from_ua_resource(derivative))

        if not paas_objects:
            return

        schedule_map = self.schedule_paas_objects(
            instance,
            paas_objects,
        )

        # Schedule PaaS objects
        # We don't expect to have a lot of derivatives.
        # So it's not a big problem to have nested loop here
        for agent_uuid, paas_objects in schedule_map.items():
            for paas_object in paas_objects:
                for derivative in derivatives:
                    if derivative.uuid == paas_object.uuid:
                        derivative.agent = agent_uuid
                        derivative.update()
