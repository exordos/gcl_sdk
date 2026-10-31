import datetime
import uuid
from types import SimpleNamespace
from unittest import mock

from gcl_iam.contexts import GenesisCoreAuthContext
import pytest
from webob import Request

from gcl_sdk.audit import constants
from gcl_sdk.audit.dm import models


def _event(**kwargs):
    timestamp = datetime.datetime(2026, 7, 12, 12, 0, tzinfo=datetime.timezone.utc)
    return models.AuditDeliveryEvent(
        uuid=uuid.uuid4(),
        service_name="compute",
        resource_type="node",
        resource_uuid=uuid.uuid4(),
        project_id=uuid.uuid4(),
        actor_user_uuid=None,
        action="create",
        snapshot={"status": "NEW"},
        created_at=timestamp,
        updated_at=timestamp,
        **kwargs,
    )


def test_dump_to_delivery_snapshot_excludes_local_delivery_state():
    event = _event()

    snapshot = event.dump_to_delivery_snapshot()

    assert set(snapshot) == set(constants.INGEST_FIELDS)
    assert "status" not in snapshot
    assert snapshot["uuid"] == str(event.uuid)
    assert snapshot["created_at"] == "2026-07-12 12:00:00.000000"


def test_base_model_matches_ingest_contract():
    assert set(models.AuditEventBase.properties) == set(constants.INGEST_FIELDS)


def test_actor_defaults_to_system():
    assert _event().actor == {"type": "system"}


def test_delivery_snapshot_preserves_custom_actor():
    event = _event(actor={"ip": "2001:db8::1"})

    assert event.dump_to_delivery_snapshot()["actor"] == {"ip": "2001:db8::1"}


def test_audit_actor_uses_user_uuid():
    actor_uuid = uuid.uuid4()
    model = models.AuditLogSQLStorableMixin()
    with mock.patch.object(model, "get_audit_actor_user_uuid", return_value=actor_uuid):
        assert model.get_audit_actor() == {"type": "user", "user_uuid": str(actor_uuid)}


def test_audit_actor_without_user_is_system():
    model = models.AuditLogSQLStorableMixin()
    with mock.patch.object(model, "get_audit_actor_user_uuid", return_value=None):
        assert model.get_audit_actor() == {"type": "system"}


def test_write_audit_event_preserves_custom_actor_and_user_uuid():
    actor_uuid = uuid.uuid4()
    model = models.AuditLogSQLStorableMixin()
    with (
        mock.patch.object(model, "get_audit_service_name", return_value="compute"),
        mock.patch.object(model, "get_audit_resource_type", return_value="node"),
        mock.patch.object(model, "get_audit_resource_uuid", return_value=uuid.uuid4()),
        mock.patch.object(model, "get_audit_project_id", return_value=None),
        mock.patch.object(model, "get_audit_actor_user_uuid", return_value=actor_uuid),
        mock.patch.object(model, "get_audit_actor", return_value={"ip": "192.0.2.1"}),
        mock.patch.object(models.AuditDeliveryEvent, "insert", autospec=True) as insert,
    ):
        model._write_audit_event("create", {}, session="session")
        insert.assert_called_once()
        event, session = insert.call_args.args
        assert session == "session"
        assert event.actor == {"ip": "192.0.2.1"}
        assert event.actor_user_uuid == actor_uuid


@pytest.mark.parametrize(
    "headers,remote_addr,expected",
    [
        ({}, "192.0.2.1", "192.0.2.1"),
        ({"X-Forwarded-For": "2001:db8::1, 192.0.2.2"}, "192.0.2.2", "2001:db8::1"),
        ({"X-Real-IP": "192.0.2.3"}, "192.0.2.2", "192.0.2.3"),
        ({}, None, None),
        ({"X-Forwarded-For": "invalid"}, "192.0.2.2", None),
    ],
)
def test_audit_actor_uses_request_ip(headers, remote_addr, expected):
    request = Request.blank("/", headers=headers)
    request.remote_addr = remote_addr
    context = GenesisCoreAuthContext(request)
    actor_uuid = uuid.uuid4()
    model = models.AuditLogSQLStorableMixin()
    with (
        mock.patch.object(models.contexts, "get_context", return_value=context),
        mock.patch.object(model, "get_audit_actor_user_uuid", return_value=actor_uuid),
    ):
        actor = model.get_audit_actor()
        assert actor["user_uuid"] == str(actor_uuid)
        assert actor.get("ip") == expected


def test_audit_actor_without_context_is_system():
    model = models.AuditLogSQLStorableMixin()
    with mock.patch.object(
        models.contexts,
        "get_context",
        side_effect=models.contexts.ContextIsNotExistsInStorage,
    ):
        assert model.get_audit_actor() == {"type": "system"}


def test_audit_actor_without_ip_or_user_is_system():
    model = models.AuditLogSQLStorableMixin()
    context = SimpleNamespace(get_user_ip=lambda: None)
    with mock.patch.object(models.contexts, "get_context", return_value=context):
        assert model.get_audit_actor() == {"type": "system"}


def test_actor_contains_only_identity_and_ip():
    actor_uuid = uuid.uuid4()
    token_uuid = uuid.uuid4()
    request = Request.blank(
        "/nodes?secret=hidden",
        headers={
            "User-Agent": "sdk-client",
            "X-Request-ID": "request-123",
            "traceparent": "trace-context",
            "Authorization": "Bearer secret",
            "Cookie": "session=secret",
        },
    )
    context = GenesisCoreAuthContext(request)
    iam_context = SimpleNamespace(
        introspection_info=lambda: {
            "user_info": {"type": "service"},
            "otp_verified": False,
        },
        token_info=SimpleNamespace(
            token_info={"jti": str(token_uuid)},
            uuid=token_uuid,
            token_type="auth",
            issuer_url="https://iam.example",
            token="secret",
        ),
    )
    model = models.AuditLogSQLStorableMixin()
    with (
        context.iam_session(iam_context),
        mock.patch.object(models.contexts, "get_context", return_value=context),
        mock.patch.object(model, "get_audit_actor_user_uuid", return_value=actor_uuid),
    ):
        assert model.get_audit_actor() == {
            "type": "service",
            "user_uuid": str(actor_uuid),
        }


def test_actor_for_request_without_iam_session_is_anonymous():
    context = GenesisCoreAuthContext(Request.blank("/"))
    model = models.AuditLogSQLStorableMixin()
    with mock.patch.object(models.contexts, "get_context", return_value=context):
        assert model.get_audit_actor() == {
            "type": "anonymous",
        }
