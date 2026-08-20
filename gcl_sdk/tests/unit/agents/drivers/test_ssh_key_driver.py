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

import pathlib
import uuid as sys_uuid

import pytest

from gcl_sdk.agents.universal.drivers import ssh_key

PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITest test-key"
OTHER_PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOther other-key"


def _make_key(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    line_ending: str = "\n",
    file_line_ending: str = "\n",
) -> tuple[ssh_key.SSHKey, pathlib.Path]:
    monkeypatch.setattr(ssh_key.SSHKey, "HOME", str(tmp_path))
    user_home = tmp_path / "test-user" / ".ssh"
    user_home.mkdir(parents=True)
    authorized_keys = user_home / "authorized_keys"
    authorized_keys.write_bytes(f"{PUBLIC_KEY}{file_line_ending}".encode())
    resource = ssh_key.SSHKey(
        uuid=sys_uuid.uuid4(),
        user="test-user",
        authorized_keys=".ssh/authorized_keys",
        target_public_key=f"{PUBLIC_KEY}{line_ending}",
    )
    return resource, authorized_keys


@pytest.mark.parametrize(
    ("line_ending", "file_line_ending"),
    [("", "\n"), ("\n", "\n"), ("\r\n", "\n"), ("\r\n", "\r\n")],
)
def test_ssh_key_accepts_serialized_trailing_newline(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    line_ending: str,
    file_line_ending: str,
):
    resource, authorized_keys = _make_key(
        tmp_path,
        monkeypatch,
        line_ending,
        file_line_ending,
    )

    resource.restore_from_dp()
    resource.dump_to_dp()

    assert authorized_keys.read_bytes() == f"{PUBLIC_KEY}{file_line_ending}".encode()


def test_ssh_key_dump_requires_an_exact_authorized_keys_line(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    resource, authorized_keys = _make_key(tmp_path, monkeypatch)
    authorized_keys.write_text(f"{PUBLIC_KEY}-different-comment\n")

    resource.dump_to_dp()

    assert authorized_keys.read_text().splitlines() == [
        f"{PUBLIC_KEY}-different-comment",
        PUBLIC_KEY,
    ]


@pytest.mark.parametrize("line_ending", ["\n", "\r\n"])
def test_ssh_key_delete_normalizes_serialized_trailing_newline(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    line_ending: str,
):
    resource, authorized_keys = _make_key(tmp_path, monkeypatch, line_ending)
    authorized_keys.write_text(f"{OTHER_PUBLIC_KEY}\n{PUBLIC_KEY}\n")

    resource.delete_from_dp()

    assert authorized_keys.read_text() == f"{OTHER_PUBLIC_KEY}\n"
