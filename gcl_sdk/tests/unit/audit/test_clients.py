from unittest import mock

from gcl_sdk.audit import clients
from gcl_sdk.audit.dm import models


def _event():
    event = mock.Mock(spec=models.AuditEventBase)
    event.dump_to_delivery_snapshot.return_value = {
        "uuid": "00000000-0000-0000-0000-000000000001"
    }
    return event


def _client(http_client, **overrides):
    kwargs = {
        "endpoint": "http://audit.local:8080",
        "version": "v1",
        "auth_token": "test-token",
        "http_client": http_client,
    }
    kwargs.update(overrides)
    return clients.HttpAuditClient(**kwargs)


def test_send_event_uses_delivery_snapshot():
    http_client = mock.Mock()
    ingest_response = mock.Mock()
    http_client.post.return_value = ingest_response

    event = _event()
    result = _client(http_client).send_event(event)

    assert result is ingest_response
    event.dump_to_delivery_snapshot.assert_called_once_with()
    ingest_call = http_client.post.call_args
    assert ingest_call.args[0] == "http://audit.local:8080/v1/audit/events/"
    assert ingest_call.kwargs["headers"]["Authorization"] == "Bearer test-token"
    assert ingest_call.kwargs["json"] == event.dump_to_delivery_snapshot.return_value


def test_missing_auth_token_is_rejected():
    try:
        _client(mock.Mock(), auth_token=None)
    except RuntimeError as exc:
        assert "requires auth_token" in str(exc)
    else:
        raise AssertionError("RuntimeError was not raised")
