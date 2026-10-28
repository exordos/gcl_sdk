[![Tests](https://img.shields.io/github/actions/workflow/status/exordos/gcl_sdk/tests.yaml?branch=master&label=tests&logo=github&style=flat-square)](https://github.com/exordos/gcl_sdk/actions/workflows/tests.yaml)
[![Publish](https://img.shields.io/github/actions/workflow/status/exordos/gcl_sdk/publish-to-pypi.yml?branch=master&label=publish&logo=github&style=flat-square)](https://github.com/exordos/gcl_sdk/actions/workflows/publish-to-pypi.yml)
[![PyPI](https://img.shields.io/pypi/v/gcl-sdk?style=flat-square)](https://pypi.org/project/gcl-sdk/)
[![Python](https://img.shields.io/pypi/pyversions/gcl-sdk?style=flat-square)](https://www.python.org/)
[![Downloads](https://img.shields.io/pypi/dm/gcl-sdk?style=flat-square)](https://pypi.org/project/gcl-sdk/)
[![uv](https://img.shields.io/badge/uv-managed-DE5FE9?logo=uv&logoColor=white&style=flat-square)](https://github.com/astral-sh/uv)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-D7FF64?logo=ruff&logoColor=black&style=flat-square)](https://github.com/astral-sh/ruff)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue?style=flat-square)](https://www.apache.org/licenses/LICENSE-2.0)

# Exordos SDK

**📚 Documentation:** [exordos.github.io/gcl_sdk](https://exordos.github.io/gcl_sdk/)

Exordos SDK is a set of tools and libraries for developing Exordos elements. It provides the building blocks needed to integrate your own services and capabilities with the [Exordos Core platform](https://github.com/exordos/exordos_core) — from event handling and auditing to universal agents, builders, and schedulers.

## What Exordos SDK does

Exordos SDK hides the complexity of interacting with the platform and lets element developers focus on their domain logic.

Key components:

- **Universal Agent** — a ready-to-use agent runtime with a pluggable capability driver model for managing services, load balancers, SSH keys, secrets, machine pools, and more on any node.
- **Universal Builder** — a framework for describing and building infrastructure and application topologies in a declarative way.
- **Universal Scheduler** — a scheduling component for orchestrating element lifecycle operations.
- **Events** — a unified API for publishing and consuming Exordos events with pluggable payload models.
- **Audit** — built-in support for recording and exporting audit trails.

> **For a full overview of components, quick start guides, and advanced usage, visit the [documentation](https://exordos.github.io/gcl_sdk/).**

# 🔗 Related projects

- Exordos Core is the main project of the Exordos ecosystem. You can find it [here](https://github.com/exordos/exordos_core).
- Exordos CLI is the official command-line interface for the Exordos Core platform. You can find it [here](https://github.com/exordos/exordos).

# 💡 Contributing

Contributing to the project is highly appreciated! However, some rules should be followed for successful inclusion of new changes in the project:

- All changes should be done in a separate branch.
- Changes should include not only new functionality or bug fixes, but also tests for the new code.
- After the changes are completed and **tested**, a Pull Request should be created with a clear description of the new functionality. And add one of the project maintainers as a reviewer.
- Changes can be merged only after receiving an approve from one of the project maintainers.


## Authenticated Notification events

The HTTP event client obtains and refreshes IAM tokens using the existing
`CoreIamAuthenticator`. Configure a private JSON file readable only by the
service account, for example `/etc/service/notification-auth.json`:

```json
{"base_url":"https://iam.example.com/api/core", "username":"event-sender",
 "password":"[REDACTED]", "scope":""}
```

```ini
[events]
enabled = true
client_type = async
event_type_mapping_filepath = event_type_mapping.yaml
exordos_notification_endpoint = https://notification.example.com
notification_auth_file = /etc/service/notification-auth.json
project_id = <EVENT_PROJECT_UUID>
```

`AsyncEventClient` keeps the existing durable local queue. `EventSenderService`
uses the authenticated HTTP client for submission and status polling. Access and
refresh tokens are held in memory and renewed after an unauthorized response.
The same HTTP session handles login, refresh and event delivery. It refuses
redirects and redacts request/response bodies, including confirmation links and
credentials. A missing IAM file/token fails closed; no placeholder token is
provided. A separately supplied valid `exordos_api_token` remains supported, but
cannot refresh without IAM credentials.

Grant only the permissions required by the destination Notification service.
For Core confirmation email, these are `event_type.read` and
`event_type_<CONFIRMATION_EVENT_TYPE_UUID>.create/read` under the
`exordos_notification` service. Push senders keep source-specific permissions.
Provision credentials and deploy this sender before requiring authentication
on an existing Notification event endpoint. Keep the current event UUID,
EventType mapping and v1 payload; no API version change is required.
