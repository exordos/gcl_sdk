# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import http.server
import json
import threading
from unittest import mock
import uuid

from bazooka import exceptions as bzk_exceptions
import pytest

from gcl_sdk.events import clients
from gcl_sdk.events.dm import models


@pytest.fixture
def notification():
    received = []
    event_uuid = str(uuid.uuid4())
    response = {
        "uuid": event_uuid,
        "status": "ERROR",
        "errors": [
            {"code": "registration_token_inactive", "device_uuid": str(uuid.uuid4())}
        ],
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(
                (
                    self.path,
                    self.headers["Authorization"],
                    json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                )
            )
            self.send_response(response.get("_post_status", 201))
            self.end_headers()  # A successful send does not require a JSON response.

        def do_GET(self):
            if self.path.endswith("/missing"):
                self.send_response(404)
                self.end_headers()
                return
            received.append((self.path, self.headers["Authorization"]))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = clients.HttpEventClient(
        {},
        f"http://127.0.0.1:{server.server_port}",
        "v1",
        "synthetic-token",
        str(uuid.uuid4()),
    )
    try:
        yield client, received, response
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_prepared_event_preserves_uuid_recipient_deadline_and_encrypted_bytes(
    notification,
):
    client, received, response = notification
    body = {
        "uuid": response["uuid"],
        "project_id": str(uuid.uuid4()),
        "event_type": f"/v1/event_types/{uuid.uuid4()}",
        "exchange": {"kind": "User", "user_uuid": str(uuid.uuid4())},
        "last_retry_at": "2026-10-28T23:00:00Z",
        "event_params": {
            "kind": "notification_message_create",
            "devices": [
                {
                    "registration_token": "synthetic-address",
                    "content": {"ciphertext": "opaque"},
                }
            ],
        },
    }
    client.send_event_body(body)
    assert received == [("/v1/events/", "Bearer synthetic-token", body)]


def test_full_result_preserves_device_errors_and_status_api_remains_compatible(
    notification,
):
    client, received, response = notification
    assert client.get_event(response["uuid"]) == response
    assert client.get_event_status(response["uuid"]) == "ERROR"
    assert (
        received == [(f"/v1/events/{response['uuid']}", "Bearer synthetic-token")] * 2
    )


def test_full_result_preserves_not_found_exception(notification):
    client, _, _ = notification
    with pytest.raises(clients.EventNotFound):
        client.get_event("missing")


def test_already_accepted_uuid_recovers_after_a_repeated_post(notification):
    client, received, response = notification
    response["_post_status"] = 409
    body = {"uuid": response["uuid"], "event_params": {"ciphertext": "unchanged"}}
    client.send_event_body(body)
    assert received == [
        ("/v1/events/", "Bearer synthetic-token", body),
        (f"/v1/events/{response['uuid']}", "Bearer synthetic-token"),
    ]


def test_uuid_conflict_is_not_accepted_when_the_event_cannot_be_read(notification):
    client, _, response = notification
    response["_post_status"] = 409
    with pytest.raises(bzk_exceptions.ConflictError):
        client.send_event_body({"uuid": "missing"})


def test_async_client_uses_the_standard_model_save():
    client = clients.AsyncEventClient({})
    event = mock.Mock()
    assert client.send_event(event) is event
    event.save.assert_called_once_with()


def test_typed_client_allows_payload_serialization_without_replacing_transport(
    notification,
):
    original, received, response = notification
    body = {"uuid": response["uuid"], "project_id": "own-project", "event_params": {}}

    class Client(clients.HttpEventClient):
        def _build_event_body(self, event):
            return body

    client = Client({}, original._endpoint, "v1", "synthetic-token", "default-project")
    event = models.Event(
        event_type=models.UserEvent(user_id=uuid.uuid4(), uuid=uuid.uuid4()),
        event_data=models.DummyEventPayload(),
    )
    assert client.send_event(event) is event
    assert received == [("/v1/events/", "Bearer synthetic-token", body)]


def test_typed_event_keeps_legacy_serialization_and_return_value(notification):
    client, received, _ = notification
    event = models.Event(
        event_type=models.UserEvent(user_id=uuid.uuid4(), uuid=uuid.uuid4()),
        event_data=models.DummyEventPayload(),
    )
    assert client.send_event(event) is event
    assert received == [
        (
            "/v1/events/",
            "Bearer synthetic-token",
            {
                "uuid": str(event.uuid),
                "project_id": client._project_id,
                "event_type": f"/v1/event_types/{event.event_type.uuid}",
                "exchange": {"kind": "User", "user_id": str(event.event_type.user_id)},
                "event_params": {},
            },
        )
    ]
