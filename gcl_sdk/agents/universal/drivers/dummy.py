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

import os

from gcl_sdk.agents.universal.dm import models
from gcl_sdk.agents.universal.drivers import base
from gcl_sdk.agents.universal.drivers import exceptions as driver_exc


class DummyFilesDriver(base.AbstractCapabilityDriver):
    """An example driver for testing.

    This driver is intended for testing and development only.
    """

    def __init__(self, work_dir: str):
        super().__init__()
        self._work_dir = work_dir

    def get(self, resource: models.Resource) -> models.Resource:
        """Find and return the file resource."""
        for f in os.listdir(self.work_dir):
            uuid, name = f.split("_", 1)
            if uuid == str(resource.uuid):
                value = {"uuid": uuid, "name": name}
                new_resource = models.Resource.from_value(value, resource.kind)
                return new_resource

        raise driver_exc.ResourceNotFound(resource=resource)

    def list(self, capability: str) -> list[models.Resource]:
        """Lists all files"""
        resources = []

        for f in os.listdir(self.work_dir):
            uuid, name = f.split("_", 1)
            value = {"uuid": uuid, "name": name}
            resource = models.Resource.from_value(value, capability)
            resources.append(resource)

        return resources

    def create(self, resource: models.Resource) -> models.Resource:
        """Creates a new file in the work directory."""
        name = f"{resource.value['uuid']}_{resource.value['name']}"
        path = os.path.join(self.work_dir, name)
        with open(path, "w") as f:
            f.write("")
        return resource

    def update(self, resource: models.Resource) -> models.Resource:
        """Update the file in the work directory."""
        self.delete(resource)
        return self.create(resource)

    def delete(self, resource: models.Resource) -> None:
        """Delete the file in the work directory."""
        try:
            res = self.get(resource)
        except driver_exc.ResourceNotFound:
            # Nothing to do, the resource does not exist
            return

        name = f"{res.value['uuid']}-{res.value['name']}"
        path = os.path.join(self.work_dir, name)
        os.remove(path)
