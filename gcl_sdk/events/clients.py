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
import uuid as sys_uuid

from bazooka import client as bzk_client
from bazooka import common as bzk_common
from bazooka import exceptions as bzk_exceptions
from izulu import root
from oslo_config import cfg
from restalchemy.dm import filters as ra_filters
from restalchemy.storage import exceptions as ra_exceptions
import yaml

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


class HttpEventClient(AbstractEventClient, bzk_common.RESTClientMixIn):
    def __init__(
        self,
        event_type_mapping,
        endpoint,
        version,
        auth_token,
        project_id,
        timeout=5,
    ):
        super().__init__(event_type_mapping=event_type_mapping)
        self._endpoint = endpoint
        self._version = version
        self._auth_token = auth_token
        self._project_id = project_id
        self._client = bzk_client.Client(default_timeout=timeout)

    def _get_headers(self):
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._auth_token}",
        }

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
            "event_type": (
                "/%s/event_types/%s"
                % (
                    self._version,
                    event.event_type.uuid,
                )
            ),
            "event_params": event.event_data.to_simple_dict(),
            "project_id": self._project_id,
        }

        url = self._build_collection_uri([self._version, "events"])

        self._client.post(url, json=body, headers=self._get_headers())

        return event

    def get_event_status(self, event_uuid):
        url = self._build_resource_uri([self._version, "events", event_uuid])
        try:
            result = self._client.get(url, headers=self._get_headers())
            return result.json()["status"]
        except bzk_exceptions.NotFoundError:
            raise EventNotFound(event_uuid=event_uuid)

    @classmethod
    def build_from_config(cls, conf=None, **kwargs):
        conf = conf or CONF

        endpoint = conf[event_c.DOMAIN].exordos_notification_endpoint
        version = conf[event_c.DOMAIN].exordos_api_version
        auth_token = conf[event_c.DOMAIN].exordos_api_token
        project_id = conf[event_c.DOMAIN].project_id

        return super().build_from_config(
            conf=conf,
            endpoint=endpoint,
            version=version,
            auth_token=auth_token,
            project_id=project_id,
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
