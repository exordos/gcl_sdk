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

"""The form a timestamp takes between an agent and the control plane.

An agent and the plane it talks to are upgraded at different times, so
what one writes the other has to read -- including the agent that has
not been upgraded yet. These spell out both halves of that, because the
form is decided by RESTAlchemy and changed once already: 16.0.0 writes a
timestamp landing on a whole second as `...:56Z`, where 15.x wrote
`...:56.000000Z`, and 15.x cannot read what 16 writes.
"""

import datetime
import uuid as sys_uuid

import orjson
import webob
from restalchemy.api import constants
from restalchemy.api import contexts
from restalchemy.api import packers
from restalchemy.dm import types

from gcl_sdk.agents.universal.status_api import controllers as status_controllers
from gcl_sdk.agents.universal.status_api.dm import models

WHOLE_SECOND = datetime.datetime(2026, 8, 16, 12, 34, 56, tzinfo=datetime.timezone.utc)
WITH_MICROSECONDS = WHOLE_SECOND.replace(microsecond=123456)


def _request():
    req = webob.Request.blank("/resources/")
    req.api_context = contexts.RequestContext(req)
    req.api_context.set_active_method(constants.GET)
    return req


def _packed(created_at):
    # `created_at` is read only, so the agent is restored the way a
    # stored row arrives rather than constructed.
    agent = models.UniversalAgent.restore(
        uuid=sys_uuid.uuid4(),
        name="probe",
        node=sys_uuid.uuid4(),
        created_at=created_at,
        updated_at=created_at,
    )
    # The resource an agent is actually answered from, so the field
    # names and the form are the ones that go over the wire.
    rt = status_controllers.UniversalAgentsController.__resource__
    return orjson.loads(packers.JSONPacker(rt, _request()).pack(agent))


def test_a_whole_second_is_written_without_a_fraction():
    # The form 15.x cannot read. It is what an agent is handed now, so
    # an agent older than this cannot be talked to -- see the module
    # docstring before changing this.
    assert _packed(WHOLE_SECOND)["created_at"] == "2026-08-16T12:34:56Z"


def test_a_fraction_is_written_out_in_full():
    assert _packed(WITH_MICROSECONDS)["created_at"] == "2026-08-16T12:34:56.123456Z"


def test_both_forms_are_read_back():
    # A control plane still has to read what an older agent writes.
    utc = types.UTCDateTimeZ()
    for written in (
        "2026-08-16T12:34:56Z",
        "2026-08-16T12:34:56.000000Z",
        "2026-08-16T12:34:56.123456Z",
        "2026-08-16T12:34:56+00:00",
    ):
        assert utc.from_simple_type(written) == WHOLE_SECOND.replace(
            microsecond=123456 if "123456" in written else 0
        )
