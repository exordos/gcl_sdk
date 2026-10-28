# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0

"""Stream ordinary Exordos image URLs into private, connection-owned Glance images."""

import contextlib
import gzip
from pathlib import Path
import shutil
import tempfile
import time
from urllib import parse
from urllib import request
import uuid

from oslo_config import cfg

CONF = cfg.CONF
CONF.register_opts(
    [
        cfg.ListOpt("image_origins", default=["https://repo.exordos.com"]),
        cfg.IntOpt("image_import_max_bytes", default=64 * 1024**3, min=1),
        cfg.IntOpt("image_import_timeout", default=900, min=1),
        cfg.IntOpt("image_import_free_bytes", default=1024**3, min=0),
    ],
    group="openstack_cloud",
)


def _origin(url):
    value = parse.urlsplit(url)
    if (
        value.scheme not in {"http", "https"}
        or not value.hostname
        or value.username is not None
        or value.password is not None
    ):
        raise ValueError("Invalid cloud image URL")
    return (
        value.scheme,
        value.hostname.lower(),
        value.port or (443 if value.scheme == "https" else 80),
    )


def _validate_url(url):
    if _origin(url) not in {_origin(v) for v in CONF.openstack_cloud.image_origins}:
        raise ValueError("Cloud image origin is not allowed by the operator")


class _RedirectHandler(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url):
    return request.build_opener(_RedirectHandler()).open(url, timeout=60)


class _BoundedReader:
    def __init__(self, source, deadline):
        self.source, self.deadline, self.count = source, deadline, 0

    def read(self, size=-1):
        if time.monotonic() >= self.deadline:
            raise ValueError("Cloud image import timed out")
        chunk = self.source.read(size)
        self.count += len(chunk)
        if self.count > CONF.openstack_cloud.image_import_max_bytes:
            raise ValueError("Cloud image download exceeds the provider size limit")
        if time.monotonic() >= self.deadline:
            raise ValueError("Cloud image import timed out")
        return chunk

    def close(self):
        self.source.close()


def _copy(source, output, directory, deadline):
    total = 0
    while chunk := source.read(1024 * 1024):
        total += len(chunk)
        if time.monotonic() >= deadline:
            raise ValueError("Cloud image import timed out")
        if total > CONF.openstack_cloud.image_import_max_bytes:
            raise ValueError("Cloud image exceeds the provider unpacked size limit")
        if shutil.disk_usage(directory).free - len(chunk) < (
            CONF.openstack_cloud.image_import_free_bytes
        ):
            raise ValueError("Insufficient temporary space for cloud image import")
        output.write(chunk)


def resolve(driver, reference):
    if parse.urlsplit(reference).scheme not in {"http", "https"}:
        return driver.api.image.find_image(reference, ignore_missing=False)
    _validate_url(reference)
    identity = uuid.uuid5(driver.config.connection, "image:" + reference)
    found = driver._find(driver.api.image.images(), "image", identity)
    if found and found.status == "active":
        return found
    if found:
        driver._require(found, "image")
        driver.api.image.delete_image(found.id, ignore_missing=True)
        driver.api.image.wait_for_delete(found)
    with tempfile.TemporaryDirectory(prefix="exordos-cloud-image-") as directory:
        filename = Path(directory) / "image"
        deadline = time.monotonic() + CONF.openstack_cloud.image_import_timeout
        with contextlib.ExitStack() as stack:
            source = _BoundedReader(stack.enter_context(_open(reference)), deadline)
            path = parse.urlsplit(reference).path.lower()
            if path.endswith(".gz"):
                source = stack.enter_context(gzip.GzipFile(fileobj=source))
            elif path.endswith((".zst", ".zstd")):
                import zstandard

                source = stack.enter_context(
                    zstandard.ZstdDecompressor().stream_reader(source)
                )
            with filename.open("wb") as output:
                _copy(source, output, directory, deadline)
        with filename.open("rb") as image:
            magic = image.read(4)
        if not magic:
            raise ValueError("Cloud image download is empty")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("Cloud image import timed out")
        return driver.api.image.create_image(
            name="exordos-image-" + identity.hex,
            filename=str(filename),
            disk_format="qcow2" if magic == b"QFI\xfb" else "raw",
            container_format="bare",
            visibility="private",
            description=driver.marker("image", identity),
            allow_duplicates=True,
            wait=True,
            timeout=remaining,
        )
