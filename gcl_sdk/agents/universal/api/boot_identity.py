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

import hashlib
import hmac
import json

HOST_HEADER = "X-Exordos-Boot-Host"
TIME_HEADER = "X-Exordos-Boot-Time"
NONCE_HEADER = "X-Exordos-Boot-Nonce"
SIGNATURE_HEADER = "X-Exordos-Boot-Signature"
MAX_AGE = 60


def signature(key, host, node, timestamp, nonce, method, path, body):
    """Bind the delegated caller to exactly one Boot API request."""
    message = json.dumps(
        [
            str(host),
            str(node),
            str(timestamp),
            nonce,
            method,
            path,
            hashlib.sha256(body).hexdigest(),
        ],
        separators=(",", ":"),
    ).encode()
    return hmac.new(
        key, b"exordos-boot-delegation-v1\0" + message, hashlib.sha256
    ).hexdigest()
