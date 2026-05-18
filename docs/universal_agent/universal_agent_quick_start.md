# Universal Agent Quick start

This page provides a quick start guide for the Universal Agent. How to run, configure and work with it.

## Install & run

The agent is embedded into [Exordos base image](https://github.com/exordos/eci_base) and the simplest way to test it just to run a VM from the base image or its inherited images.

For manual installation prepare a virtual environment and install the [gcl-sdk](https://github.com/exordos/gcl_sdk).

```bash
python3 -m venv venv
source venv/bin/activate
pip install gcl-sdk
```

Run the agent:

```bash
exordos-universal-agent --config-file /etc/exordos_universal_agent/exordos_universal_agent.conf
```

The configuration file will be described later.

## Configuration

The agent uses a configuration file in the `ini` format. The default path to the configuration file is `/etc/exordos_universal_agent/exordos_universal_agent.conf`. The main agent section is `universal_agent`:

```ini
[universal_agent]
orch_endpoint = http://localhost:11011
status_endpoint = http://localhost:11012
caps_drivers = CoreCapabilityDriver,PasswordCapabilityDriver
facts_drivers = CoreFactDriver
```

- `orch_endpoint`, `status_endpoint` are endpoints to orchestrator services.
- `caps_drivers`- the list of capability drivers.
- `facts_drivers`- the list of fact drivers.

For the universal scheduler:

```ini
[universal_agent_scheduler]
capabilities = em_core_*,password
```

- `capabilities` - the list of capabilities to schedule. You can use wildcards like `em_core_*` to schedule all capabilities starting with `em_core_`.

If any of drivers use a database it should be configured in the `[db]` section. Example:

```ini
[db]
connection_url = postgresql://exordos_core:exordos_core@127.0.0.1:5432/exordos_core
connection_pool_size = 2
```

Also you may specify specific configuration for each driver, for example, for the `CoreCapabilityDriver`:

```ini
[CoreCapabilityDriver]
username = test
password = test
user_api_base_url = http://localhost:11010
project_id = 12345678-aaaa-bbbb-cccc-f691897b8145
em_core_compute_nodes = /v1/nodes/
em_core_config_configs = /v1/config/configs/
```
