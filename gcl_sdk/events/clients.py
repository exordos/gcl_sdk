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

import abc
import json
import uuid as sys_uuid

from bazooka import client as bzk_client
from bazooka import exceptions as bzk_exceptions
from izulu import root
from oslo_config import cfg
from restalchemy.dm import filters as ra_filters
from restalchemy.storage import exceptions as ra_exceptions
import yaml

from gcl_sdk.clients.http import base as http_base
from gcl_sdk.events import constants as event_c
from gcl_sdk.events.dm import models

CONF = cfg.CONF


class EventNotFound(root.Error):
    __toggles__ = root.Toggles.DEFAULT ^ root.Toggles.FORBID_UNANNOTATED_FIELDS
    __template__ = "The Event[<{event_uuid}>] not found"


class AbstractEventClient(metaclass=abc.ABCMeta):
    def __init__(self, event_type_mapping):
        self._event_type_mapping = event_type_mapping
        super().__init__()

    @classmethod
    def build_from_config(cls, conf=None, **kwags):
        conf = conf or CONF

        config_file = conf.find_file(conf[event_c.DOMAIN].event_type_mapping_filepath)

        with open(config_file) as fp:
            event_type_mapping = yaml.safe_load(fp)

        return cls(event_type_mapping=event_type_mapping, **kwags)

    def build_user_event(self, context, user, payload):
        event_type = models.UserEvent(
            user_id=user.uuid,
            uuid=self.get_event_uuid(payload.KIND),
        )
        event = models.Event(
            event_type=event_type,
            event_data=payload,
        )

        return event

    @abc.abstractmethod
    def send_event(self, event):
        raise NotImplementedError()

    @abc.abstractmethod
    def get_event_status(self, event_uuid):
        raise NotImplementedError()

    def get_event_uuid(self, event_type_name):
        sec = self._event_type_mapping[event_c.EVENT_TYPE_MAPPING_SECTION]
        return sys_uuid.UUID(sec[event_type_name])


class DummyEventClient(AbstractEventClient):
    def __init__(self, event_type_mapping=None, **kwargs):
        event_type_mapping = event_type_mapping or {}
        super().__init__(event_type_mapping=event_type_mapping, **kwargs)

    def send_event(self, event):
        return event

    def get_event_status(self, event_uuid):
        return models.Event.STATUS.ACTIVE.value

    def get_event_uuid(self, event_type_name):
        return sys_uuid.uuid4()


class EventHttpSession(bzk_client.Client.SESSION):
    def _sanitize_body(self, body):
        return "[REDACTED]"

    @staticmethod
    def _response_body_for_log(response):
        return "[REDACTED]"


class EventHttpClient(bzk_client.Client):
    SESSION = EventHttpSession

    def request(self, method, url, **kwargs):
        kwargs["allow_redirects"] = False
        response = super().request(method, url, **kwargs)
        if 300 <= response.status_code < 400:
            raise ValueError("Event delivery does not follow redirects")
        return response


class HttpEventClient(AbstractEventClient):
    def __init__(
        self,
        event_type_mapping,
        endpoint,
        version,
        auth_token,
        project_id,
        timeout=5,
        auth=None,
        http_client=None,
    ):
        super().__init__(event_type_mapping=event_type_mapping)
        if auth is None and not auth_token:
            raise ValueError(
                "Event delivery requires IAM credentials or an access token"
            )
        self._version = version
        self._project_id = project_id
        http_client = http_client or EventHttpClient(default_timeout=timeout)
        auth = auth or http_base.CoreIamAuthenticator(
            base_url=endpoint,
            access_token=auth_token,
            http_client=http_client,
        )
        self._client = http_base.CollectionBaseClient(
            base_url=endpoint.rstrip("/"),
            http_client=http_client,
            auth=auth,
        )

    def send_event(self, event):
        exchange = {}
        if isinstance(event.event_type, models.UserEvent):
            exchange = {
                "kind": "User",
                "user_id": str(event.event_type.user_id),
            }
        body = {
            "uuid": str(event.uuid),
            "exchange": exchange,
            "event_type": (f"/{self._version}/event_types/{event.event_type.uuid}"),
            "event_params": event.event_data.to_simple_dict(),
            "project_id": self._project_id,
        }

        self._client.create(f"/{self._version}/events/", data=body)

        return event

    def get_event_status(self, event_uuid):
        try:
            result = self._client.get(f"/{self._version}/events/", event_uuid)
            return result["status"]
        except bzk_exceptions.NotFoundError:
            raise EventNotFound(event_uuid=event_uuid)

    @classmethod
    def build_from_config(cls, conf=None, **kwargs):
        conf = conf or CONF

        endpoint = conf[event_c.DOMAIN].exordos_notification_endpoint
        version = conf[event_c.DOMAIN].exordos_api_version
        auth_token = conf[event_c.DOMAIN].exordos_api_token
        project_id = conf[event_c.DOMAIN].project_id
        http_client = EventHttpClient(default_timeout=5)
        auth = None
        if conf[event_c.DOMAIN].notification_auth_file:
            with open(conf[event_c.DOMAIN].notification_auth_file) as stream:
                auth = http_base.CoreIamAuthenticator(
                    **json.load(stream),
                    http_client=http_client,
                )

        return super().build_from_config(
            conf=conf,
            endpoint=endpoint,
            version=version,
            auth_token=auth_token,
            project_id=project_id,
            auth=auth,
            http_client=http_client,
            **kwargs,
        )


class AsyncEventClient(AbstractEventClient):
    def send_event(self, event):
        event.save()
        return event

    def get_event_status(self, event_uuid):
        try:
            return models.Event.objects.get_one(
                filters={
                    "uuid": ra_filters.EQ(event_uuid),
                }
            ).status.value
        except ra_exceptions.RecordNotFound:
            raise EventNotFound(event_uuid=event_uuid)


EVENT_CLIENT_MAPPER = {
    "async": AsyncEventClient.build_from_config,
    "dummy": DummyEventClient.build_from_config,
    "http": HttpEventClient.build_from_config,
}


def build_client(conf=None):
    conf = conf or CONF

    client_type = conf[event_c.DOMAIN].client_type

    return EVENT_CLIENT_MAPPER[client_type](conf=conf)
