# Capability driver Quick start

This page provides a quick start guide for capability drivers. Please refer to main terms of the [Universal Agent](universal_agent.md) to be in the right context.

## Driver interface

You can find the driver interface [here](https://github.com/exordos/gcl_sdk/blob/master/gcl_sdk/agents/universal/drivers/base.py) every method has a docstring with short description and they won't be duplicated here.

## Quick start

If you are reading this section, it means you would like to write your capability driver and that's great! As you already read a driver needs to work with data plane part and translate results to the Universal Agent via such abstraction as `Resource`. Let's imagine a simple problem we would like to solve for this quick start guide and write a driver for it. For instance, our driver should keep files in a particular directory. For simplicity we assume all files are empty, no nested directory and so on. We only need presence of these files in the particular directory. Let's start.

The full implementation can be found in [dummy.py](https://github.com/exordos/gcl_sdk/blob/master/gcl_sdk/agents/universal/drivers/dummy.py).

### Register capability

The first step is to specify capabilities or kinds the driver works with. The capabilities are defined as a list of strings. For example, if we want to work with files we can register the `file` capability.

```python
def get_capabilities(self) -> list[str]:
    """Returns a list of capabilities supported by the driver."""
    return ["file"]
```

### Work with data plane

The data plane is a particular directory in our case. Let's say it's stored in the `self.work_dir` variable. Don't worry about initialization we will cover it later.

The first method we need to implement is `create`. The signature is as follows:

```python
def create(self, resource: models.Resource) -> models.Resource:
    """Creates a resource."""
```

The agent sends `resource` to the driver to create *something*, in our case it's a `file`. The actual data is stored in the `resource.value` field. It's an ordinary `dict` and we can assume if somebody wants to create a file, he will send a file name. In other words the `resource.value` may look like this:

```json
{
    "uuid": "a1b2c3d4-e5f6-7890-a1b2-c3d4e5f67890",
    "name": "test.txt"
}
```

Where `uuid` is a mandatory field.

The implementation of `create` can look like this:

```python
def create(self, resource: models.Resource) -> models.Resource:
    """Creates a new file in the work directory."""
    name = f"{resource.value['uuid']}_{resource.value['name']}"
    path = os.path.join(self.work_dir, name)
    with open(path, "w") as f:
        f.write("")
    return resource
```

Create a file with the name `<uuid>_<name>` in the `work_dir`. `uuid` is mandatory to detect a resource the file belongs to.

The next step is to implement `get`:

```python
def get(self, resource: models.Resource) -> models.Resource:
    """Find and return the file resource."""
    for f in os.listdir(self.work_dir):
        uuid, name = f.split("_", 1)
        if uuid == str(resource.uuid):
            value = {"uuid": uuid, "name": name}
            new_resource = models.Resource.from_value(value, resource.kind)
            return new_resource

    raise driver_exc.ResourceNotFound(resource=resource)
```

The first step is to find the file with the corresponding `uuid`. Then we need to create a new `Resource` from raw data(`value`). There are a couple of useful methods in the `Resource` class. The `from_value` static method will create a new resource from raw data. The resource is ready to be returned. If the resource is not found, raise `ResourceNotFound` exception.

`list` all available files in the `work_dir`:

```python
def list(self, capability: str) -> list[models.Resource]:
    """Lists all files"""
    resources = []

    for f in os.listdir(self.work_dir):
        uuid, name = f.split("_", 1)
        value = {"uuid": uuid, "name": name}
        resource = models.Resource.from_value(value, capability)
        resources.append(resource)

    return resources
```

As described above, the first step is collecting raw data, then create a resource and append it to the list.

The next step is to implement `delete`:

```python
def delete(self, resource: models.Resource) -> None:
    """Delete the file in the work directory."""
    try:
        res = self.get(resource)
    except driver_exc.ResourceNotFound:
        # Nothing to do, the resource does not exist
        return
    
    name = f"{res.value['uuid']}_{res.value['name']}"
    path = os.path.join(self.work_dir, name)
    os.remove(path)
```

Use `get` method to find the file and delete it. If the file is not found, then nothing to do.

Now we have a working driver for the `file` capability.

### Register the driver

When the implementation is ready, you should register the driver in [entry points](https://setuptools.pypa.io/en/latest/userguide/entry_point.html). For example, if you use `pyproject.toml` in your project you can do it like this:

```toml
[project.entry-points.gcl_sdk_universal_agent]
RenderAgentDriver = "gcl_sdk.agents.universal.drivers.dummy:DummyFilesDriver"
```

Specify your driver class name in the `gcl_sdk_universal_agent` section.

## Usage

The driver and package are ready, it's time to use it in the universal agent. Firstly install your package with the driver in the machine where you run the agent.

```bash
pip install your-package-with-driver
# or with uv
uv pip install your-package-with-driver
```

Then add the driver to the configuration file `/etc/exordos_universal_agent/exordos_universal_agent.conf`:

```ini
[universal_agent]
caps_drivers = ...,YouDriverClassName
```

Restart the agent:

```bash
systemctl restart exordos-universal-agent
```

Now the agent will use your driver to work with files!
