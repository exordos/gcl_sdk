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

    def upload(**values):
        assert Path(values["filename"]).read_bytes() == payload
        assert values["visibility"] == "private"
        assert values["disk_format"] == format
        assert values["description"].startswith(
            "exordos:" + str(driver.config.connection) + ":image:"
        )
        return Image(
            id="uploaded",
            status="active",
            **{k: v for k, v in values.items() if k in ["name", "description"]},
        )

    driver.api.image.create_image.side_effect = upload
    result = images.resolve(
        driver, "https://repo.example/version/image" + suffix + "?download=1"
    )
    assert result.id == "uploaded"
    assert not Path(driver.api.image.create_image.call_args.kwargs["filename"]).exists()


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
    old = Image(id="old", status="queued", description=driver.marker("image", identity))
    driver.api.image.images.return_value = [old]
    monkeypatch.setattr(images, "_open", lambda *a, **kw: io.BytesIO(b"raw"))
    images.resolve(driver, url)
    driver.api.image.delete_image.assert_called_once_with("old", ignore_missing=True)
    driver.api.image.wait_for_delete.assert_called_once_with(old)


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
    monkeypatch.setattr(images.time, "monotonic", lambda: next(clock))
    images.resolve(driver, "https://repo.example/image.raw")
    assert driver.api.image.create_image.call_args.kwargs["timeout"] == 400
