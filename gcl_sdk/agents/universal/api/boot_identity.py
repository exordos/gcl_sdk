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

import json

from gcl_sdk.agents.universal.api import crypto

HOST_HEADER = "X-Exordos-Boot-Host"
NONCE_HEADER = "X-Exordos-Boot-Nonce"
CONTENT_TYPE = "application/x-exordos-boot-proxy-encrypted"
RELAY_PATH = "/v1/boot-proxy"
MAX_AGE = 60
MAX_BODY = 2 << 20
MAX_RESPONSE = 32 << 20
NODE_ENV = "exordos.boot.node"


def _aad(host, request_id, response):
    return json.dumps(
        [
            "exordos-boot-proxy-v1",
            str(host),
            str(request_id),
            "response" if response else "request",
        ],
        separators=(",", ":"),
    ).encode()


def encrypt(key, host, request_id, value, *, response=False):
    """Use the agent cipher with a distinct protocol and request binding."""
    nonce = crypto.generate_nonce()
    plaintext = json.dumps(value, separators=(",", ":")).encode()
    return nonce + crypto.encrypt_chacha20_poly1305(
        key, plaintext, nonce, _aad(host, request_id, response)
    )


def decrypt(key, host, request_id, body, *, response=False):
    plaintext = crypto.decrypt_chacha20_poly1305(
        key=key,
        ciphertext=body[crypto.NONCE_SIZE :],
        nonce=body[: crypto.NONCE_SIZE],
        associated_data=_aad(host, request_id, response),
    )
    return json.loads(plaintext)
