# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

from unittest import mock

from gcl_sdk.events.services import senders


def test_preparation_can_complete_an_event_without_a_network_send():
    class Sender(senders.EventSenderService):
        def _prepare_event(self, event):
            event.status = "ACTIVE"
            return False

    client = mock.Mock()
    event = mock.Mock(status="NEW")
    sender = Sender(client)
    sender._fetch_new_events = mock.Mock(return_value=[event])
    sender._send_new_events()
    client.send_event.assert_not_called()
    assert event.status == "ACTIVE"
    event.save.assert_called_once_with()


def test_result_hooks_keep_domain_handling_inside_the_shared_sender_loop():
    class Sender(senders.EventSenderService):
        def _get_event_status(self, event):
            return self._notification_client.get_event(event.uuid)

        def _apply_event_status(self, event, result):
            assert result["errors"][0]["code"] == "registration_token_inactive"
            event.status = "ERROR"

    client = mock.Mock()
    client.get_event.return_value = {
        "status": "ERROR",
        "errors": [{"code": "registration_token_inactive"}],
    }
    event = mock.Mock(status="IN_PROGRESS")
    sender = Sender(client)
    sender._fetch_in_progress_events = mock.Mock(return_value=[event])
    sender._process_status_events()
    client.get_event.assert_called_once_with(event.uuid)
    assert event.status == "ERROR"
    event.save.assert_called_once_with()


def test_default_sender_keeps_core_send_and_error_retry_behavior():
    client = mock.Mock()
    event = mock.Mock(status="NEW")
    sender = senders.EventSenderService(client)
    sender._fetch_new_events = mock.Mock(return_value=[event])
    sender._send_new_events()
    client.send_event.assert_called_once_with(event)
    assert event.status == "IN_PROGRESS"
    client.get_event_status.return_value = "ERROR"
    sender._fetch_in_progress_events = mock.Mock(return_value=[event])
    sender._process_status_events()
    client.get_event_status.assert_called_once_with(event_uuid=event.uuid)
    assert event.status == "IN_PROGRESS"
