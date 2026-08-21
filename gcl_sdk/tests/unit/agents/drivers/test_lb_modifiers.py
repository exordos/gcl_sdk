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

"""Header modifiers differ by traffic direction: `set_header` rewrites the
request nginx forwards to the backend, `set_resp_header` decorates the response
nginx returns to the client. Rendering one as the other silently sends the
header to the wrong side of the proxy."""

import uuid as sys_uuid

import pytest

from gcl_sdk.agents.universal.drivers import lb as lb_driver

UUID_A = sys_uuid.UUID("00000000-0000-4000-8000-00000000000a")


@pytest.fixture
def lb(tmp_path, monkeypatch):
    monkeypatch.setattr(lb_driver.LB, "META_PATH", str(tmp_path / "lb_meta.json"))
    return lb_driver.LB(uuid=UUID_A, vhosts=[], backend_pools={})


VHOST = {"port": 8443}
ROUTE = {"value": "/api"}


def test_set_header_goes_upstream(lb):
    mods = lb._gen_modifiers(
        VHOST,
        ROUTE,
        [{"kind": "set_header", "name": "X-Real-IP", "value": "$remote_addr"}],
    )
    assert mods == ['proxy_set_header "X-Real-IP" "$remote_addr";']


def test_add_header_goes_back_to_the_client(lb):
    mods = lb._gen_modifiers(
        VHOST,
        ROUTE,
        [{"kind": "set_resp_header", "name": "X-Frame-Options", "value": "DENY"}],
    )
    # `always` so error responses carry the header too -- without it nginx
    # drops it on 4xx/5xx, which is exactly when e.g. CORS headers matter.
    assert mods == ['add_header "X-Frame-Options" "DENY" always;']


def test_both_directions_render_side_by_side(lb):
    mods = lb._gen_modifiers(
        VHOST,
        ROUTE,
        [
            {"kind": "set_header", "name": "X-Tenant", "value": "acme"},
            {"kind": "set_resp_header", "name": "X-Tenant", "value": "acme"},
        ],
    )
    assert mods == [
        'proxy_set_header "X-Tenant" "acme";',
        'add_header "X-Tenant" "acme" always;',
    ]


def test_add_header_value_quotes_are_escaped(lb):
    # An unescaped quote would end the directive early and break the config.
    mods = lb._gen_modifiers(
        VHOST,
        ROUTE,
        [
            {
                "kind": "set_resp_header",
                "name": 'X-"Odd"',
                "value": 'a"; return 444; #',
            }
        ],
    )
    assert mods == ['add_header "X-\\"Odd\\"" "a\\"; return 444; #" always;']


# A browser preflight only accepts the response when every one of these is
# present, so CORS is the canonical multi-header `add_header` case.
CORS = [
    ("Access-Control-Allow-Origin", "https://app.example"),
    ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
    ("Access-Control-Allow-Headers", "Authorization, Content-Type"),
    ("Access-Control-Allow-Credentials", "true"),
    ("Access-Control-Max-Age", "86400"),
]


def _cors_modifiers():
    return [{"kind": "set_resp_header", "name": n, "value": v} for n, v in CORS]


def test_cors_headers_render_in_the_declared_order(lb):
    mods = lb._gen_modifiers(VHOST, ROUTE, _cors_modifiers())

    assert mods == [
        'add_header "Access-Control-Allow-Origin" "https://app.example" always;',
        'add_header "Access-Control-Allow-Methods" "GET, POST, OPTIONS" always;',
        'add_header "Access-Control-Allow-Headers" "Authorization, Content-Type" always;',
        'add_header "Access-Control-Allow-Credentials" "true" always;',
        'add_header "Access-Control-Max-Age" "86400" always;',
    ]


def _cors_vhost():
    route = sys_uuid.uuid5(UUID_A, "route")
    return {
        "uuid": str(UUID_A),
        "proto": "http",
        "port": 80,
        "domains": ["api.example"],
        "cert": None,
        "ext_sources": [],
        "proxy_proto_from": None,
        "routes": {
            str(route): {
                "cond": {
                    "kind": "prefix",
                    "value": "/api",
                    "actions": [
                        {
                            "kind": "backend",
                            "pool": "pool_a",
                            "protocol": {"kind": "http"},
                        }
                    ],
                    "modifiers": _cors_modifiers(),
                    "allowed_ips": ["0.0.0.0/0"],
                }
            }
        },
    }


def test_cors_headers_land_inside_the_route_location(lb):
    rendered = lb._gen_vhost_l7(_cors_vhost())

    # Everything from `location /api {` up to its closing brace: a CORS
    # policy that leaked to server level would apply to every other route.
    body = rendered.split("location  /api {", 1)[1].split("}", 1)[0]
    for name, value in CORS:
        assert f'add_header "{name}" "{value}" always;' in body
    assert "proxy_pass http://pool_a;" in body


def test_cors_preflight_headers_survive_a_non_2xx_response(lb):
    # Browsers reject a preflight whose 4xx response lost the CORS headers,
    # so every rendered add_header must carry `always`.
    mods = lb._gen_modifiers(VHOST, ROUTE, _cors_modifiers())

    assert all(m.endswith(" always;") for m in mods)
