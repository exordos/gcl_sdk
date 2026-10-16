# Secrets

`gcl_sdk.secret` provides the control plane secret models shared between
Exordos Core and the services that install on top of it: passwords,
certificates, RSA keys and SSH keys. The models are the control plane
counterparts of the universal agent drivers: a secret is spread over the
target nodes as universal agent resources and the agent places them on the
data plane.

## Models

Model | Resource kind | Purpose
--- | --- | ---
`Secret` | — | Base model: status, constructor, name/description/project
`Password` | `password` | Auto-generated or manual password
`Certificate` | `certificate` | ACME certificate with domains and expiration
`RSAKey` | `rsa_key` | RSA key pair generated from `bitness`
`SSHKey` | `ssh_key` | Public key placed into a user's `authorized_keys`
`SSHHostKey` | `ssh_key_target` | Per-node projection of an `SSHKey`

`SecretStatus` and `SecretMethod` live in `gcl_sdk.secret.constants` together
with the resource kind names.

The models are intentionally storage and quota agnostic, the same way as
the infra models are: none of them carries an ORM mixin. A consumer that
keeps secrets in SQL adds `orm.SQLStorableMixin` (plus
`ua_models.TargetResourceSQLStorableMixin` for the universal agent polling
helpers) on its own subclasses; the `__tablename__` of each model is the
canonical table name the consumer storage is expected to use. Quota
accounting is a Core concern and is added by the consumer through its own
mixin subclasses.

## Targets

An `SSHKey` is bound to a target: either a single node or a whole node set
(`gcl_sdk.infra.dm.models.NodeTarget` / `NodeSetTarget`). The SDK cannot
resolve the members of a node set on its own — it does not own the node
inventory — so `NodeSetTarget._fetch_nodes()` raises `NotImplementedError`
and the consumer that keeps the inventory overrides it.

```python
import uuid

from gcl_sdk.infra.dm import models as infra_models
from gcl_sdk.secret.dm import models as secret_models

key = secret_models.SSHKey(
    name="root-access",
    project_id=uuid.uuid4(),
    target=infra_models.NodeTarget.from_node(uuid.uuid4()),
    user="root",
    target_public_key="ssh-ed25519 AAAA... comment",
)
```

## From a secret to the agent resources

`SSHKey.to_host_resource()` builds the `ssh_key_target` resource for one
node: the resource uuid is derived from the secret and the node uuid, the
secret is linked as the master, and the agent uuid points at the node. The
universal agent picks the resource up with the built-in
`SSHKeyCapabilityDriver` (see the `ssh_key` agent driver), which appends the
public key to `/home/<user>/.ssh/authorized_keys`.

```python
resource = key.to_host_resource(master=key.uuid, node=node_uuid)
```

The same pattern applies to the other kinds: a consumer builds
`TargetResource` objects from the models (`to_ua_resource`) and the universal
agent reconciles them on the data plane.
