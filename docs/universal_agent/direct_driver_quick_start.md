<!--
Copyright 2026 Genesis Corporation.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
-->

# Direct driver Quick start

This page provides a quick start guide for capability drivers based on `DirectAgentDriver`.
Please refer to [Universal Agent](universal_agent.md) main terms for context.

## Driver interface

A direct driver is based on:

- [`DirectAgentDriver`](https://github.com/exordos/gcl_sdk/blob/master/gcl_sdk/agents/universal/drivers/direct.py)
- backend client (`AbstractBackendClient`)
- target fields storage (`AbstractTargetFieldsStorage`)

`DirectAgentDriver` does not use a meta file. It gets resources directly from the backend and stores only target fields for hash calculation and reconciliation.

## Quick start

Let's implement a direct driver that works with an HTTP REST API.
Objects behind this API are files.

- **Data plane**: external service with files
- **Capability kind**: `file_target`
- **Backend client**: HTTP client for file API
- **Target fields storage**: persists target fields by `(kind, uuid)`

### Step 1. Implement backend client

```python
import typing as tp

from gcl_sdk.clients.http import base as http_base
from gcl_sdk.agents.universal.clients.backend import base as client_base
from gcl_sdk.agents.universal.clients.backend import exceptions as client_exc
from gcl_sdk.agents.universal.dm import models


FILE_TARGET_KIND = "file_target"


class FilesRestBackendClient(client_base.AbstractBackendClient):
    def __init__(self, client: http_base.CollectionBaseClient):
        self._client = client

    def _to_view(self, item: dict[str, tp.Any]) -> dict[str, tp.Any]:
        return {
            "uuid": item["uuid"],
            "name": item["name"],
            "path": item["path"],
            "size": item.get("size"),
            "checksum": item.get("checksum"),
        }

    def get(self, resource: models.Resource) -> dict:
        try:
            item = self._client.get(f"/v1/files/{resource.uuid}")
        except Exception:
            raise client_exc.ResourceNotFound(resource=resource)
        return self._to_view(item)

    def list(self, capability: str) -> list[dict]:
        # Full listing of file objects from backend API.
        # API response example:
        # {
        #   "items": [
        #     {"uuid": "...", "name": "...", "path": "...", "size": 128}
        #   ]
        # }
        response = self._client.get("/v1/files/")
        return [self._to_view(item) for item in response.get("items", [])]

    def create(self, resource: models.Resource) -> dict:
        payload = {
            "uuid": str(resource.uuid),
            "name": resource.value["name"],
            "path": resource.value["path"],
        }

        try:
            item = self._client.post("/v1/files/", data=payload)
        except Exception:
            raise client_exc.ResourceAlreadyExists(resource=resource)
        return self._to_view(item)

    def update(self, resource: models.Resource) -> dict:
        payload = {
            "name": resource.value["name"],
            "path": resource.value["path"],
        }

        try:
            item = self._client.patch(f"/v1/files/{resource.uuid}", data=payload)
        except Exception:
            raise client_exc.ResourceNotFound(resource=resource)
        return self._to_view(item)

    def delete(self, resource: models.Resource) -> None:
        try:
            self._client.delete(f"/v1/files/{resource.uuid}")
        except Exception:
            raise client_exc.ResourceNotFound(resource=resource)
```

### Step 2. Implement driver based on DirectAgentDriver

```python
import os

import bazooka

from gcl_sdk.clients.http import base as http_base
from gcl_sdk.agents.universal.drivers import direct
from gcl_sdk.agents.universal.storage import fs


class FilesDirectCapabilityDriver(direct.DirectAgentDriver):
    def __init__(self, api_url: str, work_dir: str) -> None:
        http_client = bazooka.Client()
        rest_client = http_base.CollectionBaseClient(
            http_client=http_client,
            base_url=api_url,
            auth=None,
        )

        storage_path = f"{work_dir}/file_target_fields.json"
        storage = fs.TargetFieldsFileStorage(storage_path)
        client = FilesRestBackendClient(rest_client)
        super().__init__(client=client, storage=storage)

    def get_capabilities(self) -> list[str]:
        return ["file_target"]
```

The base class already implements `create/get/list/update/delete` and maps backend/storage exceptions to driver exceptions.

### Optioanal step. Transform bckend response

`DirectAgentDriver` supports `transformer_map` for per-kind transformations.

```python
from gcl_sdk.agents.universal.drivers import direct


transformer_map = {
    "file_target": direct.ResourceTransformer(
        ignore_null_attributes=True,
        attributes={"owner", "group"},
    )
}
```

Pass this map into `DirectAgentDriver` if backend payload needs normalization.

### Example target resource

```json
{
  "kind": "file_target",
  "value": {
    "uuid": "a1b2c3d4-e5f6-7890-a1b2-c3d4e5f67890",
    "name": "config.yaml",
    "path": "/opt/example/config.yaml"
  }
}
```

After `create`:

- file object is created via REST API
- target fields for this `(kind, uuid)` are saved in storage

## Target fields

The agent decides a resource has reached its target by comparing the hash
of the target resource with the hash of the resource gathered from the data
plane. Both are computed over the *target fields* only, so a field the
backend fills in by itself (`created_at`, a materialized default deep in
the value) cannot make a resource settle forever.

The direct driver derives the fields from the target resource: the key
skeleton of its value (`utils.value_shape`), which strips nested defaults
too, inside declared dicts and inside declared lists:

```python
# declared by the control plane
{"name": "web", "server": {"port": 80}}
# gathered by the backend, weight is a backend default
{"name": "web", "server": {"port": 80, "weight": 1}}
# both hash over -> {"name": "web", "server": {"port": 80}}
```

Target fields are declared on the model via
`get_resource_target_fields()`, which may return plain names (`"name"`)
or dot separated paths (`"setter.kind"`) that descend through dicts and
through every element of a list. A plain name selects the whole value
below it and wins over paths with the same head.

Whatever is used lands in the target fields storage at create/update time,
so changing the declaration does not rehash resources that already exist
until the next update. Legacy storage files written before this feature are
read as before and the hashes of such resources do not change.

## Register the driver

```toml
[project.entry-points.gcl_sdk_universal_agent]
FilesDirectCapabilityDriver = "your_package.drivers.files_direct:FilesDirectCapabilityDriver"
```

## Usage

```ini
[universal_agent]
caps_drivers = ...,FilesDirectCapabilityDriver
```

```bash
systemctl restart exordos-universal-agent
```
