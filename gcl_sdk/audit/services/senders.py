#    Copyright 2026 Genesis Corporation.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License.

import logging
from typing import Any

from bazooka import exceptions as bzk_exceptions
from gcl_looper.services import basic

from gcl_sdk.audit import clients
from gcl_sdk.audit import opts
from gcl_sdk.audit.dm import models

LOG = logging.getLogger(__name__)


class AuditSenderService(basic.BasicService):
    def __init__(
        self,
        audit_client: Any,
        batch_size: int = 100,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._audit_client = audit_client
        self._batch_size = batch_size

    def _fetch_events(self) -> list[models.AuditDeliveryEvent]:
        return models.AuditDeliveryEvent.objects.get_all(
            order_by={"created_at": "asc", "uuid": "asc"},
            limit=self._batch_size,
        )

    def _send_events(self) -> None:
        for event in self._fetch_events():
            if event.status == models.AuditDeliveryEvent.STATUS.ERROR.value:
                LOG.error(
                    "Audit delivery is blocked by conflicting event %s",
                    event.uuid,
                )
                break
            try:
                self._audit_client.send_event(event)
            except bzk_exceptions.ConflictError:
                LOG.exception(
                    "Central Audit rejected event %s because its UUID conflicts",
                    event.uuid,
                )
                event.status = models.AuditDeliveryEvent.STATUS.ERROR.value
                event.update()
                break
            except Exception:
                LOG.exception("Can't send audit event %s", event.uuid)
                break
            else:
                event.delete()

    @classmethod
    def build_from_config(
        cls,
        conf: Any = None,
        client_cls: Any = None,
        **kwargs: Any,
    ) -> "AuditSenderService":
        client_cls = client_cls or clients.HttpAuditClient
        delivery = opts.get_audit_delivery_config(conf)
        return cls(
            audit_client=client_cls.build_from_config(conf=conf),
            batch_size=delivery.batch_size,
            **kwargs,
        )

    def _iteration(self) -> None:
        self._send_events()
