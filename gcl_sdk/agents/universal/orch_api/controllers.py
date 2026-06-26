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
from restalchemy.api import actions
from restalchemy.api import resources

from gcl_sdk.agents.universal.api import controllers as sdk_controllers
from gcl_sdk.agents.universal.dm import models


class UniversalAgentsController(sdk_controllers.BaseSdkResourceController):
    """Controller for /v1/agents/ endpoint"""

    __resource__ = resources.ResourceByRAModel(
        model_class=models.UniversalAgent,
        process_filters=True,
        convert_underscore=False,
    )

    @actions.get
    def get_payload(
        self,
        resource: models.UniversalAgent,
        hash: str = "",
        version: str = "0",
    ):
        payload = resource.get_payload(hash=hash, version=int(version))
        return payload.dump_to_simple_view()
