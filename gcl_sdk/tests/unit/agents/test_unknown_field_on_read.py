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

"""Reading a resource that carries a field this model does not have.

An installation is upgraded element by element, so a core answering with
a field an element's copy of these models predates is ordinary. It used
to take down every resource of the instance, and the traceback named
`get_custom_property_type` rather than the field -- measured on a stand
where a core carrying `tags` on its resource models met a released
element that did not.
"""

import uuid as sys_uuid

from restalchemy.dm import models as ra_models
from restalchemy.dm import properties
from restalchemy.dm import types

from gcl_sdk.agents.universal.dm import models


class _Leg(ra_models.ModelWithUUID, models.ResourceMixin):
    name = properties.property(types.String(max_length=32), required=True)


class _Sealed(
    ra_models.ModelWithUUID, ra_models.CustomPropertiesMixin, models.ResourceMixin
):
    """A model whose `__init__` needs a custom property, as `User` does."""

    __custom_properties__ = {"secret": types.String(max_length=32)}

    def __init__(self, secret, **kwargs):
        super().__init__(**kwargs)
        self.kept = secret


class TestAFieldThisModelDoesNotHave:
    def _resource(self, value):
        return models.Resource(
            uuid=sys_uuid.uuid4(),
            kind="leg",
            res_uuid=sys_uuid.uuid4(),
            value=value,
            hash="0" * 16,
            full_hash="0" * 16,
        )

    def test_the_field_is_skipped_and_the_rest_is_read(self):
        uuid = sys_uuid.uuid4()
        resource = self._resource({"uuid": str(uuid), "name": "eth0", "tags": ["one"]})

        leg = _Leg.from_ua_resource(resource)

        assert leg.uuid == uuid
        assert leg.name == "eth0"

    def test_a_resource_of_known_fields_only_is_read_whole(self):
        uuid = sys_uuid.uuid4()
        resource = self._resource({"uuid": str(uuid), "name": "eth0"})

        leg = _Leg.from_ua_resource(resource)

        assert leg.uuid == uuid
        assert leg.name == "eth0"


class TestACustomPropertyIsNotUnknown:
    """RESTAlchemy's own `skip_unknown_fields` would drop these -- it looks
    at the properties alone -- and a model that needs one in `__init__`
    would fail to build at all."""

    def test_it_reaches_the_model(self):
        uuid = sys_uuid.uuid4()
        resource = models.Resource(
            uuid=sys_uuid.uuid4(),
            kind="sealed",
            res_uuid=sys_uuid.uuid4(),
            value={"uuid": str(uuid), "secret": "hunter2", "tags": ["one"]},
            hash="0" * 16,
            full_hash="0" * 16,
        )

        sealed = _Sealed.from_ua_resource(resource)

        assert sealed.kept == "hunter2"
