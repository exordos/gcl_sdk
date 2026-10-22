#    Copyright 2026 Genesis Corporation.
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

from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
import json
import threading
from urllib.parse import parse_qs
from urllib.parse import urlsplit
import uuid

import bazooka
import pytest

from gcl_sdk.clients.http import base

COLLECTION = "/v1/events/"
RESOURCE_UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")
RESOURCE_PATH = COLLECTION + str(RESOURCE_UUID)
RESOURCE = {"uuid": str(RESOURCE_UUID), "name": "sample"}


@pytest.fixture
def http_collection():
    """Use the real collection adapter and transport, including on Python 3.10."""
    received = []

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            url = urlsplit(self.path)
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            received.append(
                {
                    "method": self.command,
                    "path": url.path,
                    "query": parse_qs(url.query),
                    "body": json.loads(body) if body else None,
                    "authorization": self.headers.get("Authorization"),
                }
            )
            if self.command == "DELETE":
                self.send_response(204)
                self.end_headers()
                return

            data = (
                [RESOURCE]
                if url.path == COLLECTION and self.command == "GET"
                else RESOURCE
            )
            payload = json.dumps(data).encode()
            self.send_response(201 if self.command == "POST" else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PUT = do_DELETE = handle_request

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{server.server_port}"
        transport = bazooka.Client(default_timeout=2)
        client = base.CollectionBaseClient(
            endpoint,
            http_client=transport,
            auth=base.CoreIamAuthenticator(endpoint, access_token="test-access-token"),
        )
        try:
            yield client, received
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()


@pytest.mark.parametrize(
    "operation,args,kwargs,method,path,query,body,result",
    [
        pytest.param(
            "create",
            (),
            {"data": RESOURCE},
            "POST",
            COLLECTION,
            {},
            RESOURCE,
            RESOURCE,
            id="create",
        ),
        pytest.param(
            "get",
            (RESOURCE_UUID,),
            {},
            "GET",
            RESOURCE_PATH,
            {},
            None,
            RESOURCE,
            id="get",
        ),
        pytest.param(
            "filter",
            (),
            {"name": "sample"},
            "GET",
            COLLECTION,
            {"name": ["sample"]},
            None,
            [RESOURCE],
            id="filter",
        ),
        pytest.param(
            "update",
            (RESOURCE_UUID,),
            {"name": "sample"},
            "PUT",
            RESOURCE_PATH,
            {},
            {"name": "sample"},
            RESOURCE,
            id="update",
        ),
        pytest.param(
            "delete",
            (RESOURCE_UUID,),
            {},
            "DELETE",
            RESOURCE_PATH,
            {},
            None,
            None,
            id="delete",
        ),
        pytest.param(
            "do_action",
            ("inspect", RESOURCE_UUID),
            {"limit": 2},
            "GET",
            RESOURCE_PATH + "/actions/inspect",
            {"limit": ["2"]},
            None,
            RESOURCE,
            id="read-action",
        ),
        pytest.param(
            "do_action",
            ("publish", RESOURCE_UUID),
            {"invoke": True, "message": "sample"},
            "POST",
            RESOURCE_PATH + "/actions/publish/invoke",
            {},
            {"message": "sample"},
            RESOURCE,
            id="invoke-action",
        ),
    ],
)
def test_collection_request_over_http(
    http_collection, operation, args, kwargs, method, path, query, body, result
):
    client, received = http_collection

    assert getattr(client, operation)(COLLECTION, *args, **kwargs) == result
    assert received == [
        {
            "method": method,
            "path": path,
            "query": query,
            "body": body,
            "authorization": "Bearer test-access-token",
        }
    ]
