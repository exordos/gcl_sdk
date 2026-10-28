# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

from unittest import mock
import uuid

from bazooka import exceptions
import pytest

from gcl_sdk.clients.http import base
from gcl_sdk.events import clients
from gcl_sdk.events.dm import models


def test_event_sender_reuses_iam_authentication_for_create_and_poll():
    http = mock.Mock()
    login = mock.Mock()
    login.json.return_value = {
        "access_token": "first-access",
        "refresh_token": "refresh",
    }
    refresh = mock.Mock()
    refresh.json.return_value = {
        "access_token": "second-access",
        "refresh_token": "next-refresh",
    }
    created = mock.Mock()
    polled = mock.Mock()
    polled.json.return_value = {"status": "ACTIVE"}
    # login -> event create -> refresh after expired access -> retry poll
    http.post.side_effect = [login, created, refresh]
    http.get.side_effect = [exceptions.UnauthorizedError(cause=mock.Mock()), polled]
    auth = base.CoreIamAuthenticator(
        base_url="https://iam.example.com/api/core",
        username="sender",
        password="synthetic-password",
        http_client=http,
    )
    sender = clients.HttpEventClient(
        event_type_mapping={},
        endpoint="https://notification.example.com",
        version="v1",
        auth_token=None,
        project_id=str(uuid.uuid4()),
        auth=auth,
        http_client=http,
    )
    event = mock.Mock()
    event.uuid = uuid.uuid4()
    event.event_type = models.UserEvent(user_id=uuid.uuid4(), uuid=uuid.uuid4())
    event.event_data.to_simple_dict.return_value = {"kind": "IamUserRegistration"}
    assert sender.send_event(event) is event
    assert sender.get_event_status(event.uuid) == "ACTIVE"
    assert http.post.call_count == 3
    assert http.post.call_args_list[0].kwargs["data"]["grant_type"] == "password"
    assert http.post.call_args_list[1].kwargs["json"]["uuid"] == str(event.uuid)
    assert http.post.call_args_list[2].kwargs["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "refresh",
    }
    assert (
        http.get.call_args.kwargs["headers"]["Authorization"] == "Bearer second-access"
    )


def test_event_sender_rejects_missing_credentials():
    with pytest.raises(ValueError):
        clients.HttpEventClient(
            event_type_mapping={},
            endpoint="https://notification.example.com",
            version="v1",
            auth_token=None,
            project_id=str(uuid.uuid4()),
        )


@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
def test_configured_sender_logs_in_refreshes_and_redacts_over_http(
    tmp_path, caplog, level
):
    import http.server
    import json
    import threading
    import urllib.parse

    from oslo_config import cfg

    from gcl_sdk.events import opts

    grants = []
    created = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, payload):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path.endswith("/actions/get_token/invoke"):
                grants.append(urllib.parse.parse_qs(body.decode()))
                self.reply(
                    200,
                    {
                        "access_token": f"access-{len(grants)}",
                        "refresh_token": "synthetic-refresh",
                    },
                )
            else:
                assert self.headers["Authorization"] == "Bearer access-1"
                created.append(json.loads(body))
                self.reply(201, {})

        def do_GET(self):
            if self.headers["Authorization"] == "Bearer access-1":
                self.reply(401, {"error": "expired"})
            else:
                self.reply(200, {"status": "ACTIVE"})

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    credentials = tmp_path / "auth.json"
    credentials.write_text(
        json.dumps(
            {
                "base_url": endpoint,
                "username": "sender",
                "password": "synthetic-password",
            }
        )
    )
    credentials.chmod(0o600)
    mapping = tmp_path / "mapping.yaml"
    mapping.write_text("EventTypeMapping: {}")
    conf = cfg.ConfigOpts()
    opts.register_event_opts(conf)
    conf([], default_config_files=[])
    conf.set_override("event_type_mapping_filepath", str(mapping), group="events")
    conf.set_override("notification_auth_file", str(credentials), group="events")
    conf.set_override("exordos_notification_endpoint", endpoint, group="events")
    event = mock.Mock()
    event.uuid = uuid.uuid4()
    event.event_type = models.UserEvent(user_id=uuid.uuid4(), uuid=uuid.uuid4())
    event.event_data.to_simple_dict.return_value = {
        "confirmation": "synthetic-private-link"
    }
    try:
        with caplog.at_level(level):
            sender = clients.HttpEventClient.build_from_config(conf)
            sender.send_event(event)
            assert sender.get_event_status(event.uuid) == "ACTIVE"
        assert [g["grant_type"] for g in grants] == [["password"], ["refresh_token"]]
        assert grants[1]["refresh_token"] == ["synthetic-refresh"]
        assert len(created) == 1
        assert created[0]["uuid"] == str(event.uuid)
        for secret in [
            "synthetic-password",
            "synthetic-refresh",
            "access-1",
            "access-2",
            "synthetic-private-link",
        ]:
            assert secret not in caplog.text
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
