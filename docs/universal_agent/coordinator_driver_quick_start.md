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

# Coordinator driver Quick start

This page provides a quick start guide for coordinator drivers based on `MetaCoordinatorAgentDriver`.
Please refer to [Metadata driver quick start](metadata_driver_quick_start.md) first — a
coordinator driver is a `MetaFileStorageAgentDriver` variant, so the meta-file storage
model still applies.

## When to use a coordinator driver

A plain metadata driver (`MetaFileStorageAgentDriver`) treats every resource kind as
independent. Use a coordinator driver instead when several capability kinds describe
related objects that must be reconciled together, and one kind needs the already-loaded
objects of another kind to do its job. For example, a VM pool driver manages three
related kinds:

- `pool` — the hypervisor/pool itself (connection details, capacity)
- `pool_volume` — a disk that belongs to a `pool` and, optionally, to a machine
- `pool_machine` — a VM that belongs to a `pool` and owns a set of `pool_volume`s

Creating a `pool_volume` requires the `pool` object (to check storage capacity);
creating a `pool_machine` requires both the `pool` (to check available cores/RAM) and
its `pool_volume`s (to build the disk list). `MetaCoordinatorAgentDriver` resolves and
injects these dependencies automatically.

## Driver interface

Coordinator drivers are built on top of:

- [`MetaCoordinatorAgentDriver`](https://github.com/exordos/gcl_sdk/blob/master/gcl_sdk/agents/universal/drivers/meta.py)
- [`MetaCoordinatorDataPlaneModel`](https://github.com/exordos/gcl_sdk/blob/master/gcl_sdk/agents/universal/drivers/meta.py)

Both subclass the plain `MetaFileStorageAgentDriver`/`MetaDataPlaneModel` pair described in
the [metadata driver quick start](metadata_driver_quick_start.md), so `create`/`get`/`list`/
`update`/`delete` still follow the same meta-file-backed workflow. The only addition is
`__coordinator_map__`.

## The coordinator map

`__coordinator_map__` describes, per capability kind, which other kinds must be loaded
first and how to match them to the current object:

```python
__coordinator_map__ = {
    "pool": {},
    "pool_volume": {
        "pool": {
            "kind": "pool",
            "relation": "pool_volume:pool",
        },
    },
    "pool_machine": {
        "pool": {
            "kind": "pool",
            "relation": "pool_machine:pool",
        },
        "volumes": {
            "kind": "pool_volume",
            "relation": "pool_volume:machine",
        },
    },
}
```

- The top-level keys are capability kinds handled by the driver (must match `__model_map__`).
- Each value is a dict of dependency name -> spec. The dependency name becomes a keyword
  argument passed to the model's `dump_to_dp`/`restore_from_dp`/`update_on_dp`/`delete_from_dp`.
- `kind` is the dependency's capability kind — its already-loaded objects are looked up
  from the driver's internal coordinator storage.
- `relation` is `"<kind_with_the_pointer_field>:<field_name>"`. If `relation_kind` matches
  the *current* object's own kind (as for `pool_volume`'s `pool` dependency), the field is
  read off the current object itself and a single matching object is returned. Otherwise
  (as for `pool_machine`'s `volumes` dependency) every object of `kind` whose `field_name`
  points back at the current object's UUID is collected into a tuple.
- Omitting `relation` copies every object of `kind` as-is (rarely needed).
- A kind with `{}` (like `pool`) has no dependencies — its data plane methods are called
  with no extra keyword arguments.

`__model_map__` order matters: kinds are processed in the order they're declared, so a
kind's dependencies should be declared before it (`pool` before `pool_volume` before
`pool_machine`).

## Data plane model methods receive dependencies as kwargs

```python
class MetaVolume(meta.MetaCoordinatorDataPlaneModel):
    pool = properties.property(types.UUID(), required=True)
    ...

    def dump_to_dp(self, pool: MetaPool) -> None:
        driver = pool.load_driver()
        ...

    def restore_from_dp(self, pool: MetaPool | None) -> None:
        ...

    def delete_from_dp(self, pool: MetaPool) -> None:
        ...

    def update_on_dp(self, pool: MetaPool) -> None:
        ...
```

```python
class MetaMachine(meta.MetaCoordinatorDataPlaneModel):
    pool = properties.property(types.AllowNone(types.UUID()))
    ...

    def dump_to_dp(self, pool: MetaPool, volumes: tp.Collection[MetaVolume]) -> None:
        ...
```

The keyword argument names (`pool`, `volumes`) must match the dependency names used in
`__coordinator_map__`.

## The driver

```python
class PoolAgentDriver(meta.MetaCoordinatorAgentDriver):
    # Order matters: dependencies must come before dependents.
    __model_map__ = {
        "pool": MetaPool,
        "pool_volume": MetaVolume,
        "pool_machine": MetaMachine,
    }

    __coordinator_map__ = {
        "pool": {},
        "pool_volume": {
            "pool": {"kind": "pool", "relation": "pool_volume:pool"},
        },
        "pool_machine": {
            "pool": {"kind": "pool", "relation": "pool_machine:pool"},
            "volumes": {"kind": "pool_volume", "relation": "pool_volume:machine"},
        },
    }
```

## Real-world example: the VM pool/libvirt driver

The full, real implementation of the pattern described above lives in
[`gcl_sdk/agents/universal/drivers/pool.py`](https://github.com/exordos/gcl_sdk/blob/master/gcl_sdk/agents/universal/drivers/pool.py)
(`MetaPool`, `MetaVolume`, `MetaMachine`, `PoolAgentDriver`, `LocalPoolAgentDriver`) and
[`gcl_sdk/agents/universal/drivers/libvirt.py`](https://github.com/exordos/gcl_sdk/blob/master/gcl_sdk/agents/universal/drivers/libvirt.py)
(`LibvirtPoolDriver`, the concrete data plane implementation that talks to libvirt to
create/resize/attach VM disks and create/update/delete VM domains).

`pool.py` also defines `AbstractPoolDriver`, a second, narrower abstraction used only
*inside* the coordinator models to talk to the actual hypervisor backend. It is loaded
dynamically by `MetaPool.load_driver()` from the `gcl_sdk_machine_pool_driver` entry-point
group, keyed by the `driver_spec.KIND` of the pool (for example `"libvirt"`, `"dummy"`, or
`"exordos_local_hyper"`). This extra indirection exists specifically for this driver so
that alternative hypervisor backends can be added without touching
`MetaPool`/`MetaVolume`/`MetaMachine` — it is not part of the generic coordinator driver
contract and most coordinator drivers won't need it.

`"exordos_local_hyper"` registers the very same `LibvirtPoolDriver` class as `"libvirt"` —
it exists as a separate KIND purely at the control-plane level, so `KindModelSelectorType`
can validate its driver_spec against `ExordosLocalHyperDriverSpec` (which adds a required
`node` field) instead of the plain `LibvirtPoolDriverSpec`. `node` is what lets the
scheduler pin such a pool to one specific agent — see `LocalPoolAgentDriver` below.

The `libvirt` backend requires the `libvirt` python bindings, shipped as the optional
`gcl_sdk[libvirt]` extra so that hosts not running VMs don't need to install them.

### Marker capabilities: pinning a pool to its own agent

`LocalPoolAgentDriver` (a thin `PoolAgentDriver` subclass) advertises one extra capability,
`local_pool`, purely so a scheduler consuming this agent's reported capabilities can tell
it apart from one that can only serve pools already reachable over the network — matching
a pool needing `local_pool` to the one agent whose node uuid equals the pool's
`driver_spec.node` is entirely up to that scheduler; gcl_sdk itself only carries the
capability. `local_pool` isn't a real, actualizable resource kind: nothing ever targets it,
so `list()` must return `[]` for it explicitly rather than falling through to the
coordinator's normal meta-file-backed lookup (which would raise, since `local_pool` was
never added to `__model_map__`):

```python
class LocalPoolAgentDriver(PoolAgentDriver):
    def get_capabilities(self) -> list[str]:
        return super().get_capabilities() + ["local_pool"]

    def list(self, capability: str) -> list[ua_models.Resource]:
        if capability == "local_pool":
            return []
        return super().list(capability)
```

Use the same pattern for any other scheduling-only marker capability: advertise it in
`get_capabilities()`, but short-circuit `list()` (and `create`/`update`/`delete`, if the
generic actualization loop could ever call them) before they reach the coordinator
machinery meant for real resource kinds.

## Register the driver

```toml
[project.entry-points.gcl_sdk_universal_agent]
YourCoordinatorDriver = "your_package.drivers.your_driver:YourCoordinatorDriver"
```

## Usage

```ini
[universal_agent]
caps_drivers = ...,YourCoordinatorDriver
```

```bash
systemctl restart exordos-universal-agent
```
