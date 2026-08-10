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

from __future__ import annotations

from restalchemy.common import contexts as ra_contexts

from gcl_sdk.agents.universal.api import packers


class SdkEncryptionInformationContext(ra_contexts.Context):
    def __init__(
        self,
        request,
        engine_name: str | None = None,
        encryption_information_class: type[
            packers.EncryptionInformation
        ] = packers.EncryptionInformation,
    ):
        if engine_name is None:
            super().__init__()
        else:
            super().__init__(engine_name=engine_name)

        self._encryption_information = encryption_information_class(
            request=request,
        )

    @property
    def encryption_information(self) -> packers.EncryptionInformation:
        return self._encryption_information
