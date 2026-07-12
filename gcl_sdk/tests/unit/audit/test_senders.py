from unittest import mock

from bazooka import exceptions as bzk_exceptions

from gcl_sdk.audit.dm import models
from gcl_sdk.audit.services import senders


def _event():
    event = mock.Mock()
    event.uuid = "00000000-0000-0000-0000-000000000001"
    event.status = models.AuditDeliveryEvent.STATUS.NEW.value
    return event


def test_successful_delivery_deletes_local_event():
    event = _event()
    audit_client = mock.Mock()
    service = senders.AuditSenderService(audit_client=audit_client)
    service._fetch_events = mock.Mock(return_value=[event])

    service._send_events()

    audit_client.send_event.assert_called_once_with(event)
    event.delete.assert_called_once_with()
    event.save.assert_not_called()


def test_transient_failure_keeps_event_new_for_retry():
    event = _event()
    audit_client = mock.Mock()
    audit_client.send_event.side_effect = RuntimeError("network unavailable")
    service = senders.AuditSenderService(audit_client=audit_client)
    service._fetch_events = mock.Mock(return_value=[event])

    service._send_events()

    assert event.status == models.AuditDeliveryEvent.STATUS.NEW.value
    event.delete.assert_not_called()
    event.update.assert_not_called()


def test_uuid_conflict_marks_event_error():
    event = _event()
    cause = mock.Mock()
    cause.response.status_code = 409
    audit_client = mock.Mock()
    audit_client.send_event.side_effect = bzk_exceptions.ConflictError(cause)
    service = senders.AuditSenderService(audit_client=audit_client)
    service._fetch_events = mock.Mock(return_value=[event])

    service._send_events()

    assert event.status == models.AuditDeliveryEvent.STATUS.ERROR.value
    event.delete.assert_not_called()
    event.update.assert_called_once_with()


def test_fetches_oldest_events_in_bounded_batches():
    service = senders.AuditSenderService(audit_client=mock.Mock(), batch_size=17)

    with mock.patch.object(senders.models, "AuditDeliveryEvent") as audit_event:
        audit_event.STATUS = models.AuditDeliveryEvent.STATUS
        service._fetch_events()

    get_all = audit_event.objects.get_all
    get_all.assert_called_once()
    kwargs = get_all.call_args.kwargs
    assert kwargs["limit"] == 17
    assert kwargs["order_by"] == {"created_at": "asc", "uuid": "asc"}
    assert "filters" not in kwargs


def test_transient_failure_blocks_later_events():
    first = _event()
    second = _event()
    audit_client = mock.Mock()
    audit_client.send_event.side_effect = RuntimeError("network unavailable")
    service = senders.AuditSenderService(audit_client=audit_client)
    service._fetch_events = mock.Mock(return_value=[first, second])

    service._send_events()

    audit_client.send_event.assert_called_once_with(first)
    second.delete.assert_not_called()


def test_existing_error_event_blocks_delivery():
    conflict = _event()
    conflict.status = models.AuditDeliveryEvent.STATUS.ERROR.value
    later = _event()
    audit_client = mock.Mock()
    service = senders.AuditSenderService(audit_client=audit_client)
    service._fetch_events = mock.Mock(return_value=[conflict, later])

    service._send_events()

    audit_client.send_event.assert_not_called()
    conflict.delete.assert_not_called()
    later.delete.assert_not_called()
