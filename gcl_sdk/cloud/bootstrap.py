# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0

import configparser
import io
import shlex
from urllib.parse import urlsplit
import uuid

import yaml

from gcl_sdk.agents.universal import constants as agent_constants


def validate_agent_config(orch_endpoint, status_endpoint, package):
    if not package.strip():
        raise ValueError("Agent package must be a nonempty requirement or URL")
    for endpoint in (orch_endpoint, status_endpoint):
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError(
                "Agent endpoint must be an HTTP(S) URL without credentials"
            )


def agent_user_data(
    node,
    private_key,
    orch_endpoint,
    status_endpoint,
    package,
    hostname=None,
    ssh_keys=(),
    caps_drivers=None,
):
    """Bootstrap an ordinary cloud image without replacing its metadata API."""
    node = str(uuid.UUID(str(node)))
    validate_agent_config(orch_endpoint, status_endpoint, package)
    config = configparser.ConfigParser(interpolation=None)
    config["universal_agent"] = {
        "uuid": node,
        "orch_endpoint": orch_endpoint,
        "status_endpoint": status_endpoint,
        "private_key_path": agent_constants.PRIVATE_KEY_PATH,
        "caps_drivers": caps_drivers
        or "GuestMachineCapabilityDriver,RenderAgentDriver,ServiceCapabilityDriver,SSHKeyCapabilityDriver",
    }
    rendered = io.StringIO()
    config.write(rendered)
    unit = """[Unit]
Description=Exordos cloud agent
Wants=network-online.target
After=network-online.target
[Service]
ExecStartPre=/opt/exordos/install-agent
TimeoutStartSec=900
ExecStart=/opt/exordos/venv/bin/exordos-universal-agent --config-file /etc/exordos/agent.conf
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
"""
    installer = (
        """#!/bin/sh
set -eu
if [ ! -x /opt/exordos/venv/bin/exordos-universal-agent ]; then
    python3 -m venv /opt/exordos/venv
    /opt/exordos/venv/bin/pip install """
        + shlex.quote(package)
        + "\nfi\n"
    )
    files = [
        ("/opt/exordos/install-agent", installer, "0700"),
        ("/var/lib/exordos/node-id", node + "\n", "0644"),
        (agent_constants.PRIVATE_KEY_PATH, private_key, "0600"),
        ("/etc/exordos/agent.conf", rendered.getvalue(), "0600"),
        ("/etc/systemd/system/exordos-agent.service", unit, "0644"),
    ]
    # Exordos images may have services waiting for configuration delivered by
    # this agent. cloud-final runs after multi-user.target, so queue agent
    # startup after cloud-config without waiting for cloud-final to unblock.
    # Only preinstalled Exordos images can use this path: a plain cloud image
    # first needs cloud-final to install the venv and native build packages.
    early_start = (
        "if systemctl cat exordos-universal-agent.service >/dev/null 2>&1; "
        "then systemctl disable --now exordos-universal-agent.service; fi; "
        "systemctl daemon-reload; systemctl enable --now --no-block exordos-agent.service"
    )
    data = {
        "bootcmd": [
            [
                "systemd-run",
                "--unit=exordos-cloud-bootstrap",
                "--no-block",
                "--property=After=cloud-config.service",
                "--property=ExecCondition=/usr/bin/test -x /usr/bin/exordos-universal-agent",
                "/bin/sh",
                "-c",
                early_start,
            ]
        ],
        "package_update": True,
        "packages": [
            "python3-venv",
            "python3-dev",
            "gcc",
            "libev-dev",
        ],
        "write_files": [
            {
                "path": path,
                "content": content,
                "permissions": mode,
                "owner": "root:root",
            }
            for path, content, mode in files
        ],
        "runcmd": [
            [
                "sh",
                "-c",
                "if systemctl cat exordos-universal-agent.service >/dev/null 2>&1; then systemctl disable --now exordos-universal-agent.service; fi",
            ],
            ["systemctl", "daemon-reload"],
            ["systemctl", "enable", "--now", "--no-block", "exordos-agent.service"],
        ],
    }
    if hostname:
        data["hostname"] = hostname
    if ssh_keys:
        data["ssh_authorized_keys"] = list(ssh_keys)
    return "#cloud-config\n" + yaml.safe_dump(data)
