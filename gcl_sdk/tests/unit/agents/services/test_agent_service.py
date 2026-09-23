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


def _make_service(
    *,
    caps_drivers=None,
    facts_drivers=None,
    orch_client=None,
    payload_path=None,
) -> UniversalAgentService:
    """Build a real service instance with mocked I/O dependencies.

    The service methods under test (``_cap_driver_iteration``,
    ``_capability_iteration``, ``_actualize_facts``, ``_iteration``) are
    real so that exceptions propagate through the reconciliation logic
    exactly as in production; only the orchestrator client and the
    drivers are mocks.
    """
    return UniversalAgentService(
        agent_uuid=sys_uuid.uuid4(),
        orch_client=orch_client or MagicMock(),
        caps_drivers=caps_drivers or [],
        facts_drivers=facts_drivers or [],
        payload_path=payload_path,
        system_uuid=sys_uuid.uuid4(),
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


class TestReadFailureProtection:
    """A failed read (``driver.list()`` raising) must never be confused
    with a successful empty read.

    The failed category must be excluded from reconciliation so its last
    known snapshot is preserved and no resources are deleted in the
    Status API. These tests guard the three-state semantics
    (success-with-resources / success-empty / failed-unknown) introduced
    to fix the Orion incident where a transient ``driver.list()`` failure
    caused a mass ``resources_delete`` of the previously exported facts.
    """

    def test_cap_driver_iteration_excludes_capability_on_list_failure(self):
        resource = _make_resource()
        payload = models.Payload.empty()
        payload.add_caps_resources([resource])
        payload.calculate_hash()

        collected_payload = models.Payload.empty()

        driver = MagicMock()
        driver.get_capabilities.return_value = [KIND]
        driver.list.side_effect = RuntimeError("simulated DB outage")

        agent = _make_service()

        result = agent._cap_driver_iteration(driver, payload, collected_payload)

        # The failing capability must not be reported as processed...
        assert result == set()
        # ...and nothing must have been collected for it.
        assert collected_payload.caps_resources() == []
        assert collected_payload.facts_resources() == []

    def test_cap_driver_iteration_reports_capability_on_success(self):
        resource = _make_resource()
        payload = models.Payload.empty()
        payload.add_caps_resources([resource])
        payload.calculate_hash()

        collected_payload = models.Payload.empty()

        driver = MagicMock()
        driver.get_capabilities.return_value = [KIND]
        driver.list.return_value = [resource]

        agent = _make_service()

        result = agent._cap_driver_iteration(driver, payload, collected_payload)

        assert result == {KIND}
        assert collected_payload.caps_resources(KIND) == [resource]

    def test_iteration_no_delete_when_capability_list_raises(self):
        """The Orion repro: a read failure must not delete exported facts."""
        resources = [_make_resource(uuid=sys_uuid.uuid4()) for _ in range(3)]

        payload = models.Payload.empty()
        payload.add_caps_resources(resources)
        payload.add_facts_resources(resources)
        payload.calculate_hash()

        driver = MagicMock()
        driver.get_capabilities.return_value = [KIND]
        driver.list.side_effect = RuntimeError("simulated DB outage")

        orch = MagicMock()
        orch.agents_get_payload.return_value = payload

        agent = _make_service(caps_drivers=[driver], orch_client=orch)
        agent._iteration()

        # Zero create/update/delete for the failing category.
        orch.resources_delete.assert_not_called()
        orch.resources_create.assert_not_called()
        orch.resources_update.assert_not_called()
        # The read was attempted exactly once.
        driver.list.assert_called_once_with(KIND)

    def test_iteration_no_delete_when_fact_driver_list_raises(self):
        """The same protection applies to plain fact drivers."""
        resources = [_make_resource(uuid=sys_uuid.uuid4()) for _ in range(3)]

        payload = models.Payload.empty()
        payload.add_facts_resources(resources)
        payload.calculate_hash()

        fact_driver = MagicMock()
        fact_driver.get_facts.return_value = [KIND]
        fact_driver.list.side_effect = RuntimeError("simulated DB outage")

        orch = MagicMock()
        orch.agents_get_payload.return_value = payload

        agent = _make_service(facts_drivers=[fact_driver], orch_client=orch)
        agent._iteration()

        orch.resources_delete.assert_not_called()
        orch.resources_create.assert_not_called()
        orch.resources_update.assert_not_called()

    def test_iteration_does_not_save_payload_on_list_failure(self, tmp_path):
        """A failed read must not overwrite the last successful snapshot."""
        payload_path = tmp_path / "payload.json"

        # Seed a non-empty last-known-good snapshot on disk. It must differ
        # from the empty collected payload so an overwrite is detectable.
        last_resources = [_make_resource(uuid=sys_uuid.uuid4()) for _ in range(3)]
        last_payload = models.Payload.empty()
        last_payload.add_caps_resources(last_resources)
        last_payload.add_facts_resources(last_resources)
        last_payload.calculate_hash()
        last_payload.save(str(payload_path))
        original_content = payload_path.read_text()

        resources = [_make_resource(uuid=sys_uuid.uuid4()) for _ in range(3)]
        payload = models.Payload.empty()
        payload.add_caps_resources(resources)
        payload.add_facts_resources(resources)
        payload.calculate_hash()

        driver = MagicMock()
        driver.get_capabilities.return_value = [KIND]
        driver.list.side_effect = RuntimeError("simulated DB outage")

        orch = MagicMock()
        orch.agents_get_payload.return_value = payload

        agent = _make_service(
            caps_drivers=[driver], orch_client=orch, payload_path=str(payload_path)
        )
        agent._iteration()

        # The on-disk snapshot is preserved verbatim.
        assert payload_path.read_text() == original_content

    def test_iteration_saves_payload_when_all_caps_succeed(self, tmp_path):
        """A fully successful iteration still persists the snapshot."""
        payload_path = tmp_path / "payload.json"
        assert not payload_path.exists()

        resources = [_make_resource(uuid=sys_uuid.uuid4()) for _ in range(3)]
        payload = models.Payload.empty()
        payload.add_caps_resources(resources)
        payload.add_facts_resources(resources)
        payload.calculate_hash()

        driver = MagicMock()
        driver.get_capabilities.return_value = [KIND]
        # Successful read returning the very same resources: no reconciliation
        # work, but the category is processed and the snapshot is saved.
        driver.list.return_value = resources

        orch = MagicMock()
        orch.agents_get_payload.return_value = payload

        agent = _make_service(
            caps_drivers=[driver], orch_client=orch, payload_path=str(payload_path)
        )
        agent._iteration()

        assert payload_path.exists()

    def test_iteration_saves_payload_when_payload_unchanged(self, tmp_path):
        """The CP answers a matching hash with an empty payload.

        Every capability is skipped then, which is not a failure. The
        snapshot must be saved so its hash changes and the next iteration
        gets the full payload and polls the data plane again. Otherwise the
        hash stays matched forever and data plane changes are never seen.
        """
        payload_path = tmp_path / "payload.json"

        resources = [_make_resource(uuid=sys_uuid.uuid4()) for _ in range(3)]
        last_payload = models.Payload.empty()
        last_payload.add_caps_resources(resources)
        last_payload.add_facts_resources(resources)
        last_payload.save(str(payload_path))

        # What ``UniversalAgent.get_payload`` returns on a hash match
        unchanged = models.Payload(hash=last_payload.hash, version=last_payload.version)

        driver = MagicMock()
        driver.get_capabilities.return_value = [KIND]

        orch = MagicMock()
        orch.agents_get_payload.return_value = unchanged

        agent = _make_service(
            caps_drivers=[driver], orch_client=orch, payload_path=str(payload_path)
        )
        agent._iteration()

        driver.list.assert_not_called()
        orch.resources_delete.assert_not_called()
        saved = models.Payload.load(str(payload_path))
        assert saved.hash != last_payload.hash

    def test_iteration_saves_payload_when_capability_not_in_payload(self, tmp_path):
        """A driver capability absent from the payload doesn't block saving."""
        payload_path = tmp_path / "payload.json"

        resources = [_make_resource(uuid=sys_uuid.uuid4()) for _ in range(3)]
        payload = models.Payload.empty()
        payload.add_caps_resources(resources)
        payload.add_facts_resources(resources)
        payload.calculate_hash()

        driver = MagicMock()
        driver.get_capabilities.return_value = [KIND, CAPABILITY]
        driver.list.return_value = resources

        orch = MagicMock()
        orch.agents_get_payload.return_value = payload

        agent = _make_service(
            caps_drivers=[driver], orch_client=orch, payload_path=str(payload_path)
        )
        agent._iteration()

        driver.list.assert_called_once_with(KIND)
        assert payload_path.exists()

    def test_actualize_facts_skips_unprocessed_deleted_category(self):
        """A category absent from ``processed_capabilities`` is left alone."""
        resource = _make_resource()
        resource_dict = resource.dump_to_simple_view()

        target_facts = {}
        actual_facts = {KIND: {"resources": [resource_dict]}}

        orch = MagicMock()
        agent = _make_service(orch_client=orch)

        agent._actualize_facts(target_facts, actual_facts, processed_capabilities=set())

        orch.resources_delete.assert_not_called()
        orch.resources_create.assert_not_called()
        orch.resources_update.assert_not_called()

    def test_actualize_facts_deletes_processed_deleted_category(self):
        """A processed category with a legitimately empty data plane is deleted."""
        resource = _make_resource()
        resource_dict = resource.dump_to_simple_view()

        target_facts = {}
        actual_facts = {KIND: {"resources": [resource_dict]}}

        orch = MagicMock()
        agent = _make_service(orch_client=orch)

        agent._actualize_facts(
            target_facts, actual_facts, processed_capabilities={KIND}
        )

        orch.resources_delete.assert_called_once()

    def test_actualize_facts_skips_unprocessed_new_category(self):
        """A new category that failed to read must not be created either."""
        resource = _make_resource()
        resource_dict = resource.dump_to_simple_view()

        target_facts = {KIND: {"resources": [resource_dict]}}
        actual_facts = {}

        orch = MagicMock()
        agent = _make_service(orch_client=orch)

        agent._actualize_facts(target_facts, actual_facts, processed_capabilities=set())

        orch.resources_create.assert_not_called()

    def test_actualize_facts_skips_unprocessed_existing_category(self):
        """An existing category that failed to read is not reconciled."""
        target_resource = _make_resource()
        actual_resource = _make_resource()

        target_facts = {KIND: {"resources": [target_resource.dump_to_simple_view()]}}
        actual_facts = {KIND: {"resources": [actual_resource.dump_to_simple_view()]}}

        orch = MagicMock()
        agent = _make_service(orch_client=orch)

        agent._actualize_facts(target_facts, actual_facts, processed_capabilities=set())

        orch.resources_create.assert_not_called()
        orch.resources_delete.assert_not_called()
        orch.resources_update.assert_not_called()

    def test_partial_failure_preserves_only_failed_category(self):
        """A successful category is reconciled while a failed one is skipped."""
        ok_kind = "ok_kind"
        fail_kind = "fail_kind"

        ok_resources = [_make_resource(uuid=sys_uuid.uuid4(), kind=ok_kind)]
        fail_resources = [_make_resource(uuid=sys_uuid.uuid4(), kind=fail_kind)]

        payload = models.Payload.empty()
        payload.add_caps_resources(ok_resources)
        payload.add_caps_resources(fail_resources)
        payload.add_facts_resources(fail_resources)
        payload.calculate_hash()

        driver = MagicMock()
        driver.get_capabilities.return_value = [ok_kind, fail_kind]
        driver.dependent_capabilities.return_value = False

        def list_side_effect(capability):
            if capability == fail_kind:
                raise RuntimeError("simulated DB outage")
            return ok_resources

        driver.list.side_effect = list_side_effect

        orch = MagicMock()
        orch.agents_get_payload.return_value = payload

        agent = _make_service(caps_drivers=[driver], orch_client=orch)
        agent._iteration()

        created_resource = orch.resources_create.call_args.args[0]
        assert created_resource.uuid == ok_resources[0].uuid
        orch.resources_delete.assert_not_called()

    def test_dependent_driver_failure_invalidates_successful_capabilities(self):
        ok_kind = "ok_kind"
        fail_kind = "fail_kind"
        ok_resources = [_make_resource(kind=ok_kind)]

        payload = models.Payload.empty()
        payload.add_caps_resources(ok_resources)
        payload.capabilities[fail_kind] = {"resources": []}

        driver = MagicMock()
        driver.get_capabilities.return_value = [ok_kind, fail_kind]
        driver.dependent_capabilities.return_value = True
        driver.list.side_effect = [
            ok_resources,
            RuntimeError("simulated DB outage"),
        ]

        agent = _make_service()
        collected_payload = models.Payload.empty()

        result = agent._cap_driver_iteration(driver, payload, collected_payload)

        assert result == set()
