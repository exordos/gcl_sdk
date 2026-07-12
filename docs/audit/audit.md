# Audit

`gcl_sdk.audit` provides a transactional local audit outbox, a project-aware
read API, and a background delivery service for a central Audit installation.

## Recording changes

Replace `orm.SQLStorableMixin` with `AuditLogSQLStorableMixin` and set the
resource type and service name:

```python
from gcl_sdk.audit.dm import models as audit_models


class Node(audit_models.AuditLogSQLStorableMixin, ...):
    __audit_service_name__ = "compute"
    __audit_resource_type__ = "node"
```

`AuditEventBase` defines the immutable event contract shared with central Audit
storage. `insert`, `update`, and `delete` write an `AuditDeliveryEvent`, which
adds local outbox persistence and delivery status, in the same database
transaction as the resource mutation. New rows start with delivery status
`NEW`. A successful or idempotently acknowledged central ingest removes the
local outbox row. A permanent central UUID/payload conflict is retained and
marked `ERROR`.

## Local API

Attach `AuditRoute` to the service route tree. Reads require
`audit.events.read` or `audit.events.read_all` and are project-scoped by the IAM
context.

```python
from gcl_sdk.audit.api import routes as audit_routes


class ApiEndpointRoute(routes.Route):
    audit = routes.route(audit_routes.AuditRoute)
```

Applications must apply SDK migrations before using the model. Migration
`0007` creates `gcl_sdk_audit_events` together with the delivery state and the
`(status, created_at, uuid)` worker index.

## Delivery daemon

Register the options and add `AuditSenderService` to the host service loop only
when delivery is enabled:

```python
from gcl_sdk.audit import opts as audit_opts
from gcl_sdk.audit.services import senders

audit_opts.register_audit_delivery_opts(CONF)

if audit_opts.get_audit_delivery_config().enabled:
    audit_sender = senders.AuditSenderService.build_from_config()
```

Configuration:

```ini
[audit_delivery]
enabled = true
endpoint = http://audit.local.genesis-core.tech:8080/
api_version = v1
auth_token = <token with audit.events.create>
timeout = 5
batch_size = 100
```

The worker sends events strictly from oldest to newest. A transient HTTP or
network error leaves the earliest row in `NEW` and stops the iteration, so later
events cannot overtake it. An `ERROR` row blocks the source outbox until an
operator resolves it. Central ingest is idempotent by UUID, therefore a retry
after an uncertain response does not create a duplicate.

The bearer token is currently configured manually. Automatic scoped service
token issuance and rotation is tracked in
[`exordos_core#470`](https://github.com/exordos/exordos_core/issues/470).
Never write the token to logs or test reports.
