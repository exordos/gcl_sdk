# Universal Agent

The Universal Agent is a compound term that refers to a collection of services, tools, models. The main components of the Universal Agent are:

- Universal Agent service.
- Universal scheduler service.
- Common high level models such as `Resource`, `TargetResource`, `Payload` and others.
- Logic of hashes calculation using fast hash algorithms `xxh3_64`.
- Interfaces for Capability and Fact drivers.
- A couple of clients.
- Other helpful tools.

## Quick start

Follow the [quick start guide](universal_agent_quick_start.md) to run the Universal Agent.

## Main terms

**Universal agent service** is a unified agent that implements common logic of abstract `resource` and `fact` management. The agent operates on high level abstractions such as `Resource` and `TargetResource` but actual interaction with data plane is performed via drivers. These drivers are loaded at launch time and registered as agent capabilities or facts. After registration the agent can handle resources with specified capabilities.

**Universal scheduler service** is a service that provides some common logic to schedule resources to particular agents based on the agent capabilities. The simplest logic is implemented for now. First agent that can handle a resource by its capability(kind) is selected and the resource is scheduled to this agent.

**Resource** this model represents an abstract resource for the Universal Agent. The model is mostly used as an **actual** resource. It will be explained later in detail.

**TargetResource** almost the same as `Resource` but it's used as a target resource. It will be explained later in detail.

**Payload** this model is used to represent the payload of the agent. The payload is a collection of resources, facts and some additional information. It will be explained later in detail.

### Resource

As described it's a model that represents an abstract resource and the model is mostly used as an actual resource, for instance, gathered from the data plane. In this case the `value` dict contains a real object gathered from the data plane in dict format. The essential fields are:

- *kind* - resource kind, for instance, "config", "secret" and so on.
- *value* - resource value in dict format.
- *hash* - hash value only for the target fields.
- *full_hash* - hash value for the whole value (all fields).
- *status* - resource status, for instance, "ACTIVE", "NEW" and others.

Some explanation for the `hash` and `full_hash`. Let's assume we have the following target node resource:

```json
    {
        "uuid": "a1b2c3d4-e5f6-7890-a1b2-c3d4e5f67890",
        "name": "vm",
        "project_id": "12345678-c625-4fee-81d5-f691897b8142",
        "cores": 1,
        "ram": 1024,
        "disk_spec": {
            "kind": "root_disk",
            "root_disk_size": 15,
            "image": "http://10.20.0.1:8080/exordos-base.raw",
        }
    }
```

All these fields are considered as target fields and they are used to calculate `hash`. After node creation we have the the following:

```json
    {
        "uuid": "a1b2c3d4-e5f6-7890-a1b2-c3d4e5f67890",
        "name": "vm",
        "project_id": "12345678-c625-4fee-81d5-f691897b8142",
        "cores": 1,
        "ram": 1024,
        "disk_spec": {
            "kind": "root_disk",
            "root_disk_size": 15,
            "image": "http://10.20.0.1:8080/exordos-base.raw",
        },

        // Not target fields below
        "created_at": "2022-01-01T00:00:00+00:00",
        "updated_at": "2022-01-01T00:00:00+00:00",
        "default_network": {}
    }
```

For hash calculation only the target fields are used as discussed above. `full_hash` is calculated for all fields.

### TargetResource

Almost the same as `Resource` but it's used as a target resource. Additional fields are:

- *agent* - agent uuid that will handle this resource.
- *master* - reference to the master resource if it exists.
- *master_hash* - hash of the master resource.
- *master_full_hash* - full hash of the master resource.
- *tracked_at* - timestamp when the resource was tracked. It's useful to track actuality of the resource.

### Payload

This model is used to represent the payload of the agent. The model is used as for control plane and data plane as well. The control plane payload is received from Orch API and it has to be applied to the data plane, except for `facts`. A data plane payload is a collected payload from the data. If CP and DP payloads are different from target values of resources that means we need to update something on the data plane. If CP and DP payloads are different from facts point of view, it means we need to update something in the Status API to save new facts.

**capabilities** - a set of managed resources, for example, configuration,
    secrets and so on. An orchestrator sets which resources should be
    presented on the data plane. CP resources from the capabilities
    contains only managed fields. When these resources are gathered
    from the data plane they may have some additional fields,
    for instance, created time, updated time and so on. Only the
    managed fields are orchestrated.

**facts** - opposite to the capabilities. These resources are gathered from
    the data plane independently from the orchestrator. In other words,
    they are not managed by the orchestrator. A simple example of facts
    are network interfaces.

**hash** - a hash of the payload. The formula is described below:

```python
    hash(
        cap_resource0.hash,
        cap_resource1.hash,
        ...
        fact_resource0.full_hash,
        fact_resource1.full_hash,
        ...
    )
```

## Capability and fact drivers

One of ideas of the Universal Agent is to use drivers to interact with the data plane but the agent itself is operated only on high level abstractions. So developers should write only drivers using specified interface. The drivers are loaded at agent launch time and registered their capabilities and facts. After registration the agent can handle resources with specified capabilities.

[Capability driver interface](https://github.com/exordos/gcl_sdk/blob/master/gcl_sdk/agents/universal/drivers/base.py#L23)

[Fact driver interface](https://github.com/exordos/gcl_sdk/blob/master/gcl_sdk/agents/universal/drivers/base.py#L62)

Look at [quick start guide](capability_driver_quick_start.md) for capability driver and [quick start guide](fact_driver_quick_start.md) for fact driver.

## Drivers

### Metadata driver

Metadata driver handles models that are partly stored in a `meta file`. This approach is useful when the data plane cannot provide all fields required to manage resources.

See [Metadata driver quick start](metadata_driver_quick_start.md) for details, examples, and implementation guidance.

#### SSHKeyCapabilityDriver

The driver handles SSH keys on the host. The `SSHKeyCapabilityDriver` is derived from `MetaFileStorageAgentDriver`. The main data plane model is `SSHKey`, it's derived from `MetaDataPlaneModel`. The meta file is located at `/var/lib/exordos/universal_agent/ssh_key_meta.json`.

### Direct driver

Direct driver gets resources directly from backend systems without a meta file. It keeps target fields in dedicated storage to preserve stable hash calculation.

See [Direct driver quick start](direct_driver_quick_start.md) for details, examples, and implementation guidance.

## Clients
