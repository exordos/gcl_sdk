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

import itertools
import logging
import typing as tp
import uuid as sys_uuid

from gcl_looper.services import basic as looper_basic

from gcl_sdk.agents.universal import constants as c
from gcl_sdk.agents.universal import utils
from gcl_sdk.agents.universal.clients.orch import base as orch_base
from gcl_sdk.agents.universal.clients.orch import exceptions as orch_exc
from gcl_sdk.agents.universal.dm import models
from gcl_sdk.agents.universal.drivers import base as driver_base
from gcl_sdk.agents.universal.drivers import exceptions as driver_exc

LOG = logging.getLogger(__name__)


class UniversalAgentService(looper_basic.BasicService):
    def __init__(
        self,
        agent_uuid: sys_uuid.UUID,
        orch_client: orch_base.AbstractOrchClient,
        caps_drivers: list[driver_base.AbstractCapabilityDriver],
        facts_drivers: list[driver_base.AbstractFactDriver],
        payload_path: str | None = c.PAYLOAD_PATH,
        iter_min_period: float = 3,
        iter_pause: float = 0.1,
        system_uuid: sys_uuid.UUID | None = None,
        verify_node_on_register: bool = True,
    ):
        super().__init__(iter_min_period, iter_pause)
        self._orch_client = orch_client
        self._payload_path = payload_path
        self._agent_uuid = agent_uuid
        self._system_uuid = system_uuid
        self._caps_drivers = caps_drivers
        self._facts_drivers = facts_drivers
        self._verify_node_on_register = verify_node_on_register

    def _register_agent(self) -> None:
        capabilities = itertools.chain.from_iterable(
            d.get_capabilities() for d in self._caps_drivers
        )
        facts = itertools.chain.from_iterable(
            d.get_facts() for d in self._facts_drivers
        )
        agent = models.UniversalAgent.from_system_uuid(
            capabilities=capabilities,
            facts=facts,
            agent_uuid=self._agent_uuid,
            system_uuid=self._system_uuid,
        )
        try:
            self._orch_client.agents_create(
                agent, check_node_exists=self._verify_node_on_register
            )
            LOG.info("Agent registered: %s", agent.uuid)
        except orch_exc.AgentAlreadyExists:
            LOG.warning("Agent already registered: %s", agent.uuid)

            # Update the agent capabilities and facts if they were changed
            self._orch_client.agents_update(agent)
        except orch_exc.NodeNotFound:
            LOG.warning(
                "Node verification failed, cannot register agent %s", agent.uuid
            )

    def _create_resource(
        self,
        driver: driver_base.AbstractCapabilityDriver,
        resource: models.Resource,
    ) -> models.Resource:
        try:
            dp_resource = driver.create(resource)
        except driver_exc.ResourceAlreadyExists:
            LOG.warning("The resource already exists: %s", resource.uuid)
            dp_resource = driver.get(resource)

        return dp_resource

    def _delete_resource(
        self,
        driver: driver_base.AbstractCapabilityDriver,
        resource: models.Resource,
    ) -> None:
        try:
            driver.delete(resource)
        except driver_exc.ResourceNotFound:
            LOG.warning("The resource does not exist: %s", resource.uuid)

    def _update_resource(
        self,
        driver: driver_base.AbstractCapabilityDriver,
        resource: models.Resource,
    ) -> models.Resource:
        dp_resource = driver.update(resource)
        return dp_resource

    def _cap_driver_iteration(
        self,
        driver: driver_base.AbstractCapabilityDriver,
        payload: models.Payload,
        collected_payload: models.Payload,
    ) -> list[models.Resource]:
        try:
            # Perform some preparations for the driver
            driver.start()

            for capability in driver.get_capabilities():
                # Skip capabilities that are not in the payload
                if capability not in payload.capabilities:
                    LOG.debug("Skipping capability %s", capability)
                    continue

                self._capability_iteration(
                    driver, capability, payload, collected_payload
                )
        except Exception:
            LOG.exception("Error actualizing driver %s", driver.__class__.__name__)
        finally:
            # Finalize the driver
            driver.finalize()

    def _capability_iteration(
        self,
        driver: driver_base.AbstractCapabilityDriver,
        capability: str,
        payload: models.Payload,
        collected_payload: models.Payload,
    ) -> None:
        target_resources = payload.caps_resources(capability)

        try:
            # Perform some preparations for the capability
            driver.start_capability(capability)

            collected_resources = self._actualize_capability(
                driver, capability, target_resources
            )
            collected_payload.add_caps_resources(collected_resources)

            # All gathered resources for capabilities are considered
            # as facts too
            collected_payload.add_facts_resources(collected_resources)
        except Exception:
            LOG.exception("Error actualizing resources for %s", capability)
        finally:
            # Finalize the capability
            driver.finalize_capability(capability)

    def _actualize_capability(
        self,
        driver: driver_base.AbstractCapabilityDriver,
        capability: str,
        resources: list[models.Resource],
    ) -> list[models.Resource]:
        """
        Actualize resources and return a list of resources from data plane.
        """
        target_resources = {r: r for r in resources}
        actual_resources = {r: r for r in driver.list(capability)}

        # A list of resources to be collected from the data plane
        # for this capability
        collected_resources = []

        # Create all new resources
        for r in target_resources.keys() - actual_resources.keys():
            try:
                resource = self._create_resource(driver, r)
                collected_resources.append(resource)
            except Exception:
                LOG.exception(
                    "Error creating resource %s with uuid (%s) and value (%s)",
                    capability,
                    r.uuid,
                    str(r.value),
                )

        # Delete outdated resources
        for r in actual_resources.keys() - target_resources.keys():
            try:
                self._delete_resource(driver, r)
            except Exception:
                # The resource wasn't deleted so add it back
                collected_resources.append(r)
                LOG.exception("Error deleting resource %s", r.uuid)

        for r in target_resources.keys() & actual_resources.keys():
            # set does not guarantee which instance will be given on
            # intersection therefore get actual and target resources
            # explicitly.
            target_resource = target_resources[r]
            actual_resource = actual_resources[r]

            # Nothing to do if the resources are the same
            if target_resource.hash == actual_resource.hash:
                collected_resources.append(actual_resource)
                continue

            try:
                resource = self._update_resource(driver, target_resource)
                collected_resources.append(resource)
            except Exception:
                LOG.exception("Error updating resource %s", r.uuid)

        return collected_resources

    def _actualize_resource_facts(
        self,
        target_facts: list[dict[str : tp.Any]],
        actual_facts: list[dict[str : tp.Any]],
    ) -> None:
        """Actualize facts in Status API.

        target_facts - The facts collected from the data plane.
        actual_facts - The facts collected from the Status API.
        """
        target_resources = {r["uuid"]: r for r in target_facts}
        actual_resources = {r["uuid"]: r for r in actual_facts}

        for uuid in target_resources.keys() - actual_resources.keys():
            resource = target_resources[uuid]
            resource["node"] = (
                str(self._system_uuid) if self._system_uuid else str(utils.node_uuid())
            )
            try:
                resource = models.Resource.restore_from_simple_view(**resource)
                self._orch_client.resources_create(resource)
            except Exception:
                LOG.exception("Error creating resource %s", uuid)

        for uuid in actual_resources.keys() - target_resources.keys():
            resource = actual_resources[uuid]

            try:
                resource = models.Resource.restore_from_simple_view(**resource)
                self._orch_client.resources_delete(resource)
            except Exception:
                LOG.exception("Error deleting resource %s", uuid)

        for uuid in target_resources.keys() & actual_resources.keys():
            target_resource = target_resources[uuid]
            actual_resource = actual_resources[uuid]

            # Nothing to do if the resources are the same
            if target_resource["full_hash"] == actual_resource["full_hash"]:
                continue

            # Remove read-only fields
            resource = target_resource.copy()
            resource.pop("created_at", None)
            resource.pop("updated_at", None)
            resource.pop("res_uuid", None)
            resource.pop("node", None)

            res_uuid = resource.pop("uuid")
            kind = resource.pop("kind")

            try:
                self._orch_client.resources_update(kind, res_uuid, **resource)
            except Exception:
                LOG.exception("Error updating resource %s", uuid)

    def _actualize_facts(self, target_facts: dict, actual_facts: dict) -> None:
        """Actualize facts in Status API.

        target_facts - The facts collected from the data plane.
        actual_facts - The facts collected from the Status API.
        """
        # New fact category
        for fact in target_facts.keys() - actual_facts.keys():
            resources = target_facts[fact]["resources"]
            self._actualize_resource_facts(resources, [])

        # Deleted fact category
        for fact in actual_facts.keys() - target_facts.keys():
            resources = actual_facts[fact]["resources"]
            self._actualize_resource_facts([], resources)

        # Actualize resource facts in the existing fact category
        for fact in target_facts.keys() & actual_facts.keys():
            target_resources = target_facts[fact]["resources"]
            actual_resources = actual_facts[fact]["resources"]
            self._actualize_resource_facts(target_resources, actual_resources)

    def _setup(self):
        # Call registry at start to update capabilities and facts
        self._register_agent()

    def _iteration(self):
        # The payload is collected every iteration from the data plane.
        # At the end of the iteration the payload hash is calculated
        # and it is saved
        collected_payload = models.Payload.empty()

        # Last successfully saved payload. Use it to compare with CP payload.
        if self._payload_path:
            last_payload = models.Payload.load(self._payload_path)
        else:
            last_payload = None

        # Check if the agent is registered
        try:
            payload = self._orch_client.agents_get_payload(
                self._agent_uuid, last_payload
            )
        except orch_exc.AgentNotFound:
            # Auto discovery mechanism
            self._register_agent()
            return

        # TODO(akremenetsky): Implement actions

        # Capabilities
        for driver in self._caps_drivers:
            self._cap_driver_iteration(driver, payload, collected_payload)

        # TODO(akremenetsky): Implement facts iterations like capabilities iteration
        # Facts
        for driver in self._facts_drivers:
            for fact in driver.get_facts():
                # Skip facts that are not in the payload
                if fact not in payload.facts:
                    LOG.debug("Skipping fact %s", fact)
                    continue

                try:
                    collected_facts = driver.list(fact)
                    collected_payload.add_facts_resources(collected_facts)
                except Exception:
                    LOG.exception("Error collecting resources for fact: %s", fact)

        # All work done. The target resources are applied and facts collected.
        # Calculate the hash of the collected payload
        collected_payload.calculate_hash()

        # The payloads aren't the same. It means the facts were updated.
        if collected_payload != payload:
            self._actualize_facts(collected_payload.facts, payload.facts)

        # The capabilities were applied but the target and actual payloads
        # are different. So the difference in the facts. Update it.

        # Save the collected payload after actualization
        if self._payload_path:
            collected_payload.save(self._payload_path)
