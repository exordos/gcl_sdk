from __future__ import annotations

from unittest.mock import MagicMock
import uuid as sys_uuid

from gcl_sdk.agents.universal.dm import models
from gcl_sdk.agents.universal.services.agent import UniversalAgentService

KIND = "test_kind"
CAPABILITY = "test_capability"


def _make_resource(
    value: dict | None = None,
    *,
    uuid: sys_uuid.UUID | None = None,
    kind: str = KIND,
) -> models.Resource:
    uuid = uuid or sys_uuid.uuid4()
    value = value or {"uuid": str(uuid), "name": "test"}
    return models.Resource.from_value(
        value, kind, target_fields=frozenset(value.keys())
    )


class TestActualizeCapability:
    def test_creates_new_resources(self):
        target_resource = _make_resource()
        driver = MagicMock()
        driver.list.return_value = []

        service = MagicMock()
        service._create_resource.return_value = target_resource

        result = UniversalAgentService._actualize_capability(
            service, driver, CAPABILITY, [target_resource]
        )

        assert result == [target_resource]
        service._create_resource.assert_called_once_with(driver, target_resource)
        driver.list.assert_called_once_with(CAPABILITY)

    def test_deletes_removed_resources(self):
        actual_resource = _make_resource()
        driver = MagicMock()
        driver.list.return_value = [actual_resource]

        service = MagicMock()

        result = UniversalAgentService._actualize_capability(
            service, driver, CAPABILITY, []
        )

        assert result == []
        service._delete_resource.assert_called_once_with(driver, actual_resource)

    def test_skips_unchanged_resources(self):
        uuid = sys_uuid.uuid4()
        value = {"uuid": str(uuid), "name": "test"}
        target = models.Resource.from_value(
            value, KIND, target_fields=frozenset(value.keys())
        )
        actual = models.Resource.from_value(
            value, KIND, target_fields=frozenset(value.keys())
        )

        driver = MagicMock()
        driver.list.return_value = [actual]

        service = MagicMock()

        result = UniversalAgentService._actualize_capability(
            service, driver, CAPABILITY, [target]
        )

        assert result == [actual]
        service._create_resource.assert_not_called()
        service._delete_resource.assert_not_called()
        service._update_resource.assert_not_called()

    def test_updates_changed_resources(self):
        uuid = sys_uuid.uuid4()
        target = models.Resource.from_value(
            {"uuid": str(uuid), "name": "old"},
            KIND,
            target_fields=frozenset({"uuid", "name"}),
        )
        updated = models.Resource.from_value(
            {"uuid": str(uuid), "name": "new"},
            KIND,
            target_fields=frozenset({"uuid", "name"}),
        )

        driver = MagicMock()
        driver.list.return_value = [target]

        service = MagicMock()
        service._update_resource.return_value = updated

        result = UniversalAgentService._actualize_capability(
            service, driver, CAPABILITY, [updated]
        )

        assert result == [updated]
        service._update_resource.assert_called_once_with(driver, updated)

    def test_create_exception_does_not_propagate(self):
        target_resource = _make_resource()
        driver = MagicMock()
        driver.list.return_value = []

        service = MagicMock()
        service._create_resource.side_effect = Exception("create failed")

        result = UniversalAgentService._actualize_capability(
            service, driver, CAPABILITY, [target_resource]
        )

        assert result == []

    def test_delete_exception_adds_resource_back(self):
        actual_resource = _make_resource()
        driver = MagicMock()
        driver.list.return_value = [actual_resource]

        service = MagicMock()
        service._delete_resource.side_effect = Exception("delete failed")

        result = UniversalAgentService._actualize_capability(
            service, driver, CAPABILITY, []
        )

        assert result == [actual_resource]

    def test_update_exception_does_not_propagate(self):
        uuid = sys_uuid.uuid4()
        target = models.Resource.from_value(
            {"uuid": str(uuid), "name": "old"},
            KIND,
            target_fields=frozenset({"uuid", "name"}),
        )
        updated = models.Resource.from_value(
            {"uuid": str(uuid), "name": "new"},
            KIND,
            target_fields=frozenset({"uuid", "name"}),
        )

        driver = MagicMock()
        driver.list.return_value = [target]

        service = MagicMock()
        service._update_resource.side_effect = Exception("update failed")

        result = UniversalAgentService._actualize_capability(
            service, driver, CAPABILITY, [updated]
        )

        assert result == []

    def test_empty_lists_returns_empty_list(self):
        driver = MagicMock()
        driver.list.return_value = []

        service = MagicMock()

        result = UniversalAgentService._actualize_capability(
            service, driver, CAPABILITY, []
        )

        assert result == []
        driver.list.assert_called_once_with(CAPABILITY)

    def test_mixed_operations(self):
        uuid_new = sys_uuid.uuid4()
        uuid_removed = sys_uuid.uuid4()
        uuid_unchanged = sys_uuid.uuid4()
        uuid_changed = sys_uuid.uuid4()

        new_resource = _make_resource(uuid=uuid_new)
        removed_resource = _make_resource(uuid=uuid_removed)

        unchanged_value = {"uuid": str(uuid_unchanged), "name": "same"}
        unchanged_target = models.Resource.from_value(
            unchanged_value, KIND, target_fields=frozenset(unchanged_value.keys())
        )
        unchanged_actual = models.Resource.from_value(
            unchanged_value, KIND, target_fields=frozenset(unchanged_value.keys())
        )

        changed_target = models.Resource.from_value(
            {"uuid": str(uuid_changed), "name": "before"},
            KIND,
            target_fields=frozenset({"uuid", "name"}),
        )
        changed_updated = models.Resource.from_value(
            {"uuid": str(uuid_changed), "name": "after"},
            KIND,
            target_fields=frozenset({"uuid", "name"}),
        )

        driver = MagicMock()
        driver.list.return_value = [
            removed_resource,
            unchanged_actual,
            changed_target,
        ]

        service = MagicMock()
        service._create_resource.return_value = new_resource
        service._update_resource.return_value = changed_updated

        result = UniversalAgentService._actualize_capability(
            service,
            driver,
            CAPABILITY,
            [new_resource, unchanged_target, changed_updated],
        )

        service._create_resource.assert_called_once_with(driver, new_resource)
        service._delete_resource.assert_called_once_with(driver, removed_resource)
        service._update_resource.assert_called_once_with(driver, changed_updated)

        assert new_resource in result
        assert unchanged_actual in result
        assert changed_updated in result
        assert removed_resource not in result
        assert len(result) == 3
