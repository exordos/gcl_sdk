#    Copyright 2025-2026 Genesis Corporation.
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

import abc
import base64
import datetime
import typing as tp
import uuid as sys_uuid

from restalchemy.api import packers as ra_packers
from restalchemy.dm import filters as dm_filters

from gcl_sdk.agents.universal.api import crypto as sdk_crypto
from gcl_sdk.agents.universal.dm import models

ENCRYPTED_JSON_CONTENT_TYPE = "application/x-genesis-agent-chacha20-poly1305-encrypted"

EXORDOS_NODE_UUID_HEADER = "X-Genesis-Node-UUID"
EXORDOS_NONCE_HEADER = "X-Genesis-Nonce"


class BaseEncryptionInformation(metaclass=abc.ABCMeta):
    def __init__(self, request):
        self._request = request
        super().__init__()

    @property
    @abc.abstractmethod
    def node_uuid(self) -> sys_uuid.UUID:
        raise NotImplementedError()

    @property
    @abc.abstractmethod
    def request_nonce(self) -> bytes:
        raise NotImplementedError()

    @property
    @abc.abstractmethod
    def request_nonce_base64(self) -> str:
        raise NotImplementedError()

    @property
    @abc.abstractmethod
    def response_nonce(self) -> bytes:
        raise NotImplementedError()

    @property
    @abc.abstractmethod
    def response_nonce_base64(self) -> str:
        raise NotImplementedError()

    @property
    @abc.abstractmethod
    def encryption_key(self) -> bytes:
        raise NotImplementedError()

    @abc.abstractmethod
    def is_requires_encryption(self) -> bool:
        raise NotImplementedError()


class EncryptionInformation(BaseEncryptionInformation):
    def __init__(self, request):
        super().__init__(request=request)
        self._node_encryption_key = None
        self._response_nonce = None
        self._request_nonce_base64 = None
        self._node_uuid = None

    @property
    def node_uuid(self) -> sys_uuid.UUID:
        if self._node_uuid is not None:
            return self._node_uuid

        node_uuid_str = self._request.headers.get(EXORDOS_NODE_UUID_HEADER)
        if node_uuid_str:
            self._node_uuid = sys_uuid.UUID(node_uuid_str)
            return self._node_uuid

        raise ValueError(f"{EXORDOS_NODE_UUID_HEADER} header is missing or invalid")

    @property
    def request_nonce(self) -> bytes:
        return base64.b64decode(self.request_nonce_base64)

    @property
    def request_nonce_base64(self) -> str:
        if self._request_nonce_base64 is not None:
            return self._request_nonce_base64

        self._request_nonce_base64 = self._request.headers.get(EXORDOS_NONCE_HEADER)
        if self._request_nonce_base64:
            return self._request_nonce_base64

        raise ValueError(f"{EXORDOS_NONCE_HEADER} header is missing or invalid")

    @property
    def response_nonce(self) -> bytes:
        if self._response_nonce is not None:
            return self._response_nonce

        self._response_nonce = sdk_crypto.generate_nonce()
        return self._response_nonce

    @property
    def response_nonce_base64(self) -> str:
        return base64.b64encode(self.response_nonce).decode()

    def _get_node_encryption_key(self):
        if self._node_encryption_key is not None:
            return self._node_encryption_key

        self._node_encryption_key = models.NodeEncryptionKey.objects.get_one(
            filters={"uuid": dm_filters.EQ(self.node_uuid)}
        )
        return self._node_encryption_key

    @property
    def encryption_key(self) -> bytes:
        return base64.b64decode(self._get_node_encryption_key().private_key.encode())

    def is_requires_encryption(self) -> bool:
        now = datetime.datetime.now(datetime.timezone.utc)
        node_encryption_key = self._get_node_encryption_key()
        return now >= node_encryption_key.encryption_disabled_until


class NoEncryptionInformation(BaseEncryptionInformation):
    @property
    def node_uuid(self) -> sys_uuid.UUID:
        raise NotImplementedError()

    @property
    def request_nonce(self) -> bytes:
        raise NotImplementedError()

    @property
    def request_nonce_base64(self) -> str:
        raise NotImplementedError()

    @property
    def response_nonce(self) -> bytes:
        raise NotImplementedError()

    @property
    def response_nonce_base64(self) -> str:
        raise NotImplementedError()

    @property
    def encryption_key(self) -> bytes:
        raise NotImplementedError()

    def is_requires_encryption(self) -> bool:
        return False


class ExordosAgentEncryptedJsonPacker(ra_packers.JSONPacker):
    """Packer for encrypted JSON payloads."""

    def pack(self, obj: tp.Any) -> tp.Any:
        plaintext = super().pack(obj)
        ctx = self._req.context
        encryption_key = ctx.encryption_information.encryption_key
        nonce = ctx.encryption_information.response_nonce

        return sdk_crypto.encrypt_chacha20_poly1305(
            plaintext=plaintext,
            key=encryption_key,
            nonce=nonce,
        )

    def unpack(self, value: tp.Any) -> tp.Any:
        ctx = self._req.context
        encryption_key = ctx.encryption_information.encryption_key
        nonce = ctx.encryption_information.request_nonce

        value = sdk_crypto.decrypt_chacha20_poly1305(
            ciphertext=value,
            key=encryption_key,
            nonce=nonce,
        )

        return super().unpack(value)


ra_packers.set_packer(ENCRYPTED_JSON_CONTENT_TYPE, ExordosAgentEncryptedJsonPacker)
