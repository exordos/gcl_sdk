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

from typing import Any
import uuid as sys_uuid

from gcl_iam import exceptions as iam_exceptions
from restalchemy.common import contexts
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.storage.sql import orm

from gcl_sdk.audit import constants
from gcl_sdk.common import constants as sdk_constants


class AuditEventBase(
    models.ModelWithTimestamp,
    models.ModelWithRequiredUUID,
):
    service_name = properties.property(
        types.String(max_length=128),
        required=True,
        read_only=True,
    )
    resource_type = properties.property(
        types.String(max_length=128),
        required=True,
        read_only=True,
    )
    resource_uuid = properties.property(
        types.UUID(),
        required=True,
        read_only=True,
    )
    project_id = properties.property(
        types.AllowNone(types.UUID()),
        default=None,
        read_only=True,
    )
    actor_user_uuid = properties.property(
        types.AllowNone(types.UUID()),
        default=None,
        read_only=True,
    )
    action = properties.property(
        types.Enum([action.value for action in constants.Action]),
        required=True,
        read_only=True,
    )
    snapshot = properties.property(
        types.AllowNone(types.Dict()),
        default=None,
        read_only=True,
    )

    def dump_to_delivery_snapshot(self) -> dict[str, Any]:
        snapshot: dict[str, Any] = {}
        for field_name in constants.INGEST_FIELDS:
            prop = self.properties.properties[field_name]
            snapshot[field_name] = prop.get_property_type().to_simple_type(
                getattr(self, field_name)
            )
        return snapshot


class AuditDeliveryEvent(AuditEventBase, orm.SQLStorableMixin):
    __tablename__ = "gcl_sdk_audit_events"

    STATUS = constants.DeliveryStatus

    status = properties.property(
        types.Enum([status.value for status in STATUS]),
        default=STATUS.NEW.value,
    )

    def update(
        self,
        session: Any = None,
        force: bool = False,
    ) -> None:
        """Persist delivery state without mutating immutable event timestamps."""

        orm.SQLStorableMixin.update(self, session=session, force=force)


class AuditLogSQLStorableMixin(orm.SQLStorableMixin):
    __audit_service_name__ = sdk_constants.GLOBAL_SERVICE_NAME
    __audit_resource_type__ = None

    def insert(self, session: Any = None) -> None:
        with self._get_engine().session_manager(session=session) as s:
            super().insert(session=s)
            self._write_audit_event(
                action=constants.Action.create.value,
                snapshot=self.dump_to_audit_snapshot(),
                session=s,
            )

    def update(
        self,
        session: Any = None,
        force: bool = False,
    ) -> None:
        if force or self.is_dirty():
            with self._get_engine().session_manager(session=session) as s:
                super().update(session=s, force=force)
                self._write_audit_event(
                    action=constants.Action.update.value,
                    snapshot=self.dump_to_audit_snapshot(),
                    session=s,
                )

    def delete(self, session: Any = None) -> None:
        with self._get_engine().session_manager(session=session) as s:
            super().delete(session=s)
            self._write_audit_event(
                action=constants.Action.delete.value,
                snapshot=None,
                session=s,
            )

    def dump_to_audit_snapshot(self) -> dict[str, Any]:
        return self.dump_to_simple_view()

    @classmethod
    def get_audit_service_name(cls) -> str:
        if cls.__audit_service_name__ is None:
            raise RuntimeError("Audit service_name is not configured")
        return cls.__audit_service_name__

    @classmethod
    def get_audit_resource_type(cls) -> str:
        if cls.__audit_resource_type__ is None:
            raise RuntimeError("Audit resource_type is not configured")
        return cls.__audit_resource_type__

    def get_audit_resource_uuid(self) -> sys_uuid.UUID:
        return self.uuid

    def get_audit_project_id(self) -> sys_uuid.UUID | None:
        if hasattr(self, "project_id"):
            return self.project_id
        return self._get_audit_context_project_id()

    def get_audit_actor_user_uuid(self) -> sys_uuid.UUID | None:
        try:
            context = contexts.get_context()
            iam_context = getattr(context, "iam_context", None)
            return getattr(getattr(iam_context, "token_info", None), "user_uuid", None)
        except (
            contexts.ContextIsNotExistsInStorage,
            iam_exceptions.NoIamSessionStored,
        ):
            return None

    def _get_audit_context_project_id(self) -> sys_uuid.UUID | None:
        try:
            return contexts.get_context().project_id
        except (contexts.ContextIsNotExistsInStorage, AttributeError):
            return None

    def _write_audit_event(
        self,
        action: str,
        snapshot: dict[str, Any] | None,
        session: Any = None,
    ) -> None:
        AuditDeliveryEvent(
            uuid=sys_uuid.uuid4(),
            service_name=self.get_audit_service_name(),
            resource_type=self.get_audit_resource_type(),
            resource_uuid=self.get_audit_resource_uuid(),
            project_id=self.get_audit_project_id(),
            actor_user_uuid=self.get_audit_actor_user_uuid(),
            action=action,
            snapshot=snapshot,
        ).insert(session)
