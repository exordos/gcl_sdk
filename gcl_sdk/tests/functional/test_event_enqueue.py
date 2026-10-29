# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import uuid

import pytest

from restalchemy.common import contexts

from gcl_sdk.events import clients
from gcl_sdk.events.dm import models
from gcl_sdk.tests.functional import conftest
from gcl_sdk.tests.functional import utils


class TestAsyncEventEnqueue(utils.RestServiceTestCase):
    def setup_method(self):
        self._migrations = self.get_migration_engine()
        self._migrations.apply_migration(conftest.FIRST_MIGRATION)

    def teardown_method(self):
        self._migrations.rollback_migration(conftest.FIRST_MIGRATION)

    @staticmethod
    def event(event_uuid):
        return models.Event(
            uuid=event_uuid,
            event_type=models.UserEvent(uuid=uuid.uuid4(), user_id=uuid.uuid4()),
            event_data=models.DummyEventPayload(),
        )

    def test_async_status_returns_the_stored_status(self):
        event = self.event(uuid.uuid4())
        client = clients.AsyncEventClient({})
        assert client.send_event(event) is event
        event.status = "ACTIVE"
        event.save()
        assert client.get_event_status(event.uuid) == "ACTIVE"

    def test_enqueue_rolls_back_with_the_explicit_caller_session(self):
        event = self.event(uuid.uuid4())
        with pytest.raises(RuntimeError, match="synthetic caller rollback"):
            with contexts.Context().session_manager() as session:
                clients.AsyncEventClient({}).send_event(event)
                assert (
                    session.execute(
                        "SELECT count(*) AS n FROM gcl_sdk_events WHERE uuid=%s",
                        (event.uuid,),
                    ).fetchone()["n"]
                    == 1
                )
                raise RuntimeError("synthetic caller rollback")
        assert models.Event.objects.get_all() == []
