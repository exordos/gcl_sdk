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

"""A writable `local_dir` (WebDAV) guarded by an `auth_request` modifier:
nginx asks a backend pool whether each request may pass and serves or stores
the files itself."""

import shutil
import subprocess
import uuid as sys_uuid

import pytest

from gcl_sdk.agents.universal.drivers import lb as lb_driver

UUID_A = sys_uuid.UUID("00000000-0000-4000-8000-00000000000a")
POOL = "00000000-0000-4000-8000-0000000000aa"


def _vhost(action, modifiers):
    return {
        "uuid": str(UUID_A),
        "proto": "http",
        "port": 80,
        "domains": ["_"],
        "cert": None,
        "ext_sources": [],
        "proxy_proto_from": None,
        "routes": {
            str(sys_uuid.uuid5(UUID_A, "route")): {
                "cond": {
                    "kind": "prefix",
                    "value": "/repo/",
                    "actions": [action],
                    "modifiers": modifiers,
                    "allowed_ips": ["0.0.0.0/0"],
                }
            }
        },
    }


DAV_ACTION = {
    "kind": "local_dir",
    "path": "/var/www/repo",
    "is_spa": False,
    "dav_methods": ["PUT", "DELETE", "MKCOL", "MOVE"],
}
AUTH_MODIFIER = {"kind": "auth_request", "pool": POOL, "path": "/v1/repo/auth"}
POOLS = {
    POOL: {
        "endpoints": [{"kind": "host", "host": "127.0.0.1", "port": 11010, "weight": 1}]
    }
}


@pytest.fixture
def make_lb(tmp_path, monkeypatch):
    monkeypatch.setattr(lb_driver.LB, "META_PATH", str(tmp_path / "lb_meta.json"))

    def make(vhost):
        return lb_driver.LB(uuid=UUID_A, vhosts=[vhost], backend_pools=POOLS)

    return make


def _render(lb):
    _l4, vhosts_l7, _ext = lb._gen_vhosts()
    return lb._gen_file_content_l7(vhosts_l7)


def test_dav_dir_is_writable_and_not_a_spa(make_lb):
    conf = _render(make_lb(_vhost({**DAV_ACTION, "is_spa": True}, [])))

    assert "dav_methods PUT DELETE MKCOL MOVE;" in conf
    assert "create_full_put_path on;" in conf
    # try_files would send a PUT of a new file to /index.html.
    assert "try_files" not in conf


def test_plain_local_dir_is_unchanged(make_lb):
    action = {"kind": "local_dir", "path": "/var/www/site", "is_spa": True}
    conf = _render(make_lb(_vhost(action, [])))

    assert "try_files $uri $uri/ /index.html;" in conf
    assert "dav_methods" not in conf


def test_auth_request_points_at_an_internal_backend_location(make_lb):
    lb = make_lb(_vhost(DAV_ACTION, [AUTH_MODIFIER]))
    conf = _render(lb)
    v = lb.vhosts[0]
    key = lb._route_key(v, next(iter(v["routes"].values()))["cond"])

    assert f"auth_request /_exordos_auth_{key};" in conf
    auth_loc = conf.split(f"location = /_exordos_auth_{key} {{")[1].split("}")[0]
    assert "internal;" in auth_loc
    assert f"proxy_pass http://{POOL}/v1/repo/auth;" in auth_loc
    # The backend decides from the original request, never from its body.
    assert "proxy_pass_request_body off;" in auth_loc
    assert "proxy_set_header X-Original-Method $request_method;" in auth_loc
    assert "proxy_set_header X-Original-URI $request_uri;" in auth_loc
    assert "proxy_set_header X-Original-Addr $remote_addr;" in auth_loc


def test_no_auth_location_without_the_modifier(make_lb):
    conf = _render(make_lb(_vhost(DAV_ACTION, [])))

    assert "auth_request" not in conf
    assert "_exordos_auth_" not in conf


def test_dav_dir_is_created_for_nginx(make_lb, tmp_path, monkeypatch):
    path = tmp_path / "repo"
    lb = make_lb(_vhost({**DAV_ACTION, "path": str(path)}, []))
    chowned = []
    monkeypatch.setattr(lb_driver.shutil, "chown", lambda p, **kw: chowned.append(p))

    lb._ensure_dav_dirs()

    assert path.is_dir()
    assert chowned == [str(path)]


@pytest.mark.skipif(shutil.which("nginx") is None, reason="nginx is not installed")
def test_rendered_config_is_accepted_by_nginx(make_lb, tmp_path):
    # `nginx -t` binds the listen port, so stay unprivileged.
    lb = make_lb({**_vhost(DAV_ACTION, [AUTH_MODIFIER]), "port": 18080})
    (tmp_path / "lb.conf").write_text(_render(lb))
    (tmp_path / "nginx.conf").write_text(
        f"""\
pid {tmp_path}/nginx.pid;
error_log {tmp_path}/error.log;
events {{}}
http {{
    access_log off;
    client_body_temp_path {tmp_path}/body;
    include {tmp_path}/lb.conf;
}}
"""
    )

    res = subprocess.run(
        ["nginx", "-t", "-p", str(tmp_path), "-c", str(tmp_path / "nginx.conf")],
        capture_output=True,
        text=True,
    )

    assert res.returncode == 0, res.stderr
