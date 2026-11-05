# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0

import gzip
import io
from pathlib import Path
from unittest import mock
import uuid

from openstack.image.v2.image import Image
import pytest
import zstandard

from gcl_sdk.cloud import images
from gcl_sdk.cloud.drivers.base import CloudConfig
from gcl_sdk.cloud.drivers.openstack import OpenStackDriver


@pytest.fixture
def driver():
    images.CONF.set_override(
        "image_origins",
        ["https://repo.example", "http://repo.example"],
        group="openstack_cloud",
    )
    value = OpenStackDriver(
        CloudConfig(
            uuid.uuid4(),
            "https://identity.example/v3",
            {
                "application_credential_id": "test",
                "application_credential_secret": "secret",
            },
        ),
        connection=mock.Mock(),
    )
    value.api.image.images.return_value = []
    yield value
    images.CONF.clear_override("image_origins", group="openstack_cloud")


@pytest.mark.parametrize(
    "suffix,encode,payload,format",
    [
        (".raw", lambda b: b, b"raw image data", "raw"),
        (".raw.gz", gzip.compress, b"raw image data", "raw"),
        (".raw.zst", zstandard.ZstdCompressor().compress, b"raw image data", "raw"),
        (
            ".qcow2.zst",
            zstandard.ZstdCompressor().compress,
            b"QFI\xfbimage data",
            "qcow2",
        ),
    ],
)
def test_provider_uploads_uncompressed_image_privately(
    driver, monkeypatch, suffix, encode, payload, format
):
    monkeypatch.setattr(images, "_open", lambda *a, **kw: io.BytesIO(encode(payload)))

    captured = {}

    def create(cls, **values):
        assert values["visibility"] == "private"
        assert values["disk_format"] == format
        assert values["description"].startswith("exordos:" + str(driver.config.connection) + ":image:")
        return Image(status="queued", **values)

    def upload(self, session, *, data):
        captured["path"] = Path(data.name)
        assert data.read() == payload
        return mock.Mock(status_code=204)

    driver.api.image._create.side_effect = create
    driver.api.image.wait_for_status.side_effect = lambda image, **kw: image
    monkeypatch.setattr(Image, "upload", upload)
    result = images.resolve(driver, "https://repo.example/version/image" + suffix + "?download=1")
    assert result.id == str(uuid.uuid5(driver.config.connection, "image:https://repo.example/version/image" + suffix + "?download=1"))
    assert not captured["path"].exists()



def test_ready_import_is_reused_without_downloading_or_adopting_other_connection(
    driver, monkeypatch
):
    url = "https://repo.example/image.raw.zst"
    identity = uuid.uuid5(driver.config.connection, "image:" + url)
    own = Image(id="own", status="active", description=driver.marker("image", identity))
    other = Image(id="other", status="active", description="unmanaged")
    driver.api.image.images.return_value = [other, own]
    with mock.patch.object(images, "_open") as download:
        assert images.resolve(driver, url) is own
    download.assert_not_called()
    driver.api.image.delete_image.assert_not_called()
    driver.api.image.create_image.assert_not_called()


def test_failed_download_does_not_create_glance_resource(driver):
    with mock.patch.object(images, "_open", side_effect=OSError("download failed")):
        with pytest.raises(OSError):
            images.resolve(driver, "https://repo.example/image.raw")
    driver.api.image.create_image.assert_not_called()


def test_existing_glance_references_keep_the_native_contract(driver):
    assert (
        images.resolve(driver, "image-uuid") is driver.api.image.find_image.return_value
    )
    driver.api.image.find_image.assert_called_once_with(
        "image-uuid", ignore_missing=False
    )


def test_incomplete_owned_import_is_replaced_on_retry(driver, monkeypatch):
    url = "http://repo.example/image.raw"
    identity = uuid.uuid5(driver.config.connection, "image:" + url)
    old = Image(id="old", status="killed", description=driver.marker("image", identity))
    driver.api.image.images.return_value = [old]
    monkeypatch.setattr(images, "_open", lambda *a, **kw: io.BytesIO(b"raw"))
    monkeypatch.setattr(Image, "upload", lambda self, session, *, data: mock.Mock(status_code=204))
    driver.api.image._create.return_value = Image(id=str(identity), status="queued", description=driver.marker("image", identity))
    images.resolve(driver, url)
    driver.api.image.delete_image.assert_called_once_with("old", ignore_missing=True)
    driver.api.image.wait_for_delete.assert_called_once_with(old)


@pytest.mark.parametrize("status", ["saving", "importing", "uploading"])
def test_concurrent_import_waits_without_deleting_another_upload(driver, status):
    url = "http://repo.example/image.raw"
    identity = uuid.uuid5(driver.config.connection, "image:" + url)
    existing = Image(id=str(identity), status=status, description=driver.marker("image", identity))
    driver.api.image.images.return_value = [existing]
    result = images.resolve(driver, url)
    assert result is driver.api.image.wait_for_status.return_value
    driver.api.image.wait_for_status.assert_called_once_with(
        existing, status="active", failures=["killed", "deleted", "pending_delete"],
        wait=images.CONF.openstack_cloud.image_import_timeout,
    )
    driver.api.image.delete_image.assert_not_called()
    driver.api.image.create_image.assert_not_called()


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest",
        "http://127.0.0.1/",
        "https://user:secret@repo.example/image",
    ],
)
def test_untrusted_image_origin_is_rejected_before_network_access(driver, url):
    with mock.patch.object(images, "_open") as fetch:
        with pytest.raises(ValueError):
            images.resolve(driver, url)
    fetch.assert_not_called()


def test_redirect_cannot_leave_operator_image_origins(driver):
    handler = images._RedirectHandler()
    with pytest.raises(ValueError, match="origin"):
        handler.redirect_request(
            images.request.Request("https://repo.example/image"),
            None,
            302,
            "Found",
            {},
            "http://169.254.169.254/latest",
        )


@pytest.mark.parametrize("compressed", [False, True])
def test_image_byte_limit_covers_download_and_unpacked_content(
    driver, monkeypatch, compressed
):
    images.CONF.set_override("image_import_max_bytes", 100, group="openstack_cloud")
    payload = b"a" * 1000
    monkeypatch.setattr(
        images,
        "_open",
        lambda url: io.BytesIO(gzip.compress(payload) if compressed else payload),
    )
    try:
        with pytest.raises(ValueError, match="size limit"):
            images.resolve(
                driver, "https://repo.example/image.raw" + (".gz" if compressed else "")
            )
    finally:
        images.CONF.clear_override("image_import_max_bytes", group="openstack_cloud")
    driver.api.image.create_image.assert_not_called()


def test_import_preserves_temporary_filesystem_reserve(driver, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(images, "_open", lambda url: io.BytesIO(b"image"))
    monkeypatch.setattr(
        images.shutil, "disk_usage", lambda path: SimpleNamespace(free=1)
    )
    with pytest.raises(ValueError, match="temporary space"):
        images.resolve(driver, "https://repo.example/image.raw")
    driver.api.image.create_image.assert_not_called()


def test_download_is_bounded_by_total_time(driver, monkeypatch):
    monkeypatch.setattr(images, "_open", lambda url: io.BytesIO(b"image"))
    clock = iter([0, 0, 901])
    monkeypatch.setattr(images.time, "monotonic", lambda: next(clock))
    with pytest.raises(ValueError, match="timed out"):
        images.resolve(driver, "https://repo.example/image.raw")
    driver.api.image.create_image.assert_not_called()


def test_glance_wait_uses_remaining_import_deadline(driver, monkeypatch):
    monkeypatch.setattr(images, "_open", lambda url: io.BytesIO(b"image"))
    clock = iter([0, 100, 200, 250, 300, 400, 500])
    monkeypatch.setattr(images.time, "monotonic", lambda: next(clock, 500))
    driver.api.image._create.side_effect = lambda cls, **values: Image(status="queued", **values)
    monkeypatch.setattr(Image, "upload", lambda self, session, *, data: mock.Mock(status_code=204))
    images.resolve(driver, "https://repo.example/image.raw")
    assert 0 < driver.api.image.wait_for_status.call_args.kwargs["wait"] < 900


def test_abandoned_queued_image_is_uploaded_without_deletion(driver, monkeypatch):
    url = "https://repo.example/image.raw"
    identity = uuid.uuid5(driver.config.connection, "image:" + url)
    queued = Image(id=str(identity), status="queued", description=driver.marker("image", identity))
    driver.api.image.images.return_value = [queued]
    monkeypatch.setattr(images, "_open", lambda *_: io.BytesIO(b"raw"))
    with mock.patch.object(Image, "upload", return_value=mock.Mock(status_code=204)) as upload:
        images.resolve(driver, url)
    upload.assert_called_once()
    driver.api.image._create.assert_not_called()
    driver.api.image.delete_image.assert_not_called()
    driver.api.image.wait_for_status.assert_called_once()


def test_create_collision_returns_owned_active_image_without_deleting(driver, monkeypatch):
    from openstack import exceptions

    url = "https://repo.example/image.raw"
    identity = uuid.uuid5(driver.config.connection, "image:" + url)
    existing = Image(id=str(identity), status="active", description=driver.marker("image", identity))
    driver.api.image._create.side_effect = exceptions.ConflictException("already exists")
    driver.api.image.get_image.return_value = existing
    monkeypatch.setattr(images, "_open", lambda *_: io.BytesIO(b"raw"))
    assert images.resolve(driver, url) is existing
    driver.api.image.delete_image.assert_not_called()


@pytest.mark.parametrize("status", [409, 410])
def test_upload_collision_waits_for_the_owned_image_without_deleting(driver, monkeypatch, status):
    from openstack import exceptions
    from requests import Response

    url = "https://repo.example/image.raw"
    identity = uuid.uuid5(driver.config.connection, "image:" + url)
    queued = Image(id=str(identity), status="queued", description=driver.marker("image", identity))
    saving = Image(id=str(identity), status="saving", description=driver.marker("image", identity))
    driver.api.image.images.return_value = [queued]
    driver.api.image.get_image.return_value = saving
    monkeypatch.setattr(images, "_open", lambda *_: io.BytesIO(b"raw"))
    response = Response()
    response.status_code = status
    monkeypatch.setattr(Image, "upload", mock.Mock(side_effect=exceptions.HttpException(response=response)))
    images.resolve(driver, url)
    driver.api.image.wait_for_status.assert_called_once()
    assert driver.api.image.wait_for_status.call_args.args[0] is saving
    driver.api.image.delete_image.assert_not_called()
