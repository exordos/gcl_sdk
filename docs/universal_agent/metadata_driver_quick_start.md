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

# Metadata driver Quick start

This page provides a quick start guide for metadata drivers based on `MetaFileStorageAgentDriver`.
Please refer to [Universal Agent](universal_agent.md) main terms for context.

## Driver interface

Metadata drivers are built on top of:

- [`MetaFileStorageAgentDriver`](https://github.com/exordos/gcl_sdk/blob/master/gcl_sdk/agents/universal/drivers/meta.py)
- [`MetaDataPlaneModel`](https://github.com/exordos/gcl_sdk/blob/master/gcl_sdk/agents/universal/drivers/meta.py)

A metadata driver stores resource metadata (for example UUID and file path) in a meta file and keeps the actual object in the data plane.

## Quick start

Let's implement a simple metadata driver that manages files in a directory.

- **Data plane**: files in `self.work_dir`
- **Meta file**: JSON file with resource UUIDs and metadata
- **Capability kind**: `file_target`

The implementation idea is:

1. Keep the file on disk.
2. Keep resource UUIDs and metadata in the meta file.
3. Restore full resource state using metadata + data plane.

### Step 1. Define a data plane model

`MetaDataPlaneModel` is responsible for operations in the data plane.

```python
import os

from restalchemy.dm import properties
from restalchemy.dm import types

from gcl_sdk.agents.universal.drivers import exceptions as driver_exc
from gcl_sdk.agents.universal.drivers import meta


FILE_TARGET_KIND = "file_target"


class FileMetaModel(meta.MetaDataPlaneModel):
    name = properties.property(types.String(min_length=1, max_length=255))
    path = properties.property(types.String(min_length=1, max_length=4096))

    def get_meta_model_fields(self) -> set[str] | None:
        # Save only fields required to locate and identify the file.
        return {"uuid", "name", "path"}

    def dump_to_dp(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if os.path.exists(self.path):
            resource = self.to_ua_resource(FILE_TARGET_KIND)
            raise driver_exc.ResourceAlreadyExists(resource=resource)

        with open(self.path, "w") as f:
            f.write("")

    def restore_from_dp(self) -> None:
        if not os.path.exists(self.path):
            resource = self.to_ua_resource(FILE_TARGET_KIND)
            raise driver_exc.ResourceNotFound(resource=resource)

    def delete_from_dp(self) -> None:
        if os.path.exists(self.path):
            os.remove(self.path)

    def update_on_dp(self) -> None:
        # Simplest strategy for this example.
        self.delete_from_dp()
        self.dump_to_dp()
```

### Step 2. Define the metadata driver

The driver maps capability kinds to metadata models and points to a meta file.

```python
import os

from gcl_sdk.agents.universal.drivers import meta


class FilesMetaCapabilityDriver(meta.MetaFileStorageAgentDriver):
    FILE_META_PATH = "/var/lib/exordos/universal_agent/file_meta.json"

    __model_map__ = {
        "file_target": FileMetaModel,
    }

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, meta_file=self.FILE_META_PATH, **kwargs)
```

`MetaFileStorageAgentDriver` already implements `create/get/list/update/delete` workflow.
It uses:

- model methods (`dump_to_dp`, `restore_from_dp`, `delete_from_dp`, `update_on_dp`)
- meta file storage to persist model metadata

### Step 3. Example target resource

Example resource sent to the agent:

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

- `/opt/example/config.yaml` exists in the data plane
- meta file contains resource UUID and metadata for future reconciliation

## Register the driver

When implementation is ready, register the driver via entry points:

```toml
[project.entry-points.gcl_sdk_universal_agent]
FilesMetaCapabilityDriver = "your_package.drivers.files_meta:FilesMetaCapabilityDriver"
```

## Usage

1. Install package with your driver.
2. Add the driver class to the Universal Agent config.
3. Restart the agent.

```ini
[universal_agent]
caps_drivers = ...,FilesMetaCapabilityDriver
```

```bash
systemctl restart exordos-universal-agent
```
