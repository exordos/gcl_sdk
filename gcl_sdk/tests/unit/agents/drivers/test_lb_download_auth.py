#    Copyright 2026 Genesis Corporation.
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

import http.server
import io
import json
import shutil
import tarfile
import threading
import uuid

import pytest

from gcl_sdk.agents.universal.drivers import lb as lb_driver

TOKEN = 'Bearer private-"quoted"-\\token'


@pytest.fixture
def server():
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        info = tarfile.TarInfo("index.html")
        content = b"<title>Clock</title>"
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            if self.path.startswith("/redirect"):
                self.send_response(302)
                self.send_header("Location", "/stolen.tar.gz")
                self.end_headers()
                return
            if (
                self.path == "/private.tar.gz"
                and self.headers.get("Authorization") != TOKEN
            ):
                self.send_response(401)
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(archive.getvalue())

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", seen
    finally:
        httpd.shutdown()
        thread.join()
        httpd.server_close()


@pytest.fixture
def downloader(tmp_path, monkeypatch):
    if not all(shutil.which(cmd) for cmd in ("wget", "tar")):
        pytest.skip("wget and tar are required")
    target, staging = tmp_path / "target", tmp_path / "staging"
    target.mkdir()
    staging.mkdir()
    monkeypatch.setattr(lb_driver, "DOWNLOAD_DIR", str(target))
    monkeypatch.setattr(lb_driver, "DOWNLOAD_DIR_TMP", str(staging))
    monkeypatch.setattr(lb_driver.shutil, "chown", lambda *a, **kw: None)
    monkeypatch.setattr(lb_driver.LB, "META_PATH", str(tmp_path / "meta.json"))
    popen = lb_driver.subprocess.Popen
    commands = []

    def run(args, **kwargs):
        commands.append(args)
        # Exercise real wget and tar as the test user, without requiring root.
        if args[:3] == ("sudo", "-u", "www-data"):
            args = args[3:]
        return popen(args, **kwargs)

    monkeypatch.setattr(lb_driver.subprocess, "Popen", run)
    lb = lb_driver.LB(uuid=uuid.uuid4(), vhosts=[], backend_pools={})
    return lb, target, commands


@pytest.mark.parametrize("headers", [None, {"Authorization": TOKEN}])
def test_download_extracts_archive(server, downloader, headers, caplog):
    base, seen = server
    lb, target, commands = downloader
    url = base + ("/private.tar.gz" if headers else "/public.tar.gz")

    assert lb._download_url("site", url, headers) is True
    assert (target / "site" / "index.html").read_text() == "<title>Clock</title>"
    assert seen == [(url.removeprefix(base), TOKEN if headers else None)]
    assert TOKEN not in repr(commands)
    assert TOKEN not in json.dumps(lb._download_dirs)
    assert TOKEN not in caplog.text
    if not headers:
        assert lb._download_dirs["site"] == url


def test_unauthorized_download_does_not_replace_existing_site(server, downloader):
    base, _ = server
    lb, target, _ = downloader
    site = target / "site"
    site.mkdir()
    (site / "index.html").write_text("old site")

    result = lb._download_url("site", base + "/private.tar.gz")

    assert "401" in result
    assert (site / "index.html").read_text() == "old site"
    assert "site" not in lb._download_dirs


def test_authenticated_download_refuses_redirect(server, downloader):
    base, seen = server
    lb, target, _ = downloader

    result = lb._download_url(
        "site", base + "/redirect.tar.gz", {"Authorization": TOKEN}
    )

    assert result is not True
    assert seen == [("/redirect.tar.gz", TOKEN)]
    assert not (target / "site").exists()
    assert "site" not in lb._download_dirs


def test_public_download_still_follows_redirect(server, downloader):
    base, seen = server
    lb, target, _ = downloader

    assert lb._download_url("site", base + "/redirect.tar.gz") is True
    assert seen == [("/redirect.tar.gz", None), ("/stolen.tar.gz", None)]
    assert (target / "site" / "index.html").is_file()


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization\n": "value"},
        {"Authorization": "value\nheader = injected"},
        {"Authorization": "value\r\nX-Injected: yes"},
        {"Authorization": "value\x00"},
        {"Authorization": None},
    ],
)
def test_invalid_headers_are_rejected_before_starting_process(downloader, headers):
    lb, _, commands = downloader
    with pytest.raises(ValueError, match="Invalid download HTTP header"):
        lb._download_url("site", "https://example.com/site.tar.gz", headers)
    assert commands == []


def test_rotated_credentials_trigger_download(downloader, monkeypatch):
    lb, target, _ = downloader
    url = "https://example.com/site.tar.gz"
    old = {"Authorization": "Bearer old"}
    new = {"Authorization": "Bearer new"}
    (target / "site").mkdir()
    lb._download_dirs["site"] = lb._download_key(url, old)
    monkeypatch.setattr(lb, "_get_target_paths", lambda _: {"site": (url, new)})
    submitted = []
    monkeypatch.setattr(lb_driver.TPOOL, "submit", lambda *a: submitted.append(a))
    monkeypatch.setattr(lb, "_download_dirs_futures", {})

    assert lb._validate_downloaded_dirs([]) is False
    lb._actualize_downloaded_dirs([])

    assert submitted == [(lb._download_url, "site", url, new)]


def test_route_headers_are_download_only(downloader):
    lb, _, _ = downloader
    url = "https://example.com/site.tar.gz"
    condition = {
        "kind": "prefix",
        "value": "/",
        "allowed_ips": ["0.0.0.0/0"],
        "actions": [
            {
                "kind": "local_dir_download",
                "url": url,
                "headers": {"Authorization": TOKEN},
                "is_spa": True,
            }
        ],
        "modifiers": [],
    }
    vhost = {
        "uuid": str(uuid.uuid4()),
        "proto": "http",
        "port": 80,
        "domains": ["_"],
        "cert": None,
        "ext_sources": [],
        "proxy_proto_from": None,
        "routes": {"site": {"cond": condition}},
    }
    lb.vhosts = [vhost]
    targets = lb._get_target_paths([vhost])
    assert list(targets.values()) == [(url, {"Authorization": TOKEN})]
    _, rendered, _ = lb._gen_vhosts([vhost])
    config = lb._gen_file_content_l7(rendered, {})
    assert TOKEN not in config
    assert "Authorization" not in config
