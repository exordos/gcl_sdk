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

from gcl_sdk.agents.universal.dm import models


class AbstractCapabilityDriver(abc.ABC):
    """Abstract driver for capabilities.

    The main purpose of this driver is to apply target
    capabilities to the DP.
    """

    @abc.abstractmethod
    def get_capabilities(self) -> list[str]:
        """Returns a list of capabilities supported by the driver."""

    @abc.abstractmethod
    def get(self, resource: models.Resource) -> models.Resource:
        """Find and return a resource by uuid and kind.

        It returns the resource from the data plane.
        """

    @abc.abstractmethod
    def create(self, resource: models.Resource) -> models.Resource:
        """Creates a resource."""

    @abc.abstractmethod
    def update(self, resource: models.Resource) -> models.Resource:
        """Update the resource.

        The simplest implementation. The driver should detect which
        fields were changed itself.
        """

    @abc.abstractmethod
    def list(self, capability: str) -> list[models.Resource]:
        """Lists all resources by capability."""

    @abc.abstractmethod
    def delete(self, resource: models.Resource) -> None:
        """Delete the resource."""

    def start_capability(self, capability: str) -> None:
        """Perform `capability` initialization.

        This method is called once before any other capability method like list,
        create, update, delete are called. It can be used to do some
        preparations like establishing connections, opening files, etc.

        The driver iteration:
            start -> [start_capability -> list -> [create | update | delete]* -> \
                finalize_capability]* -> finalize
        """

    def finalize_capability(self, capability: str) -> None:
        """Perform `capability` finalization.

        This method is called once after all other capability methods like list,
        create, update, delete are called. It can be used to do some
        finalization or cleanups like closing connections, files, etc.

        The driver iteration:
            start -> [start_capability -> list -> [create | update | delete]* -> \
                finalize_capability]* -> finalize
        """

    def start(self) -> None:
        """Perform some initialization before starting any operations.

        This method is called once at driver begin of its workflow.

        The driver iteration:
            start -> [start_capability -> list -> [create | update | delete]* -> \
                finalize_capability]* -> finalize
        """

    def finalize(self) -> None:
        """Perform some finalization after finishing all operations.

        This method is called once at driver end of its workflow.

        The driver iteration:
            start -> [start_capability -> list -> [create | update | delete]* -> \
                finalize_capability]* -> finalize
        """


class AbstractFactDriver(abc.ABC):
    """Abstract driver for facts.

    The main purpose of this driver is to gather facts from the
    data plane. The simplest example is gathering network interfaces
    from nodes.
    """

    @abc.abstractmethod
    def get_facts(self) -> list[str]:
        """Returns a list of facts supported by the driver."""

    @abc.abstractmethod
    def get(self, resource: models.Resource) -> models.Resource:
        """Find and return a resource by uuid and kind.

        It returns the resource from the data plane.
        """

    @abc.abstractmethod
    def list(self, fact: str) -> list[models.Resource]:
        """Lists all resources by facts."""
