#    Copyright 2026 Genesis Corporation.
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

"""Flat and nested (``/v1/network/lb/{lb}/vhosts/``) REST collections."""

import types
from unittest import mock
import uuid as sys_uuid

import pytest

from gcl_sdk.agents.universal.clients.backend import core
from gcl_sdk.agents.universal.clients.backend import rest

LB = "11111111-1111-4111-8111-111111111111"
VHOST = "22222222-2222-4222-8222-222222222222"
UUID = sys_uuid.UUID("33333333-3333-4333-8333-333333333333")

COLLECTIONS = {
    "border": "/v1/network/border/",
    "lb_vhost": "/v1/network/lb/{lb}/vhosts/",
    "lb_vhost_route": "/v1/network/lb/{lb}/vhosts/{vhost}/routes/",
}


def _client():
    http = mock.MagicMock()
    return rest.RestApiBackendClient(http, COLLECTIONS), http


def _resource(kind, **value):
    return types.SimpleNamespace(kind=kind, uuid=UUID, value=value)


def test_flat_collection_is_used_as_is():
    client, http = _client()
    http.create.return_value = {"uuid": str(UUID), "name": "b"}

    res = client.create(_resource("border", uuid=str(UUID), name="b"))

    http.create.assert_called_once_with(
        "/v1/network/border/", {"uuid": str(UUID), "name": "b"}
    )
    assert res == {"uuid": str(UUID), "name": "b"}


def test_flat_collection_list_passes_filters():
    client, http = _client()
    http.filter.return_value = [{"uuid": str(UUID)}]

    assert client.list("border", project_id="p") == [{"uuid": str(UUID)}]
    http.filter.assert_called_once_with("/v1/network/border/", project_id="p")


def test_nested_create_addresses_parent_and_keeps_it_out_of_body():
    client, http = _client()
    http.create.return_value = {"uuid": str(UUID), "condition": {}}

    res = client.create(
        _resource("lb_vhost_route", uuid=str(UUID), lb=LB, vhost=VHOST, condition={})
    )

    http.create.assert_called_once_with(
        f"/v1/network/lb/{LB}/vhosts/{VHOST}/routes/",
        {"uuid": str(UUID), "condition": {}},
    )
    # Put back, so the value matches the target fields.
    assert res == {"uuid": str(UUID), "condition": {}, "lb": LB, "vhost": VHOST}


def test_nested_get_update_delete_address_parent():
    client, http = _client()
    http.get.return_value = {"uuid": str(UUID), "port": 80}
    http.update.return_value = {"uuid": str(UUID), "port": 8080}
    url = f"/v1/network/lb/{LB}/vhosts/"

    assert client.get(_resource("lb_vhost", lb=LB)) == {
        "uuid": str(UUID),
        "port": 80,
        "lb": LB,
    }
    http.get.assert_called_once_with(url, UUID)

    assert client.update(_resource("lb_vhost", lb=LB, port=8080))["lb"] == LB
    http.update.assert_called_once_with(url, UUID, port=8080)

    client.delete(_resource("lb_vhost", lb=LB))
    http.delete.assert_called_once_with(url, UUID)


def test_nested_list_walks_parents():
    client, http = _client()
    lb2 = "44444444-4444-4444-8444-444444444444"
    listing = {
        "/v1/network/lb/": [{"uuid": LB}, {"uuid": lb2}],
        f"/v1/network/lb/{LB}/vhosts/": [{"uuid": VHOST}],
        f"/v1/network/lb/{lb2}/vhosts/": [],
        f"/v1/network/lb/{LB}/vhosts/{VHOST}/routes/": [{"uuid": str(UUID)}],
    }
    http.filter.side_effect = lambda url, **kw: listing[url]

    res = client.list("lb_vhost_route", project_id="p", uuid=(str(UUID),))

    assert res == [{"uuid": str(UUID), "lb": LB, "vhost": VHOST}]
    # Parents are narrowed by project only; the uuid filter is the leaf's.
    assert http.filter.call_args_list == [
        mock.call("/v1/network/lb/", project_id="p"),
        mock.call(f"/v1/network/lb/{LB}/vhosts/", project_id="p"),
        mock.call(f"/v1/network/lb/{lb2}/vhosts/", project_id="p"),
        mock.call(
            f"/v1/network/lb/{LB}/vhosts/{VHOST}/routes/",
            project_id="p",
            uuid=(str(UUID),),
        ),
    ]


def test_core_client_needs_a_project_for_nested_collections():
    with pytest.raises(ValueError, match="lb_vhost"):
        core.GCRestApiBackendClient(mock.MagicMock(), COLLECTIONS)

    core.GCRestApiBackendClient(
        mock.MagicMock(), COLLECTIONS, project_id=sys_uuid.uuid4()
    )
    core.GCRestApiBackendClient(mock.MagicMock(), {"border": "/v1/network/border/"})
