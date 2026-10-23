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

import uuid

from cryptography.exceptions import InvalidTag
import pytest

from gcl_sdk.agents.universal.api import boot_identity
from gcl_sdk.agents.universal.api import crypto


@pytest.mark.parametrize("response", [False, True])
def test_boot_cipher_binds_host_request_and_direction(response):
    key = b"k" * 32
    host, request = str(uuid.uuid4()), str(uuid.uuid4())
    value = {"secret": "sentinel", "node": str(uuid.uuid4())}
    sealed = boot_identity.encrypt(key, host, request, value, response=response)
    assert b"sentinel" not in sealed
    assert value == boot_identity.decrypt(key, host, request, sealed, response=response)
    for bad_host, bad_request, direction, ciphertext in [
        (str(uuid.uuid4()), request, response, sealed),
        (host, str(uuid.uuid4()), response, sealed),
        (host, request, not response, sealed),
        (host, request, response, sealed[:-1] + bytes([sealed[-1] ^ 1])),
    ]:
        with pytest.raises(InvalidTag):
            boot_identity.decrypt(
                key, bad_host, bad_request, ciphertext, response=direction
            )
    with pytest.raises(InvalidTag):
        # An ordinary agent ciphertext cannot cross the protocol boundary.
        crypto.decrypt_chacha20_poly1305(key, sealed[:12], sealed[12:])
