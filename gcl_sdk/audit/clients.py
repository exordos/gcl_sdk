#    Copyright 2026 Genesis Corporation.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License.

from urllib import parse
from typing import Any

from bazooka import client as bzk_client

from gcl_sdk.audit import opts
from gcl_sdk.audit.dm import models


class HttpAuditClient:
    def __init__(
        self,
        endpoint: str,
        version: str,
        auth_token: str,
        timeout: int = 5,
        http_client: Any = None,
    ) -> None:
        if not auth_token:
            raise RuntimeError("Audit delivery requires auth_token")
        self._endpoint = endpoint.rstrip("/") + "/"
        self._version = version
        self._token = auth_token
        self._client = http_client or bzk_client.Client(default_timeout=timeout)

    def _event_url(self) -> str:
        return parse.urljoin(
            self._endpoint,
            f"{self._version}/audit/events/",
        )

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def send_event(self, event: models.AuditEventBase) -> Any:
        body = event.dump_to_delivery_snapshot()
        return self._client.post(
            self._event_url(),
            json=body,
            headers=self._auth_headers(),
        )

    @classmethod
    def build_from_config(
        cls,
        conf: Any = None,
        **kwargs: Any,
    ) -> "HttpAuditClient":
        delivery = opts.get_audit_delivery_config(conf)
        return cls(
            endpoint=delivery.endpoint,
            version=delivery.api_version,
            auth_token=delivery.auth_token,
            timeout=delivery.timeout,
            **kwargs,
        )
