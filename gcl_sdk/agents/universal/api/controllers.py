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

import base64

from restalchemy.api import controllers

from gcl_sdk.agents.universal.api import packers


class SdkEncryptionHeadersMixin:
    def _apply_encryption_headers(self, headers):
        encryption_information = self._req.context.encryption_information
        if not encryption_information.is_requires_encryption():
            return headers

        headers["Content-Type"] = (
            self._req.content_type or packers.ENCRYPTED_JSON_CONTENT_TYPE
        )
        headers[packers.EXORDOS_NONCE_HEADER] = base64.b64encode(
            encryption_information.response_nonce
        ).decode()
        headers[packers.EXORDOS_NODE_UUID_HEADER] = str(
            encryption_information.node_uuid
        )
        return headers


class BaseSdkResourceController(
    SdkEncryptionHeadersMixin, controllers.BaseResourceController
):
    def process_result(self, result, status_code=200, headers=None, add_location=False):
        headers = headers or {}
        headers = self._apply_encryption_headers(headers)

        return super().process_result(
            result,
            status_code=status_code,
            headers=headers,
            add_location=add_location,
        )


class BaseSdkNestedResourceController(
    SdkEncryptionHeadersMixin, controllers.BaseNestedResourceController
):
    def process_result(self, result, status_code=200, headers=None, add_location=False):
        headers = headers or {}
        headers = self._apply_encryption_headers(headers)

        return super().process_result(
            result,
            status_code=status_code,
            headers=headers,
            add_location=add_location,
        )
