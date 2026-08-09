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

from gcl_looper.services import basic as looper_basic
from restalchemy.common import contexts
from restalchemy.dm import filters as dm_filters

from gcl_sdk.agents.universal import constants as c
from gcl_sdk.agents.universal.dm import models

LOG = logging.getLogger(__name__)


class UniversalAgentSchedulerService(looper_basic.BasicService):
    def __init__(
        self,
        capabilities: list[str],
        iter_min_period=3,
        iter_pause=0.1,
    ):
        super().__init__(iter_min_period, iter_pause)
        self._capabilities = capabilities

    def _get_unscheduled_resources(
        self, limit: int = c.DEF_SQL_LIMIT
    ) -> list[models.TargetResource]:
        capabilities_in = []
        filters_in = []
        filters_like = []

        # Prepare like and in filters based on capabilities
        for cap in self._capabilities:
            if cap.endswith("*"):
                cap = cap.replace("*", "%")
                filters_like.append({"kind": dm_filters.Like(cap)})
            else:
                capabilities_in.append(cap)

        if capabilities_in:
            filters_in = [{"kind": dm_filters.In(capabilities_in)}]

        if not filters_like and not filters_in:
            LOG.debug("No filters for target resources")
            return []

        filters = dm_filters.AND(
            {"agent": dm_filters.Is(None)},
            dm_filters.OR(*itertools.chain(filters_in + filters_like)),
        )

        return models.TargetResource.objects.get_all(
            filters=filters,
            limit=limit,
        )

    def _iteration(self):
        with contexts.Context().session_manager():
            unscheduled = self._get_unscheduled_resources()
            if not unscheduled:
                return

            LOG.info("Found %d unscheduled resources", len(unscheduled))

            cap_agent_map = models.UniversalAgent.have_capabilities(
                tuple(u.kind for u in unscheduled)
            )

            # FIXME(akremenetsky): The simplest implementation.
            # Take a first agent that satisfies
            for resource in unscheduled:
                if resource.kind not in cap_agent_map:
                    LOG.warning("No agent for resource %s", resource.kind)
                    continue

                # It's supposed only one agent have a particular EM capability
                # In case of multiple agents, the first one is chosen for nodes,
                # the second one is chosen for configs and so on.
                agent = cap_agent_map[resource.kind][0]
                resource.agent = agent.uuid
                resource.node = agent.node
                resource.save()
                LOG.info(
                    "Assigning resource %s to agent %s",
                    resource.uuid,
                    agent.uuid,
                )
