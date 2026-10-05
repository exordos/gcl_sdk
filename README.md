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

## Rawstor storage clusters

`StorageClusterAgentDriver` runs on core and manages a separate
`rawstor-mds@<cluster-uuid>.service`, persistent SQLite index and topology per
cluster. Its driver spec carries the MDS `endpoint` and UUID-keyed `nodes`
and `pools` supplied by the control plane. OST topology paths run from outermost
to innermost (`dc/row/rack/server`). Topology edits reload the MDS; removing a
referenced OST is rejected. Unregistering a cluster retains its database.

Pool policies share physical OST capacity. The driver reports an OST inventory
and completed MDS objects; `storage_capacity.available_by_policy` derives a
placement upper bound accounting for mirrors, failure domains and pending disks.
Pool figures are not additive. Each OST needs a dedicated backing filesystem.
Unreachable OSTs contribute zero free space. MDS remains the final placement
authority.

`MachineVolume.storage_policy` preserves the assigned pool UUID, mirrors,
chunk size and failure domain across reconciliation. `ExordosLocalHyperDriver`
passes these to `rawstor.Target.create()` directly. This requires bindings from
[run 37239522275](https://github.com/rawstor/librawstor/actions/runs/37239522275),
version `99.0.0+0.4e3d1f3`, or a compatible newer build. Rawstor disks do not
consume the hypervisor's local qcow2 budget. Implicit qcow2 pool attributes and
new disk requests default to HOT ephemeral; explicit pool attributes override them.

Set `RAWSTOR_TEST_MDS` and `RAWSTOR_TEST_OST` to daemon paths to run the native
integration in `test_rawstor_cluster.py`. It checks two MDS instances, two OSTs
per cluster, rack replication, topology reload, removal protection and recovery.

# 🔗 Related projects

- Exordos Core is the main project of the Exordos ecosystem. You can find it [here](https://github.com/exordos/exordos_core).
- Exordos CLI is the official command-line interface for the Exordos Core platform. You can find it [here](https://github.com/exordos/exordos).

# 💡 Contributing

Contributing to the project is highly appreciated! However, some rules should be followed for successful inclusion of new changes in the project:

- All changes should be done in a separate branch.
- Changes should include not only new functionality or bug fixes, but also tests for the new code.
- After the changes are completed and **tested**, a Pull Request should be created with a clear description of the new functionality. And add one of the project maintainers as a reviewer.
- Changes can be merged only after receiving an approve from one of the project maintainers.
