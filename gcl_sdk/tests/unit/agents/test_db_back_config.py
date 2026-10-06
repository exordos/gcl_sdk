#    Copyright 2026 Genesis Corporation.
#    Licensed under the Apache License, Version 2.0 (the "License")

from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from oslo_config import cfg
import pytest

from gcl_sdk.agents.universal.cmd import universal_agent_db_back as cmd


@pytest.mark.parametrize("period", [None, 0.0, 0.5, 10.0])
def test_main_uses_configured_iteration_period(
    tmp_path: Path, period: float | None
) -> None:
    conf = cfg.ConfigOpts()
    conf.register_cli_opts(cmd.core_agent_opts, cmd.DOMAIN)
    config_path = tmp_path / "agent.conf"
    config_path.write_text(
        "[agent]\n" + (f"iter_min_period = {period}\n" if period is not None else "")
    )
    conf(["--config-file", str(config_path)])

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(cmd, "CONF", conf))
        stack.enter_context(mock.patch.object(cmd.config, "parse"))
        stack.enter_context(mock.patch.object(cmd.infra_log, "configure"))
        stack.enter_context(
            mock.patch.object(
                cmd.engines.engine_factory, "configure_postgresql_factory"
            )
        )
        stack.enter_context(
            mock.patch.object(cmd.ua_utils, "cfg_load_section_map", return_value={})
        )
        stack.enter_context(mock.patch.object(cmd.ua_utils, "system_uuid"))
        stack.enter_context(
            mock.patch.object(cmd.ua_core_drivers, "DatabaseCapabilityDriver")
        )
        stack.enter_context(mock.patch.object(cmd.orch_db, "DatabaseOrchClient"))
        service = stack.enter_context(
            mock.patch.object(cmd.agent, "UniversalAgentService")
        )
        cmd.main()

    expected = 3.0 if period is None else period
    assert service.call_args.kwargs["iter_min_period"] == expected
    service.return_value.start.assert_called_once_with()


def test_iteration_period_rejects_negative_value(tmp_path: Path) -> None:
    conf = cfg.ConfigOpts()
    conf.register_cli_opts(cmd.core_agent_opts, cmd.DOMAIN)
    config_path = tmp_path / "agent.conf"
    config_path.write_text("[agent]\niter_min_period = -1\n")
    with pytest.raises(SystemExit) as error:
        conf(["--config-file", str(config_path)])
    assert error.value.code == 1
