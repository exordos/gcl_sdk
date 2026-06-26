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

import os
import tempfile
import typing as tp
from unittest import mock
import uuid as sys_uuid

from gcl_sdk.agents.universal import utils
from gcl_sdk.agents.universal.clients.orch import exceptions as orch_exc
from gcl_sdk.agents.universal.dm import models
from gcl_sdk.agents.universal.drivers import base
from gcl_sdk.agents.universal.services import agent as agent_svc
from gcl_sdk.tests.functional import conftest

MACHINE_UUID = sys_uuid.UUID("44b5857b-c15d-47f2-bed1-00fecd137208")


class FooCapDriver(base.AbstractCapabilityDriver):
    create_called = False
    delete_called = False
    update_called = False
    list_called = False
    get_called = False

    def get_capabilities(self) -> tp.List[str]:
        return ["foo"]

    def get(self, resource: models.Resource) -> models.Resource:
        self.__class__.get_called = True
        return resource

    def create(self, resource: models.Resource) -> models.Resource:
        self.__class__.create_called = True
        return resource

    def update(self, resource: models.Resource) -> models.Resource:
        self.__class__.update_called = True
        return resource

    def list(self, capability: str) -> tp.List[models.Resource]:
        self.__class__.list_called = True
        return []

    def delete(self, resource: models.Resource) -> None:
        self.__class__.delete_called = True
        pass


class FooFactDriver(base.AbstractFactDriver):
    get_called = False
    list_called = False

    def get_facts(self) -> tp.List[str]:
        return ["foo"]

    def get(self, resource: models.Resource) -> models.Resource:
        self.__class__.get_called = True
        return resource

    def list(self, fact: str) -> tp.List[models.Resource]:
        self.__class__.list_called = True
        return []


@mock.patch.object(
    utils,
    "system_uuid",
    lambda: MACHINE_UUID,
)
class TestUniversalAgent:
    PAYLOAD_PATH = os.path.join(tempfile.gettempdir(), "___payload___.json")

    def setup_method(self) -> None:
        # Run service
        self.orch_client = mock.MagicMock()
        self.agent_uuid = MACHINE_UUID

    def teardown_method(self) -> None:
        if os.path.exists(self.PAYLOAD_PATH):
            os.remove(self.PAYLOAD_PATH)

    def test_agent_registration(self):
        # Simulate agent not found on orchestrator
        self.orch_client.agents_get_payload = mock.MagicMock(
            side_effect=orch_exc.AgentNotFound(uuid=self.agent_uuid)
        )

        agent = agent_svc.UniversalAgentService(
            agent_uuid=self.agent_uuid,
            orch_client=self.orch_client,
            caps_drivers=[],
            facts_drivers=[],
        )

        agent._iteration()

        self.orch_client.agents_create.assert_called_once()

    def test_agent_updates_capabilities_and_facts_on_start(self):
        # Simulate that agent already exists on orchestrator
        self.orch_client.agents_create = mock.MagicMock(
            side_effect=orch_exc.AgentAlreadyExists(uuid=self.agent_uuid)
        )
        self.orch_client.agents_update = mock.MagicMock()

        agent = agent_svc.UniversalAgentService(
            agent_uuid=self.agent_uuid,
            orch_client=self.orch_client,
            caps_drivers=[FooCapDriver()],
            facts_drivers=[FooFactDriver()],
        )

        # Startup path should register and then update capabilities and facts
        agent._setup()

        # Ensure create attempted and then update called with agent containing
        # capabilities and facts derived from drivers
        self.orch_client.agents_create.assert_called_once()
        self.orch_client.agents_update.assert_called_once()

        updated_agent = self.orch_client.agents_update.call_args.args[0]
        assert isinstance(updated_agent, models.UniversalAgent)
        assert set(updated_agent.capabilities["capabilities"]) == {"foo"}
        assert set(updated_agent.facts["facts"]) == {"foo"}

    def test_agent_registration_node_not_found(self):
        """Test that NodeNotFound exception is handled and logged during registration."""
        self.orch_client.agents_create = mock.MagicMock(
            side_effect=orch_exc.NodeNotFound(uuid=self.agent_uuid)
        )

        agent = agent_svc.UniversalAgentService(
            agent_uuid=self.agent_uuid,
            orch_client=self.orch_client,
            caps_drivers=[],
            facts_drivers=[],
            verify_node_on_register=True,
        )

        # Should not raise, just log warning
        agent._setup()

        self.orch_client.agents_create.assert_called_once()
        # Verify check_node_exists was passed as True
        call_kwargs = self.orch_client.agents_create.call_args.kwargs
        assert call_kwargs.get("check_node_exists") is True

    def test_agent_registration_skip_node_verify(self):
        """Test that node verification can be skipped."""
        self.orch_client.agents_create = mock.MagicMock(return_value=None)

        agent = agent_svc.UniversalAgentService(
            agent_uuid=self.agent_uuid,
            orch_client=self.orch_client,
            caps_drivers=[],
            facts_drivers=[],
            verify_node_on_register=False,
        )

        agent._setup()

        self.orch_client.agents_create.assert_called_once()
        # Verify check_node_exists was passed as False
        call_kwargs = self.orch_client.agents_create.call_args.kwargs
        assert call_kwargs.get("check_node_exists") is False

    def test_agent_new_capability(self):
        uuid = sys_uuid.uuid4()
        resource = conftest.FooResource(uuid=uuid)
        payload = models.Payload.empty()
        payload.add_caps_resource(resource.to_ua_resource(kind="foo"))
        payload.calculate_hash()

        self.orch_client.agents_get_payload = mock.MagicMock(return_value=payload)

        class CapDriver(FooCapDriver):
            def create(self, resource):
                assert resource.uuid == uuid
                return super().create(resource)

        agent = agent_svc.UniversalAgentService(
            agent_uuid=self.agent_uuid,
            orch_client=self.orch_client,
            caps_drivers=[CapDriver()],
            facts_drivers=[],
            payload_path=self.PAYLOAD_PATH,
        )

        agent._iteration()

        assert CapDriver.create_called
        assert not CapDriver.delete_called
        assert not CapDriver.update_called
        self.orch_client.agents_get_payload.assert_called_once()
        # A fact for created capability should be created
        # in Status via orch client
        self.orch_client.resources_create.assert_called_once()
        created_res = self.orch_client.resources_create.call_args.args[0]
        assert isinstance(created_res, models.Resource)
        assert created_res.uuid == uuid
        assert created_res.kind == "foo"
        self.orch_client.resources_update.assert_not_called()
        self.orch_client.resources_delete.assert_not_called()

    def test_agent_update_capability(self):
        uuid = sys_uuid.uuid4()
        resource = conftest.FooResource(uuid=uuid)
        payload = models.Payload.empty()
        payload.add_caps_resource(resource.to_ua_resource(kind="foo"))
        payload.add_facts_resource(resource.to_ua_resource(kind="foo"))
        payload.calculate_hash()

        self.orch_client.agents_get_payload = mock.MagicMock(return_value=payload)

        class CapDriver(FooCapDriver):
            def list(self, capability):
                res = resource.dump_to_simple_view()
                res["name"] = "foo-name-updated"
                res = conftest.FooResource.restore_from_simple_view(**res)
                return [res.to_ua_resource(kind="foo")]

            def update(self, resource):
                assert resource.uuid == uuid
                return super().update(resource)

        agent = agent_svc.UniversalAgentService(
            agent_uuid=self.agent_uuid,
            orch_client=self.orch_client,
            caps_drivers=[CapDriver()],
            facts_drivers=[],
            payload_path=self.PAYLOAD_PATH,
        )

        agent._iteration()

        assert CapDriver.update_called
        assert not CapDriver.delete_called
        assert not CapDriver.create_called
        self.orch_client.agents_get_payload.assert_called_once()
        self.orch_client.resources_create.assert_not_called()
        self.orch_client.resources_update.assert_not_called()
        self.orch_client.resources_delete.assert_not_called()

    def test_agent_delete_capability(self):
        uuid = sys_uuid.uuid4()
        resource = conftest.FooResource(uuid=uuid)
        payload = models.Payload.empty()
        payload.capabilities = {"foo": {"resources": []}}
        payload.add_facts_resource(resource.to_ua_resource(kind="foo"))
        payload.calculate_hash()

        self.orch_client.agents_get_payload = mock.MagicMock(return_value=payload)

        class CapDriver(FooCapDriver):
            def list(self, capability):
                return [resource.to_ua_resource(kind="foo")]

            def delete(self, resource):
                assert resource.uuid == uuid
                return super().delete(resource)

        agent = agent_svc.UniversalAgentService(
            agent_uuid=self.agent_uuid,
            orch_client=self.orch_client,
            caps_drivers=[CapDriver()],
            facts_drivers=[],
            payload_path=self.PAYLOAD_PATH,
        )

        agent._iteration()

        assert CapDriver.delete_called
        assert not CapDriver.update_called
        assert not CapDriver.create_called
        self.orch_client.agents_get_payload.assert_called_once()
        self.orch_client.resources_create.assert_not_called()
        self.orch_client.resources_update.assert_not_called()
        self.orch_client.resources_delete.assert_called_once()
        deleted_res = self.orch_client.resources_delete.call_args.args[0]
        assert isinstance(deleted_res, models.Resource)
        assert deleted_res.uuid == uuid

    def test_agent_new_capabilities(self):
        uuid_a = sys_uuid.uuid4()
        uuid_b = sys_uuid.uuid4()
        resource_a = conftest.FooResource(uuid=uuid_a)
        resource_b = conftest.FooResource(uuid=uuid_b)
        payload = models.Payload.empty()
        payload.add_caps_resource(resource_a.to_ua_resource(kind="foo"))
        payload.add_caps_resource(resource_b.to_ua_resource(kind="foo"))
        payload.calculate_hash()

        self.orch_client.agents_get_payload = mock.MagicMock(return_value=payload)

        class CapDriver(FooCapDriver):
            def create(self, resource):
                assert resource.uuid in {uuid_a, uuid_b}
                return super().create(resource)

        agent = agent_svc.UniversalAgentService(
            agent_uuid=self.agent_uuid,
            orch_client=self.orch_client,
            caps_drivers=[CapDriver()],
            facts_drivers=[],
            payload_path=self.PAYLOAD_PATH,
        )

        agent._iteration()

        self.orch_client.agents_get_payload.assert_called_once()
        assert self.orch_client.resources_create.call_count == 2
        self.orch_client.resources_update.assert_not_called()
        self.orch_client.resources_delete.assert_not_called()

    def test_agent_mix_capability_actions(self):
        uuid_a = sys_uuid.uuid4()
        uuid_b = sys_uuid.uuid4()
        uuid_c = sys_uuid.uuid4()
        resource_a = conftest.FooResource(uuid=uuid_a)
        resource_b = conftest.FooResource(uuid=uuid_b)
        resource_c = conftest.FooResource(uuid=uuid_c)
        payload = models.Payload.empty()
        payload.add_caps_resource(resource_a.to_ua_resource(kind="foo"))
        payload.add_caps_resource(resource_b.to_ua_resource(kind="foo"))
        payload.add_facts_resource(resource_b.to_ua_resource(kind="foo"))
        payload.add_facts_resource(resource_c.to_ua_resource(kind="foo"))
        payload.calculate_hash()

        self.orch_client.agents_get_payload = mock.MagicMock(return_value=payload)

        class CapDriver(FooCapDriver):
            def list(self, capability):
                res = resource_b.dump_to_simple_view()
                res["name"] = "foo-name-updated"
                res = conftest.FooResource.restore_from_simple_view(**res)
                return [
                    res.to_ua_resource(kind="foo"),
                    resource_c.to_ua_resource(kind="foo"),
                ]

            def create(self, resource):
                assert resource.uuid == uuid_a
                return super().create(resource)

            def delete(self, resource):
                assert resource.uuid == uuid_c
                return super().delete(resource)

        agent = agent_svc.UniversalAgentService(
            agent_uuid=self.agent_uuid,
            orch_client=self.orch_client,
            caps_drivers=[CapDriver()],
            facts_drivers=[],
            payload_path=self.PAYLOAD_PATH,
        )

        agent._iteration()

        self.orch_client.resources_create.assert_called_once()
        created_res = self.orch_client.resources_create.call_args.args[0]
        assert isinstance(created_res, models.Resource)
        assert created_res.uuid == uuid_a
        self.orch_client.resources_update.assert_not_called()
        self.orch_client.resources_delete.assert_called_once()
        deleted_res = self.orch_client.resources_delete.call_args.args[0]
        assert isinstance(deleted_res, models.Resource)
        assert deleted_res.uuid == uuid_c

    def test_agent_new_fact(self):
        uuid = sys_uuid.uuid4()
        resource = conftest.FooResource(uuid=uuid)
        payload = models.Payload.empty()
        payload.facts = {"foo": {"resources": []}}
        payload.calculate_hash()

        self.orch_client.agents_get_payload = mock.MagicMock(return_value=payload)

        class FactDriver(FooFactDriver):
            def list(self, fact):
                super().list(fact)
                return [resource.to_ua_resource(kind="foo")]

        agent = agent_svc.UniversalAgentService(
            agent_uuid=self.agent_uuid,
            orch_client=self.orch_client,
            caps_drivers=[],
            facts_drivers=[FactDriver()],
            payload_path=self.PAYLOAD_PATH,
        )

        agent._iteration()

        self.orch_client.agents_get_payload.assert_called_once()
        self.orch_client.resources_create.assert_called_once()
        created_res = self.orch_client.resources_create.call_args.args[0]
        assert isinstance(created_res, models.Resource)
        assert created_res.uuid == uuid
        assert created_res.kind == "foo"
        self.orch_client.resources_update.assert_not_called()
        self.orch_client.resources_delete.assert_not_called()

    def test_agent_update_fact(self):
        uuid = sys_uuid.uuid4()
        resource = conftest.FooResource(uuid=uuid)
        payload = models.Payload.empty()
        payload.add_facts_resource(resource.to_ua_resource(kind="foo"))
        payload.calculate_hash()

        res = resource.dump_to_simple_view()
        res["name"] = "foo-name-updated"
        res = conftest.FooResource.restore_from_simple_view(**res)

        self.orch_client.agents_get_payload = mock.MagicMock(return_value=payload)

        class FactDriver(FooFactDriver):
            def list(self, fact):
                super().list(fact)
                return [res.to_ua_resource(kind="foo")]

        agent = agent_svc.UniversalAgentService(
            agent_uuid=self.agent_uuid,
            orch_client=self.orch_client,
            caps_drivers=[],
            facts_drivers=[FactDriver()],
            payload_path=self.PAYLOAD_PATH,
        )

        agent._iteration()

        self.orch_client.agents_get_payload.assert_called_once()
        self.orch_client.resources_create.assert_not_called()
        self.orch_client.resources_delete.assert_not_called()

        data = res.to_ua_resource(kind="foo").dump_to_simple_view()
        data.pop("created_at", None)
        data.pop("updated_at", None)
        data.pop("res_uuid", None)
        data.pop("node", None)
        uuid = data.pop("uuid")
        kind = data.pop("kind")
        self.orch_client.resources_update.assert_called_once_with(kind, uuid, **data)

    def test_agent_delete_fact(self):
        uuid = sys_uuid.uuid4()
        resource = conftest.FooResource(uuid=uuid)
        payload = models.Payload.empty()
        payload.add_facts_resource(resource.to_ua_resource(kind="foo"))
        payload.calculate_hash()

        self.orch_client.agents_get_payload = mock.MagicMock(return_value=payload)

        class FactDriver(FooFactDriver):
            pass

        agent = agent_svc.UniversalAgentService(
            agent_uuid=self.agent_uuid,
            orch_client=self.orch_client,
            caps_drivers=[],
            facts_drivers=[FactDriver()],
            payload_path=self.PAYLOAD_PATH,
        )

        agent._iteration()

        self.orch_client.agents_get_payload.assert_called_once()
        self.orch_client.resources_create.assert_not_called()
        self.orch_client.resources_update.assert_not_called()
        self.orch_client.resources_delete.assert_called_once()
        deleted_res = self.orch_client.resources_delete.call_args.args[0]
        assert isinstance(deleted_res, models.Resource)
        assert deleted_res.uuid == uuid

    def test_agent_new_facts(self):
        uuid_a = sys_uuid.uuid4()
        uuid_b = sys_uuid.uuid4()
        resource_a = conftest.FooResource(uuid=uuid_a)
        resource_b = conftest.FooResource(uuid=uuid_b)
        payload = models.Payload.empty()
        payload.facts = {"foo": {"resources": []}}
        payload.calculate_hash()

        self.orch_client.agents_get_payload = mock.MagicMock(return_value=payload)

        class FactDriver(FooFactDriver):
            def list(self, fact):
                super().list(fact)
                return [
                    resource_a.to_ua_resource(kind="foo"),
                    resource_b.to_ua_resource(kind="foo"),
                ]

        agent = agent_svc.UniversalAgentService(
            agent_uuid=self.agent_uuid,
            orch_client=self.orch_client,
            caps_drivers=[],
            facts_drivers=[FactDriver()],
            payload_path=self.PAYLOAD_PATH,
        )

        agent._iteration()

        self.orch_client.agents_get_payload.assert_called_once()
        assert self.orch_client.resources_create.call_count == 2
        self.orch_client.resources_update.assert_not_called()
        self.orch_client.resources_delete.assert_not_called()

    def test_agent_mix_fact_actions(self):
        uuid_a = sys_uuid.uuid4()
        uuid_b = sys_uuid.uuid4()
        uuid_c = sys_uuid.uuid4()
        resource_a = conftest.FooResource(uuid=uuid_a)
        resource_b = conftest.FooResource(uuid=uuid_b)
        resource_c = conftest.FooResource(uuid=uuid_c)
        payload = models.Payload.empty()
        payload.add_facts_resource(resource_b.to_ua_resource(kind="foo"))
        payload.add_facts_resource(resource_c.to_ua_resource(kind="foo"))
        payload.calculate_hash()

        res = resource_b.dump_to_simple_view()
        res["name"] = "foo-name-updated"
        res = conftest.FooResource.restore_from_simple_view(**res)

        self.orch_client.agents_get_payload = mock.MagicMock(return_value=payload)

        class FactDriver(FooFactDriver):
            def list(self, fact):
                super().list(fact)
                return [
                    resource_a.to_ua_resource(kind="foo"),
                    res.to_ua_resource(kind="foo"),
                ]

        agent = agent_svc.UniversalAgentService(
            agent_uuid=self.agent_uuid,
            caps_drivers=[],
            facts_drivers=[FactDriver()],
            orch_client=self.orch_client,
            payload_path=self.PAYLOAD_PATH,
        )

        agent._iteration()

        self.orch_client.agents_get_payload.assert_called_once()
        self.orch_client.resources_create.assert_called_once()
        created_res = self.orch_client.resources_create.call_args.args[0]
        assert isinstance(created_res, models.Resource)
        assert created_res.uuid == uuid_a
        self.orch_client.resources_delete.assert_called_once()
        deleted_res = self.orch_client.resources_delete.call_args.args[0]
        assert isinstance(deleted_res, models.Resource)
        assert deleted_res.uuid == uuid_c

        data = res.to_ua_resource(kind="foo").dump_to_simple_view()
        data.pop("created_at", None)
        data.pop("updated_at", None)
        data.pop("res_uuid", None)
        data.pop("node", None)
        uuid = data.pop("uuid")
        kind = data.pop("kind")
        self.orch_client.resources_update.assert_called_once_with(kind, uuid, **data)
