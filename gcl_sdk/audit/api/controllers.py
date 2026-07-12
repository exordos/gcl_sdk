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

import typing as tp

from gcl_iam import controllers as iam_controllers
from gcl_iam import exceptions as iam_exceptions
from gcl_iam import rules as iam_rules
from restalchemy.api import constants as api_constants
from restalchemy.api import controllers
from restalchemy.api import packers
from restalchemy.api import resources

from gcl_sdk.audit.dm import models


class AuditPolicyBasedController(iam_controllers.PolicyBasedController):
    """Shared serialization and project-aware IAM policy for Audit APIs."""

    def get_packer(
        self,
        content_type: str,
        resource_type: tp.Any = None,
    ) -> tp.Any:
        if content_type == api_constants.CONTENT_TYPE_APPLICATION_JSON:
            resource_type = resource_type or self.get_resource()
            return packers.JSONPackerIncludeNullFields(
                resource_type,
                request=self._req,
            )
        return super().get_packer(content_type, resource_type)

    def _enforce_and_override_project_id_in_kwargs(
        self,
        method: str,
        kwargs: dict[str, tp.Any],
    ) -> None:
        if method != "read":
            super()._enforce_and_override_project_id_in_kwargs(method, kwargs)
            return

        read_all = iam_rules.Rule(
            self.__policy_service_name__,
            self.__policy_name__,
            "read_all",
        )
        if self._enforcer.enforce(read_all):
            if self._ctx_project_id is not None:
                if "project_id" in kwargs:
                    self._force_project_id(kwargs["project_id"])
                else:
                    kwargs["project_id"] = self._ctx_project_id
            return

        self._enforce("read")
        if self._ctx_project_id is None:
            raise iam_exceptions.Forbidden()

        super()._enforce_and_override_project_id_in_kwargs(method, kwargs)


class AuditController(
    AuditPolicyBasedController,
    controllers.BaseResourceControllerPaginated,
):
    __policy_service_name__ = "audit"
    __policy_name__ = "events"
    __resource__ = resources.ResourceByRAModel(
        model_class=models.AuditDeliveryEvent,
        process_filters=True,
        convert_underscore=False,
    )
