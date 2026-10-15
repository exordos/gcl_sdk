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
