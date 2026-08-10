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
from unittest import mock
import uuid as sys_uuid

import pytest

from gcl_sdk.agents.universal import constants as ua_c
from gcl_sdk.agents.universal.dm import models as ua_models
from gcl_sdk.agents.universal.services import builder as builder_svc
from gcl_sdk.tests.functional import conftest
from gcl_sdk.tests.functional import utils as test_utils


class DummyBuilder(builder_svc.UniversalBuilderService):
    def __init__(self, instance_model):
        super().__init__(instance_model=instance_model)
        self.pre_create_called = False
        self.post_create_called = False
        self.pre_update_called = False
        self.post_update_called = False
        self.create_instance_derivatives_called = False
        self.deleted_pre_called = []

    def pre_create_instance_resource(self, instance):
        self.pre_create_called = True

    def post_create_instance_resource(self, instance, resource, derivatives=()):
        self.post_create_called = True
        instance.status = ua_c.InstanceStatus.IN_PROGRESS.value

    def pre_update_instance_resource(self, instance, resource):
        self.pre_update_called = True
        instance.status = ua_c.InstanceStatus.IN_PROGRESS.value

    def post_update_instance_resource(self, instance, resource, derivatives=()):
        self.post_update_called = True

    def pre_delete_instance_resource(self, resource):
        self.deleted_pre_called.append(resource)

    def create_instance_derivatives(
        self, instance: ua_models.InstanceMixin
    ) -> tp.Collection[ua_models.TargetResourceKindAwareMixin]:
        self.create_instance_derivatives_called = True
        return ()


class DummyBuilderWithDerivatives(DummyBuilder):
    def create_instance_derivatives(
        self, instance: conftest.DummyInstanceWithDerivatives
    ) -> tp.Collection[conftest.DummyDerivative]:
        self.create_instance_derivatives_called = True
        return [instance.to_derivative()]


class DummyBuilderWithMasterTracking(DummyBuilderWithDerivatives):
    def track_outdated_master_hash_instances(self) -> bool:
        """Track outdated master hash instances."""
        return True

    def track_outdated_master_full_hash_instances(self) -> bool:
        """Track outdated master full hash instances."""
        return True

    def actualize_outdated_master_hash_instance(
        self,
        instance,
        master_instance,
        derivatives,
    ) -> tp.Collection[ua_models.TargetResourceKindAwareMixin]:
        return tuple(p[0] for p in derivatives)

    def actualize_outdated_master_full_hash_instance(
        self,
        instance: ua_models.InstanceMixin,
        master_instance: ua_models.InstanceMixin,
        derivatives: tp.Collection[
            tuple[
                ua_models.TargetResourceKindAwareMixin,  # The target resource
                ua_models.TargetResourceKindAwareMixin | None,  # The actual resource
            ]
        ],
    ) -> tp.Collection[ua_models.TargetResourceKindAwareMixin]:
        return tuple(p[0] for p in derivatives)


class TestUniversalBuilderService:
    # Need only to apply DB migrations
    @pytest.fixture(scope="class", autouse=True)
    def api_service(self, orch_api_wsgi_app):
        class ApiRestService(test_utils.RestServiceTestCase):
            __FIRST_MIGRATION__ = conftest.FIRST_MIGRATION
            __APP__ = orch_api_wsgi_app

        rest_service = ApiRestService()
        rest_service.setup_class()

        yield rest_service

        rest_service.teardown_class()

    @pytest.fixture(autouse=True)
    def db_migrations(self, api_service: test_utils.RestServiceTestCase):
        api_service.setup_method()
        yield api_service
        api_service.teardown_method()

    def test_no_actions(self):
        svc = DummyBuilder(instance_model=conftest.DummyInstance)
        svc._iteration()

    # No derivatives

    def test_create_new_instance(self, dummy_instance_factory: tp.Callable):
        instance = dummy_instance_factory({"new": 1})[0]

        svc = DummyBuilder(instance_model=conftest.DummyInstance)
        svc._iteration()

        resources = ua_models.TargetResource.objects.get_all()
        assert len(resources) == 1

        resource = resources[0]
        assert resource.uuid == instance.uuid
        assert resource.kind == conftest.DummyInstance.get_resource_kind()

        resources = ua_models.Resource.objects.get_all()
        assert len(resources) == 0

        assert svc.pre_create_called
        assert svc.post_create_called
        assert not svc.create_instance_derivatives_called

    def test_create_multiple_instances(self, dummy_instance_factory: tp.Callable):
        instances = dummy_instance_factory({"new": 2})

        svc = DummyBuilder(instance_model=conftest.DummyInstance)
        svc._iteration()

        resources = ua_models.TargetResource.objects.get_all()
        assert len(resources) == len(instances)

        resource = resources[0]
        assert {r.uuid for r in resources} == {i.uuid for i in instances}
        assert resource.kind == conftest.DummyInstance.get_resource_kind()

        resources = ua_models.Resource.objects.get_all()
        assert len(resources) == 0

        assert svc.pre_create_called
        assert svc.post_create_called
        assert not svc.create_instance_derivatives_called

    def test_delete_instance(self, dummy_instance_factory: tp.Callable):
        resource = dummy_instance_factory({"deleted": 1})[0]

        svc = DummyBuilder(instance_model=conftest.DummyInstance)
        svc._iteration()

        resources = ua_models.TargetResource.objects.get_all()
        assert len(resources) == 0

        resources = ua_models.Resource.objects.get_all()
        assert len(resources) == 0

        assert svc.deleted_pre_called[0] == resource
        assert not svc.pre_create_called
        assert not svc.post_create_called
        assert not svc.create_instance_derivatives_called

    def test_delete_multiple_instances(self, dummy_instance_factory: tp.Callable):
        instance_resources = dummy_instance_factory({"deleted": 2})

        svc = DummyBuilder(instance_model=conftest.DummyInstance)
        svc._iteration()

        resources = ua_models.TargetResource.objects.get_all()
        assert len(resources) == 0

        resources = ua_models.Resource.objects.get_all()
        assert len(resources) == 0

        assert svc.deleted_pre_called == instance_resources
        assert not svc.pre_create_called
        assert not svc.post_create_called
        assert not svc.create_instance_derivatives_called

    def test_actualize_updated_instances(self, dummy_instance_factory: tp.Callable):
        instance = dummy_instance_factory({"updated": 1})[0]

        resources = ua_models.TargetResource.objects.get_all()
        assert len(resources) == 1
        resource = resources[0]
        assert resource.uuid == instance.uuid
        assert resource.value["name"] != instance.name

        svc = DummyBuilder(instance_model=conftest.DummyInstance)
        svc._iteration()

        resources = ua_models.TargetResource.objects.get_all()
        assert len(resources) == 1

        resource = resources[0]
        assert resource.uuid == instance.uuid
        assert resource.value["name"] == instance.name
        assert resource.kind == conftest.DummyInstance.get_resource_kind()

        resources = ua_models.Resource.objects.get_all()
        assert len(resources) == 0

        assert not svc.pre_create_called
        assert not svc.post_create_called
        assert not svc.create_instance_derivatives_called
        assert svc.pre_update_called
        assert svc.post_update_called

    def test_actualize_updated_multiple_instances(
        self, dummy_instance_factory: tp.Callable
    ):
        instances = dummy_instance_factory({"updated": 2})

        resources = ua_models.TargetResource.objects.get_all()
        assert len(resources) == 2
        assert {r.uuid for r in resources} == {i.uuid for i in instances}
        assert not ({r.value["name"] for r in resources} & {i.name for i in instances})

        svc = DummyBuilder(instance_model=conftest.DummyInstance)
        svc._iteration()

        resources = ua_models.TargetResource.objects.get_all()
        assert len(resources) == 2
        assert {r.uuid for r in resources} == {i.uuid for i in instances}
        assert {r.value["name"] for r in resources} == {i.name for i in instances}

        resources = ua_models.Resource.objects.get_all()
        assert len(resources) == 0

        assert not svc.pre_create_called
        assert not svc.post_create_called
        assert not svc.create_instance_derivatives_called
        assert svc.pre_update_called
        assert svc.post_update_called

    def test_actualize_outdated_instances(self, dummy_instance_factory: tp.Callable):
        class Builder(DummyBuilder):
            actualized = False

            def actualize_outdated_instance(
                self,
                current_instance,
                actual_instance,
            ) -> None:
                self.__class__.actualized = True

        # Prepare: create an existing target resource (no derivatives)
        instance = dummy_instance_factory({"existing": 1})[0]

        conftest.DummyInstance.objects = mock.MagicMock()
        conftest.DummyInstance.objects.get_all = mock.MagicMock(return_value=[instance])

        # Sanity: only target resource exists, no actual resources yet
        t_resources = ua_models.TargetResource.objects.get_all()
        a_resources = ua_models.Resource.objects.get_all()
        assert len(t_resources) == 1
        assert len(a_resources) == 0

        # Create an actual resource with a different value/status to mark it as outdated
        target = t_resources[0]
        assert target.kind == conftest.DummyInstance.get_resource_kind()

        actual_value = instance.dump_to_simple_view()
        actual = ua_models.Resource.from_value(
            value=actual_value,
            kind=target.kind,
        )
        actual.save()

        # Run actualization
        svc = Builder(instance_model=conftest.DummyInstance)
        svc._iteration()

        # Validate target resource is actualized from actual (status/full_hash)
        t_resources = ua_models.TargetResource.objects.get_all()
        a_resources = ua_models.Resource.objects.get_all()
        assert len(t_resources) == 1
        assert len(a_resources) == 1

        target = t_resources[0]
        actual = a_resources[0]

        assert target.uuid == actual.uuid
        assert target.kind == actual.kind
        # full_hash is synced with actual; status follows actual
        assert target.full_hash == actual.full_hash
        assert target.status == actual.status

        assert Builder.actualized

    def test_skip_create_when_not_ready(self, dummy_instance_factory: tp.Callable):
        class DummyInstanceNotReadyCreate(
            conftest.DummyInstance, ua_models.ReadinessMixin
        ):
            def is_ready_to_create(self) -> bool:
                return False

        storage = {"new": [], "updated": [], "deleted": [], "existing": []}
        project_id = sys_uuid.uuid4()
        uuid = sys_uuid.uuid5(DummyInstanceNotReadyCreate.NAMESPACE, "new-0")
        inst = DummyInstanceNotReadyCreate(
            uuid=uuid, name="inst-new-0", project_id=project_id
        )
        storage["new"].append(inst)
        DummyInstanceNotReadyCreate.__dummy_storage__ = storage

        svc = DummyBuilder(instance_model=DummyInstanceNotReadyCreate)
        svc._iteration()

        resources = ua_models.TargetResource.objects.get_all()
        assert len(resources) == 0
        assert not svc.pre_create_called
        assert not svc.post_create_called
        assert not svc.create_instance_derivatives_called

    def test_skip_update_when_not_ready(self, dummy_instance_factory: tp.Callable):
        class DummyInstanceNotReadyUpdate(
            conftest.DummyInstance, ua_models.ReadinessMixin
        ):
            def is_ready_to_update(self) -> bool:
                return False

        # Prepare storage for the subclass
        storage = {"new": [], "updated": [], "deleted": [], "existing": []}
        project_id = sys_uuid.uuid4()
        uuid = sys_uuid.uuid5(DummyInstanceNotReadyUpdate.NAMESPACE, "updated-0")
        instance = DummyInstanceNotReadyUpdate(
            uuid=uuid, name="inst-updated-0", project_id=project_id
        )
        resource = instance.to_ua_resource()
        instance.name = f"{instance.name}-updated"
        resource.save()
        storage["updated"].append(instance)
        DummyInstanceNotReadyUpdate.__dummy_storage__ = storage

        svc = DummyBuilder(instance_model=DummyInstanceNotReadyUpdate)
        svc._iteration()

        resources = ua_models.TargetResource.objects.get_all()
        assert len(resources) == 1
        target = resources[0]
        assert target.value["name"] != instance.name
        assert not svc.pre_update_called
        assert not svc.post_update_called

        # No new actual resources should appear
        actual_resources = ua_models.Resource.objects.get_all()
        assert len(actual_resources) == 0

    def test_skip_actualize_outdated_when_not_ready(
        self, dummy_instance_factory: tp.Callable
    ):
        class DummyInstanceNotReadyActualize(
            conftest.DummyInstance, ua_models.ReadinessMixin
        ):
            def is_ready_to_actualize(self) -> bool:
                return False

        class Builder(DummyBuilder):
            actualized = False

            def actualize_outdated_instance(self, current_instance, actual_instance):
                self.__class__.actualized = True

        storage = {"new": [], "updated": [], "deleted": [], "existing": []}
        project_id = sys_uuid.uuid4()
        uuid = sys_uuid.uuid5(DummyInstanceNotReadyActualize.NAMESPACE, "existing-0")
        instance = DummyInstanceNotReadyActualize(
            uuid=uuid, name="inst-existing-0", project_id=project_id
        )
        instance._instance_model = DummyInstanceNotReadyActualize
        instance._instance_model.objects = mock.MagicMock()
        instance._instance_model.objects.get_all = mock.MagicMock(
            return_value=[instance]
        )
        target = instance.to_ua_resource()
        target.save()
        storage["existing"].append(instance)
        DummyInstanceNotReadyActualize.__dummy_storage__ = storage

        actual_value = instance.dump_to_simple_view()
        actual = ua_models.Resource.from_value(value=actual_value, kind=target.kind)
        actual.save()

        before_full_hash = target.full_hash
        before_status = target.status

        svc = Builder(instance_model=DummyInstanceNotReadyActualize)
        svc._iteration()

        t_resources = ua_models.TargetResource.objects.get_all()
        a_resources = ua_models.Resource.objects.get_all()
        assert len(t_resources) == 1
        assert len(a_resources) == 1

        target = t_resources[0]
        assert target.full_hash == before_full_hash
        assert target.status == before_status
        assert not Builder.actualized

    def test_skip_delete_when_not_ready(self, dummy_instance_factory: tp.Callable):
        class DummyInstanceNotReadyDelete(
            conftest.DummyInstance, ua_models.ReadinessMixin
        ):
            def is_ready_to_delete(self) -> bool:
                return False

        storage = {"new": [], "updated": [], "deleted": [], "existing": []}
        project_id = sys_uuid.uuid4()
        uuid = sys_uuid.uuid5(DummyInstanceNotReadyDelete.NAMESPACE, "deleted-0")
        instance = DummyInstanceNotReadyDelete(
            uuid=uuid, name="inst-deleted-0", project_id=project_id
        )
        resource = instance.to_ua_resource()
        resource.save()
        storage["deleted"].append(resource)
        DummyInstanceNotReadyDelete.__dummy_storage__ = storage

        svc = DummyBuilder(instance_model=DummyInstanceNotReadyDelete)
        svc._iteration()

        resources = ua_models.TargetResource.objects.get_all()
        assert len(resources) == 1
        assert resources[0].uuid == resource.uuid
        assert svc.deleted_pre_called == []

    def test_actualize_outdated_instances_failed(
        self, dummy_instance_factory: tp.Callable
    ):
        class Builder(DummyBuilder):
            called = 0

            def actualize_outdated_instance(
                self,
                current_instance,
                actual_instance,
            ) -> None:
                self.__class__.called += 1
                raise RuntimeError("Failed to actualize instance")

        # Prepare: create an existing target resource (no derivatives)
        instance = dummy_instance_factory({"existing": 1})[0]

        conftest.DummyInstance.objects = mock.MagicMock()
        conftest.DummyInstance.objects.get_all = mock.MagicMock(return_value=[instance])

        # Sanity: only target resource exists, no actual resources yet
        t_resources = ua_models.TargetResource.objects.get_all()
        a_resources = ua_models.Resource.objects.get_all()
        assert len(t_resources) == 1
        assert len(a_resources) == 0

        # Create an actual resource with a different value/status to mark it as outdated
        target = t_resources[0]
        assert target.kind == conftest.DummyInstance.get_resource_kind()

        actual_value = instance.dump_to_simple_view()
        actual = ua_models.Resource.from_value(
            value=actual_value,
            kind=target.kind,
        )
        actual.save()

        # Run actualization
        svc = Builder(instance_model=conftest.DummyInstance)
        svc._iteration()

        # Validate target resource is actualized from actual (status/full_hash)
        t_resources = ua_models.TargetResource.objects.get_all()
        a_resources = ua_models.Resource.objects.get_all()
        assert len(t_resources) == 1
        assert len(a_resources) == 1

        target = t_resources[0]
        actual = a_resources[0]

        assert target.uuid == actual.uuid
        assert target.kind == actual.kind
        # full_hash is synced with actual; status follows actual
        assert target.full_hash != actual.full_hash

        assert Builder.called == 1

        svc._iteration()

        t_resources = ua_models.TargetResource.objects.get_all()
        a_resources = ua_models.Resource.objects.get_all()
        assert len(t_resources) == 1
        assert len(a_resources) == 1

        target = t_resources[0]
        actual = a_resources[0]

        assert target.uuid == actual.uuid
        assert target.kind == actual.kind
        # full_hash is synced with actual; status follows actual
        assert target.full_hash != actual.full_hash
        assert Builder.called == 2

    # Derivatives

    def test_create_new_instance_derivatives(
        self, dummy_instance_with_derivatives_factory: tp.Callable
    ):
        instance = dummy_instance_with_derivatives_factory({"new": 1})[0]

        svc = DummyBuilderWithDerivatives(
            instance_model=conftest.DummyInstanceWithDerivatives
        )
        svc._iteration()

        resources = ua_models.TargetResource.objects.get_all()
        assert len(resources) == 2
        assert {r.kind for r in resources} == {"foo", "foo-derivative"}
        assert {r.uuid for r in resources} == {
            instance.uuid,
            sys_uuid.uuid5(instance.uuid, instance.name),
        }

        resources = ua_models.Resource.objects.get_all()
        assert len(resources) == 0

        assert svc.pre_create_called
        assert svc.post_create_called
        assert svc.create_instance_derivatives_called

    def test_create_multiple_instance_derivatives(
        self, dummy_instance_with_derivatives_factory: tp.Callable
    ):
        dummy_instance_with_derivatives_factory({"new": 2})

        svc = DummyBuilderWithDerivatives(
            instance_model=conftest.DummyInstanceWithDerivatives
        )
        svc._iteration()

        resources = ua_models.TargetResource.objects.get_all()
        assert len(resources) == 4

        assert {r.kind for r in resources} == {"foo", "foo-derivative"}

        resources = ua_models.Resource.objects.get_all()
        assert len(resources) == 0

        assert svc.pre_create_called
        assert svc.post_create_called
        assert svc.create_instance_derivatives_called

    def test_delete_instance_derivatives(
        self, dummy_instance_with_derivatives_factory: tp.Callable
    ):
        resource = dummy_instance_with_derivatives_factory({"deleted": 1})[0]

        svc = DummyBuilderWithDerivatives(
            instance_model=conftest.DummyInstanceWithDerivatives
        )
        svc._iteration()

        resources = ua_models.TargetResource.objects.get_all()
        assert len(resources) == 0

        resources = ua_models.Resource.objects.get_all()
        assert len(resources) == 0

        assert svc.deleted_pre_called[0] == resource
        assert not svc.pre_create_called
        assert not svc.post_create_called
        assert not svc.create_instance_derivatives_called

    def test_actualize_updated_instance_derivatives(
        self, dummy_instance_with_derivatives_factory: tp.Callable
    ):
        instance = dummy_instance_with_derivatives_factory({"updated": 1})[0]

        resources = ua_models.TargetResource.objects.get_all()
        derivative = next(r for r in resources if r.master == instance.uuid)

        assert len(resources) == 2
        assert derivative.master == instance.uuid
        assert {r.uuid for r in resources} == {
            instance.uuid,
            sys_uuid.uuid5(instance.uuid, "inst-updated-0"),
        }
        assert not ({r.value["name"] for r in resources} & {instance.name})

        svc = DummyBuilderWithDerivatives(
            instance_model=conftest.DummyInstanceWithDerivatives
        )
        svc._iteration()

        resources = ua_models.TargetResource.objects.get_all()
        assert len(resources) == 2

        assert {r.uuid for r in resources} == {
            instance.uuid,
            sys_uuid.uuid5(instance.uuid, instance.name),
        }
        assert {r.value["name"] for r in resources} == {instance.name}

        resources = ua_models.Resource.objects.get_all()
        assert len(resources) == 0

        assert not svc.pre_create_called
        assert not svc.post_create_called
        assert svc.create_instance_derivatives_called
        assert svc.pre_update_called
        assert svc.post_update_called

    def test_actualize_outdated_instances_derivatives(
        self, dummy_instance_with_derivatives_factory: tp.Callable
    ):
        class Builder(DummyBuilderWithDerivatives):
            actualized = False

            def actualize_outdated_instance_derivatives(
                self,
                instance,
                derivative_pairs,
            ):
                self.__class__.actualized = True
                assert len(derivative_pairs) == 1
                assert instance.get_resource_kind() == "foo"
                assert derivative_pairs[0][0].get_resource_kind() == "foo-derivative"

                return tuple(p[0] for p in derivative_pairs)

        # Prepare: create an existing target resource (no derivatives)
        instance = dummy_instance_with_derivatives_factory({"existing": 1})[0]

        conftest.DummyInstanceWithDerivatives.objects = mock.MagicMock()
        conftest.DummyInstanceWithDerivatives.objects.get_all = mock.MagicMock(
            return_value=[instance]
        )

        # Sanity: only target resource exists, no actual resources yet
        t_resources = ua_models.TargetResource.objects.get_all()
        a_resources = ua_models.Resource.objects.get_all()
        assert len(t_resources) == 2
        assert len(a_resources) == 0

        derivative = instance.to_derivative()
        actual_value = derivative.dump_to_simple_view()
        actual = ua_models.Resource.from_value(
            value=actual_value,
            kind=derivative.get_resource_kind(),
        )
        actual.save()

        # Run actualization
        svc = Builder(instance_model=conftest.DummyInstanceWithDerivatives)
        svc._iteration()

        # Validate target resource is actualized from actual (status/full_hash)
        t_resources = ua_models.TargetResource.objects.get_all()
        a_resources = ua_models.Resource.objects.get_all()
        assert len(t_resources) == 2
        assert len(a_resources) == 1

        target = next(r for r in t_resources if r.kind == "foo-derivative")
        actual = a_resources[0]

        assert target.uuid == actual.uuid
        assert target.kind == actual.kind
        # full_hash is synced with actual; status follows actual
        assert target.full_hash == actual.full_hash

        assert Builder.actualized

    def test_actualize_outdated_instances_derivatives_failed(
        self, dummy_instance_with_derivatives_factory: tp.Callable
    ):
        class Builder(DummyBuilderWithDerivatives):
            called = 0

            def actualize_outdated_instance_derivatives(
                self,
                instance,
                derivative_pairs,
            ):
                self.__class__.called += 1
                assert len(derivative_pairs) == 1
                assert instance.get_resource_kind() == "foo"
                assert derivative_pairs[0][0].get_resource_kind() == "foo-derivative"

                raise RuntimeError("Failed to actualize instance derivatives")

        # Prepare: create an existing target resource (no derivatives)
        instance = dummy_instance_with_derivatives_factory({"existing": 1})[0]

        conftest.DummyInstanceWithDerivatives.objects = mock.MagicMock()
        conftest.DummyInstanceWithDerivatives.objects.get_all = mock.MagicMock(
            return_value=[instance]
        )

        # Sanity: only target resource exists, no actual resources yet
        t_resources = ua_models.TargetResource.objects.get_all()
        a_resources = ua_models.Resource.objects.get_all()
        assert len(t_resources) == 2
        assert len(a_resources) == 0

        derivative = instance.to_derivative()
        actual_value = derivative.dump_to_simple_view()
        actual = ua_models.Resource.from_value(
            value=actual_value,
            kind=derivative.get_resource_kind(),
        )
        actual.save()

        # Run actualization
        svc = Builder(instance_model=conftest.DummyInstanceWithDerivatives)
        svc._iteration()

        # Validate target resource is actualized from actual (status/full_hash)
        t_resources = ua_models.TargetResource.objects.get_all()
        a_resources = ua_models.Resource.objects.get_all()
        assert len(t_resources) == 2
        assert len(a_resources) == 1

        target = next(r for r in t_resources if r.kind == "foo-derivative")
        actual = a_resources[0]

        assert target.uuid == actual.uuid
        assert target.kind == actual.kind
        # full_hash is synced with actual; status follows actual
        assert target.full_hash != actual.full_hash

        assert Builder.called == 1

        svc._iteration()

        t_resources = ua_models.TargetResource.objects.get_all()
        a_resources = ua_models.Resource.objects.get_all()
        target = next(r for r in t_resources if r.kind == "foo-derivative")
        actual = a_resources[0]

        assert target.full_hash != actual.full_hash
        assert Builder.called == 2

    # Master tracking

    def test_track_master_hash_instance(
        self, dummy_instance_with_derivatives_factory: tp.Callable
    ):
        class DummyBuilderWithMasterTracking(DummyBuilderWithDerivatives):
            _actualized = False

            def track_outdated_master_hash_instances(self) -> bool:
                """Track outdated master hash instances."""
                return True

            def actualize_outdated_master_hash_instance(
                self,
                instance,
                master_instance,
                derivatives,
            ) -> tp.Collection[ua_models.TargetResourceKindAwareMixin]:
                self.__class__._actualized = True
                master_kind = master_instance.get_resource_kind()
                instance_kind = instance.get_resource_kind()
                assert master_kind == "foo"
                assert instance_kind == "sub-foo"

                return tuple(p[0] for p in derivatives)

        instance = dummy_instance_with_derivatives_factory(
            {"existing": 1}, model=conftest.DummySubInstanceWithDerivatives
        )[0]

        svc = DummyBuilderWithMasterTracking(
            instance_model=conftest.DummySubInstanceWithDerivatives
        )
        svc._iteration()

        resources = ua_models.TargetResource.objects.get_all()
        assert len(resources) == 3

        instance_resource = next(r for r in resources if r.kind == "sub-foo")
        instance_resource.master_hash = "1111"
        instance_resource.save()

        svc._iteration()

        assert DummyBuilderWithMasterTracking._actualized

        resources = ua_models.TargetResource.objects.get_all()
        master = next(r for r in resources if r.kind == "foo")
        instance = next(r for r in resources if r.kind == "sub-foo")

        assert master.hash == instance.master_hash
        assert instance.full_hash != "1111"

    def test_track_master_full_hash_instance(
        self, dummy_instance_with_derivatives_factory: tp.Callable
    ):
        class DummyBuilderWithMasterTracking(DummyBuilderWithDerivatives):
            _actualized = False

            def track_outdated_master_full_hash_instances(self) -> bool:
                """Track outdated master hash instances."""
                return True

            def actualize_outdated_master_full_hash_instance(
                self,
                instance,
                master_instance,
                derivatives,
            ) -> tp.Collection[ua_models.TargetResourceKindAwareMixin]:
                self.__class__._actualized = True
                master_kind = master_instance.get_resource_kind()
                instance_kind = instance.get_resource_kind()
                assert master_kind == "foo"
                assert instance_kind == "sub-foo"

                return tuple(p[0] for p in derivatives)

        instance = dummy_instance_with_derivatives_factory(
            {"existing": 1}, model=conftest.DummySubInstanceWithDerivatives
        )[0]

        svc = DummyBuilderWithMasterTracking(
            instance_model=conftest.DummySubInstanceWithDerivatives
        )
        svc._iteration()

        resources = ua_models.TargetResource.objects.get_all()
        assert len(resources) == 3

        instance_resource = next(r for r in resources if r.kind == "sub-foo")
        instance_resource.master_full_hash = "1111"
        instance_resource.save()

        svc._iteration()

        assert DummyBuilderWithMasterTracking._actualized

        resources = ua_models.TargetResource.objects.get_all()
        master = next(r for r in resources if r.kind == "foo")
        instance = next(r for r in resources if r.kind == "sub-foo")

        assert master.full_hash == instance.master_full_hash
        assert instance.full_hash != "1111"

    def test_schedule_instance_on_create_when_schedulable(
        self,
        dummy_instance_factory: tp.Callable,
    ):
        class SchedulableDummyInstance(
            conftest.DummyInstance, ua_models.SchedulableToAgentMixin
        ):
            pass

        storage = {"new": [], "updated": [], "deleted": [], "existing": []}
        uuid = sys_uuid.uuid5(SchedulableDummyInstance.NAMESPACE, "new-0")

        # Prepare the agent for scheduling
        agent = ua_models.UniversalAgent(uuid=uuid, node=uuid, name="agent-0")
        agent.save()

        inst = SchedulableDummyInstance(uuid=uuid, name="inst-new-0")
        storage["new"].append(inst)
        SchedulableDummyInstance.__dummy_storage__ = storage

        svc = DummyBuilder(instance_model=SchedulableDummyInstance)
        svc._iteration()

        resources = ua_models.TargetResource.objects.get_all()
        assert len(resources) == 1
        r = resources[0]
        assert r.uuid == inst.uuid
        assert r.agent == inst.uuid

    def test_schedule_instance_and_derivative_on_create_when_schedulable(
        self,
    ):
        class SchedulableDummyDerivative(
            conftest.DummyDerivative, ua_models.SchedulableToAgentMixin
        ):
            pass

        class SchedulableInstanceWithDerivatives(
            conftest.DummyInstanceWithDerivatives,
            ua_models.SchedulableToAgentMixin,
        ):
            __derivative_model_map__ = {
                "foo-derivative": SchedulableDummyDerivative,
            }

            def to_derivative(self) -> SchedulableDummyDerivative:
                return SchedulableDummyDerivative(
                    uuid=sys_uuid.uuid5(self.uuid, self.name),
                    name=self.name,
                    project_id=self.project_id,
                )

        storage = {"new": [], "updated": [], "deleted": [], "existing": []}
        uuid = sys_uuid.uuid5(SchedulableInstanceWithDerivatives.NAMESPACE, "new-0")
        name = "inst-new-0"

        # Prepare the agents for scheduling
        agent = ua_models.UniversalAgent(uuid=uuid, node=uuid, name="agent-0")
        agent.save()

        agent = ua_models.UniversalAgent(
            uuid=sys_uuid.uuid5(uuid, name), node=uuid, name="agent-1"
        )
        agent.save()

        inst = SchedulableInstanceWithDerivatives(uuid=uuid, name=name)
        storage["new"].append(inst)
        SchedulableInstanceWithDerivatives.__dummy_storage__ = storage

        svc = DummyBuilderWithDerivatives(
            instance_model=SchedulableInstanceWithDerivatives
        )
        svc._iteration()

        resources = ua_models.TargetResource.objects.get_all()
        assert len(resources) == 2
        inst_res = next(r for r in resources if r.master is None)
        der_res = next(r for r in resources if r.master == inst_res.uuid)
        assert inst_res.agent == inst.uuid
        assert der_res.agent == sys_uuid.uuid5(inst.uuid, inst.name)

    def test_schedule_new_derivative_on_update_when_schedulable(self):
        class SchedulableDummyDerivative(
            conftest.DummyDerivative, ua_models.SchedulableToAgentMixin
        ):
            pass

        class SchedulableInstanceWithDerivatives(conftest.DummyInstanceWithDerivatives):
            __derivative_model_map__ = {
                "foo-derivative": SchedulableDummyDerivative,
            }

            def to_derivative(self) -> SchedulableDummyDerivative:
                return SchedulableDummyDerivative(
                    uuid=sys_uuid.uuid5(self.uuid, self.name),
                    name=self.name,
                    project_id=self.project_id,
                )

        storage = {"new": [], "updated": [], "deleted": [], "existing": []}
        uuid = sys_uuid.uuid5(SchedulableInstanceWithDerivatives.NAMESPACE, "updated-0")
        name = "inst-updated-0"

        inst = SchedulableInstanceWithDerivatives(uuid=uuid, name=name)

        base_resource = inst.to_ua_resource()
        base_resource.save()
        # existing derivative with old name
        old_der = SchedulableDummyDerivative(
            uuid=sys_uuid.uuid5(inst.uuid, base_resource.value["name"]),
            name=base_resource.value["name"],
            project_id=inst.project_id,
        )
        old_der_res = old_der.to_ua_resource(
            master=base_resource.uuid,
            master_hash=base_resource.hash,
            master_full_hash=base_resource.full_hash,
        )
        old_der_res.save()

        # Prepare the agents for scheduling
        agent = ua_models.UniversalAgent(
            uuid=sys_uuid.uuid5(uuid, f"{name}-updated"),
            node=uuid,
            name="agent-0",
        )
        agent.save()

        inst.name = f"{name}-updated"
        storage["updated"].append(inst)
        SchedulableInstanceWithDerivatives.__dummy_storage__ = storage

        svc = DummyBuilderWithDerivatives(
            instance_model=SchedulableInstanceWithDerivatives
        )
        svc._iteration()

        resources = ua_models.TargetResource.objects.get_all()
        inst_res = next(r for r in resources if r.uuid == inst.uuid)
        new_der_uuid = sys_uuid.uuid5(inst.uuid, inst.name)
        der_res = next(
            r for r in resources if r.master == inst_res.uuid and r.uuid == new_der_uuid
        )
        assert der_res.agent == new_der_uuid
