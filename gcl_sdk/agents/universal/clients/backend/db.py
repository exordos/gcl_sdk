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

from restalchemy.common import contexts
from restalchemy.dm import filters as dm_filters
from restalchemy.storage import base as ra_storage
from restalchemy.storage import exceptions as ra_exc

from gcl_sdk.agents.universal.clients.backend import base
from gcl_sdk.agents.universal.clients.backend import exceptions as client_exc
from gcl_sdk.agents.universal.dm import models
from gcl_sdk.agents.universal.storage import base as storage_base

LOG = logging.getLogger(__name__)


def merge_declared_object(current: tp.Any, patch: tp.Any) -> tp.Any:
    """Merge declared dictionary keys; scalars and lists replace atomically.

    Only schema-declared model fields opt in. A new discriminator starts a
    new object, rather than carrying properties from the previous variant.
    """
    if not isinstance(current, dict) or not isinstance(patch, dict):
        return patch
    if "kind" in patch and patch["kind"] != current.get("kind"):
        return patch
    result = current.copy()
    for key, value in patch.items():
        result[key] = merge_declared_object(current.get(key), value)
    return result


class ModelSpec(tp.NamedTuple):
    model: type[ra_storage.AbstractStorableMixin]
    kind: str

    # Special case if the filters are `None` it means the target fields
    # storage will be used as filters.
    # Example:
    # filters = None
    # target_fields = {
    #     "kind_foo": {
    #         "uuid1": ["field1", "field2"],
    #         "uuid2": ["field1"],
    #     }
    # }
    # The `list` method will use the following filters:
    # filters = {
    #     "uuid": dm_filters.In(["uuid1", "uuid2"]),
    # }
    filters: dict[str, dm_filters.AbstractClause] | None = None

    # Inject filter fields into the model if the model does not have them
    # This is used to be able to filter resources on the list method
    inject_filter_fields: bool = True

    @classmethod
    def from_collection(
        cls,
        collection: tp.Collection[tuple[type, str]],
        filters: dict[str, dm_filters.AbstractClause],
    ) -> tuple[ModelSpec, ...]:
        return tuple(
            cls(
                model=model,
                kind=kind,
                filters=filters,
            )
            for model, kind in collection
        )


class DatabaseBackendClient(base.AbstractBackendClient):
    """Database backend client."""

    def get_create_only_fields(self, kind: str) -> tp.Collection[str]:
        return getattr(self._model_spec_map[kind].model, "__create_only_fields__", ())

    def __init__(
        self,
        model_specs: tp.Collection[ModelSpec],
        tf_storage: storage_base.AbstractTargetFieldsStorage,
        session: tp.Any | None = None,
    ):
        super().__init__()
        self._model_spec_map = {m.kind: m for m in model_specs}
        self._tf_storage = tf_storage
        self.set_session(session)

    def _get_resource_filters(
        self, resource: models.Resource
    ) -> dict[str, dm_filters.AbstractClause]:
        model_spec = self._model_spec_map[resource.kind]
        filters = {"uuid": dm_filters.EQ(str(resource.uuid))}
        if model_spec.filters is not None:
            filters.update(model_spec.filters)
        return filters

    def _get_filters(self, kind: str) -> dict[str, dm_filters.AbstractClause]:
        """Get filters for the kind.

        If the model spec has filters, return them.
        Otherwise, construct them from the target fields
        from the storage.
        """
        model_spec = self._model_spec_map[kind]
        if model_spec.filters is not None:
            return model_spec.filters

        # Construct filters from the target fields
        target_fields: dict = self._tf_storage.storage()
        if kind not in target_fields or not target_fields[kind]:
            return {}

        return {"uuid": dm_filters.In(tuple(target_fields[kind].keys()))}

    def _get(self, session: tp.Any, resource: models.Resource) -> models.ResourceMixin:
        """Find and return a resource by uuid and kind."""
        model_spec = self._model_spec_map[resource.kind]
        filters = self._get_resource_filters(resource)

        try:
            return model_spec.model.objects.get_one(
                session=session,
                filters=filters,
            )
        except ra_exc.RecordNotFound:
            LOG.exception("Unable to find %s %s", str(model_spec), resource.uuid)
            raise client_exc.ResourceNotFound(resource=resource)

    def _list(self, session: tp.Any, capability: str) -> list[models.ResourceMixin]:
        """Lists all resources by capability."""
        model_spec = self._model_spec_map[capability]
        filters = self._get_filters(capability)

        if not filters:
            return []

        # Get all objects for the project from the database
        return model_spec.model.objects.get_all(
            session=session,
            filters=filters,
        )

    def _create(
        self, session: tp.Any, resource: models.Resource
    ) -> models.ResourceMixin:
        """Creates a resource."""
        model_spec = self._model_spec_map[resource.kind]
        res_filters = self._get_resource_filters(resource)

        # Check if the resource already exists
        # We need to do this check since PG does not correctly handle
        # the case when the resource already exists and fails the transaction
        obj = model_spec.model.objects.get_one_or_none(
            session=session,
            filters=res_filters,
        )
        if obj is not None:
            LOG.warning("The resource already exists: %s", resource.uuid)
            raise client_exc.ResourceAlreadyExists(resource=resource)

        # Inject filter fields into the resource value if they are not present
        validator = getattr(model_spec.model, "validate_create_input", None)
        if validator is not None:
            validator(resource.value)
        if model_spec.inject_filter_fields and model_spec.filters:
            value = resource.value.copy()
            for field, _filter in model_spec.filters.items():
                if field not in value:
                    value[field] = _filter.value
                else:
                    LOG.warning(
                        "The filter field %s is already present in "
                        "the resource value: %s",
                        field,
                        resource.uuid,
                    )
            obj = model_spec.model.restore_from_simple_view(**value)
        else:
            obj = model_spec.model.from_ua_resource(resource)

        # Save to db
        obj.insert(session=session)

        return obj

    def _update(
        self, session: tp.Any, resource: models.Resource
    ) -> models.ResourceMixin:
        """Update the resource."""
        model_spec = self._model_spec_map[resource.kind]
        res_filters = self._get_resource_filters(resource)

        # Check if the resource already exists
        obj = model_spec.model.objects.get_one_or_none(
            session=session,
            filters=res_filters,
        )
        if obj is None:
            LOG.warning("The resource does not exist: %s", resource.uuid)
            raise client_exc.ResourceNotFound(resource=resource)

        create_only = self.get_create_only_fields(resource.kind)
        value = {k: v for k, v in resource.value.items() if k not in create_only}
        validator = getattr(model_spec.model, "validate_update_input", None)
        if validator is not None:
            validator(value, obj)
        for field in getattr(model_spec.model, "__partial_update_fields__", ()):
            if field in value:
                current = (
                    obj.properties[field]
                    .get_property_type()
                    .to_simple_type(getattr(obj, field))
                )
                value[field] = merge_declared_object(current, value[field])
        current_view = {
            name: prop.get_property_type().to_simple_type(getattr(obj, name))
            for name, prop in obj.properties.properties.items()
        }
        updated_obj = model_spec.model.from_ua_resource(
            resource.replace_value({**current_view, **value})
        )

        # Update the object
        for field_name in value:
            prop = obj.properties.get(field_name)
            if not prop or prop.is_read_only():
                continue
            setattr(obj, field_name, getattr(updated_obj, field_name))

        # Only write when a target field actually changed. A no-op update()
        # is not free: models may run side effects on write (e.g. a child
        # model touching its parent's updated_at). If the desired target
        # carries only read-only/id fields (or already matches the DB), an
        # unconditional update() bumps timestamps every iteration, which
        # makes every other reconciler sharing that row see it as "changed"
        # and re-actualize forever — a self-sustaining write loop that never
        # settles. Skipping a clean update keeps reconciliation idempotent.
        if obj.is_dirty():
            obj.update(session=session)

        return obj

    def _delete(self, session: tp.Any, resource: models.Resource) -> None:
        """Delete the resource."""
        model_spec = self._model_spec_map[resource.kind]
        res_filters = self._get_resource_filters(resource)

        try:
            obj = model_spec.model.objects.get_one(
                session=session,
                filters=res_filters,
            )
            obj.delete(session=session)
            LOG.debug("Deleted resource: %s", resource.uuid)
        except ra_exc.RecordNotFound:
            LOG.warning("The resource is already deleted: %s", resource.uuid)

    def set_session(self, session: tp.Any) -> None:
        """Set the session to be used by the client."""
        self._session = session

    def clear_session(self) -> None:
        """Clear the session."""
        self._session = None

    def get(self, resource: models.Resource) -> models.ResourceMixin:
        """Find and return a resource by uuid and kind."""
        if self._session:
            return self._get(self._session, resource)

        with contexts.Context().session_manager() as session:
            return self._get(session, resource)

    def list(self, capability: str) -> list[models.ResourceMixin]:
        """Lists all resources by capability."""
        if self._session:
            return self._list(self._session, capability)

        with contexts.Context().session_manager() as session:
            return self._list(session, capability)

    def create(self, resource: models.Resource) -> models.ResourceMixin:
        """Creates a resource."""
        if self._session:
            return self._create(self._session, resource)

        with contexts.Context().session_manager() as session:
            return self._create(session, resource)

    def update(self, resource: models.Resource) -> models.ResourceMixin:
        """Update the resource."""
        if self._session:
            return self._update(self._session, resource)

        with contexts.Context().session_manager() as session:
            return self._update(session, resource)

    def delete(self, resource: models.Resource) -> None:
        """Delete the resource."""
        if self._session:
            return self._delete(self._session, resource)

        with contexts.Context().session_manager() as session:
            return self._delete(session, resource)
