from gcl_iam import controllers as iam_controllers
from restalchemy.api import controllers
from restalchemy.api import resources

from gcl_sdk.audit.dm import models


class AuditController(
    iam_controllers.PolicyBasedControllerMixin,
    controllers.BaseResourceControllerPaginated,
):
    __policy_service_name__ = "audit_log"
    __policy_name__ = "audit_record"
    __resource__ = resources.ResourceByRAModel(
        model_class=models.AuditRecord,
        process_filters=True,
        convert_underscore=False,
    )
