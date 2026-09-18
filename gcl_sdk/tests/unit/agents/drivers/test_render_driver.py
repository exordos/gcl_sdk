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

import grp
import os
import pwd
import stat
import uuid as sys_uuid

import pytest

from gcl_sdk.agents.universal.drivers import render


def _render(path, content="new", mode="0600"):
    # The current user, so the test doesn't need root to chown
    return render.Render(
        uuid=sys_uuid.uuid4(),
        path=str(path),
        content=content,
        mode=mode,
        owner=pwd.getpwuid(os.getuid()).pw_name,
        group=grp.getgrgid(os.getgid()).gr_name,
        on_change=render.OnChangeNoAction(),
    )


def test_dump_writes_the_content_with_the_mode(tmp_path):
    path = tmp_path / "sub" / "init.txt"

    _render(path, mode="0640").dump_to_dp()

    assert path.read_text() == "new"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


@pytest.mark.parametrize("action", ["dump_to_dp", "update_on_dp"])
def test_readers_see_the_old_file_until_the_new_one_is_complete(
    tmp_path, monkeypatch, action
):
    path = tmp_path / "init.txt"
    path.write_text("old")
    seen = []
    real_replace = os.replace

    def replace(src, dst):
        # The moment the new content becomes visible
        seen.append(
            (
                path.read_text(),
                open(src).read(),
                stat.S_IMODE(os.stat(src).st_mode),
            )
        )
        real_replace(src, dst)

    monkeypatch.setattr(render.os, "replace", replace)

    getattr(_render(path, content="new", mode="0600"), action)()

    # Until then the whole old file, and the new one is already complete and
    # as closed as it's meant to be
    assert seen == [("old", "new", 0o600)]
    assert path.read_text() == "new"


def test_failed_write_leaves_the_old_file_and_no_temporary_one(tmp_path, monkeypatch):
    path = tmp_path / "init.txt"
    path.write_text("old")

    def replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(render.os, "replace", replace)

    with pytest.raises(OSError, match="disk full"):
        _render(path).dump_to_dp()

    assert path.read_text() == "old"
    assert os.listdir(tmp_path) == ["init.txt"]


def test_unknown_owner_is_rejected_before_anything_is_written(tmp_path):
    path = tmp_path / "init.txt"
    model = _render(path)
    model.owner = "no-such-user-" + sys_uuid.uuid4().hex[:8]

    with pytest.raises(ValueError, match="does not exist"):
        model.dump_to_dp()

    assert os.listdir(tmp_path) == []
