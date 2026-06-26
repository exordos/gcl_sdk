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

import logging

from gcl_looper.services import basic
from oslo_config import cfg
from restalchemy.dm import filters as ra_filters

from gcl_sdk.events import clients
from gcl_sdk.events import constants as event_c
from gcl_sdk.events.dm import models

LOG = logging.getLogger(__name__)


class EventSenderService(basic.BasicService):
    def __init__(self, notification_client, butch_size=100, enabled=False, **kwargs):
        super().__init__(**kwargs)
        self._notification_client = notification_client
        self._butch_size = butch_size
        self._enabled = enabled

    def _fetch_new_events(self):
        return models.Event.objects.get_all(
            filters={
                "status": ra_filters.EQ(models.Event.STATUS.NEW.value),
            },
            limit=self._butch_size,
        )

    def _fetch_in_progress_events(self):
        return models.Event.objects.get_all(
            filters={
                "status": ra_filters.EQ(models.Event.STATUS.IN_PROGRESS.value),
            },
            limit=self._butch_size,
        )

    def _send_new_events(self):
        events = self._fetch_new_events()
        for event in events:
            try:
                self._notification_client.send_event(event)
                event.status = models.Event.STATUS.IN_PROGRESS.value
            except Exception:
                LOG.exception("Can't send event by reason:")
            event.save()

    def _process_status_events(self):
        events = self._fetch_in_progress_events()
        for event in events:
            try:
                event_status = self._notification_client.get_event_status(
                    event_uuid=event.uuid
                )
                if event_status == models.Event.STATUS.ACTIVE.value:
                    event.status = event_status
                else:
                    event.status = models.Event.STATUS.IN_PROGRESS.value
            except clients.EventNotFound:
                event.status = models.Event.STATUS.NEW.value
            event.save()

    @classmethod
    def build_from_config(cls, conf=None, client_cls=None, **kwargs):
        conf = conf or cfg.CONF
        client_cls = client_cls or clients.HttpEventClient

        notification_client = client_cls.build_from_config(conf=conf)

        return cls(
            enabled=conf[event_c.DOMAIN].enabled,
            notification_client=notification_client,
        )

    def _iteration(self):
        if self._enabled:
            self._send_new_events()
            self._process_status_events()
        LOG.debug("Process Events is %s", "enabled" if self._enabled else "disabled")
