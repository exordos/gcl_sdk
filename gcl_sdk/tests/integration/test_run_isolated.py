# Copyright 2026 Genesis Corporation. Licensed under the Apache License, Version 2.0.
"""Check the boundary BEFORE invoking any stock DHCP script or hook."""

import errno
import json
import os
from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0, reason="run-isolated.sh requires root"
)


def _assert_isolated():
    isolated = os.environ.get("SDN_INTEGRATION_ISOLATED")
    assert isolated == "1", "use run-isolated.sh"
    assert Path("/run/exordos-sdn-isolated").is_file()
    for kind in ("mnt", "net", "pid", "uts"):
        original = os.environ["SDN_ORIGINAL_" + kind]
        assert os.readlink("/proc/self/ns/" + kind) != original
    for path in ("/etc", "/run", "/var/lib", "/var/lib/dhcp", "/opt", "/sys"):
        assert subprocess.run(["mountpoint", "-q", path]).returncode == 0, path
    for path in ("/opt", "/var/lib"):
        assert os.statvfs(path).f_flag & os.ST_RDONLY, path
    assert Path("/etc/resolv.conf").is_file()
    assert not Path("/etc/resolv.conf").is_symlink()
    assert not Path("/run/dbus/system_bus_socket").exists()
    assert not Path("/run/systemd/private").exists()
    assert not os.environ.get("DBUS_SYSTEM_BUS_ADDRESS")
    assert not os.environ.get("SYSTEMD_BUS_ADDRESS")


def test_namespaces_and_control_paths_are_private():
    _assert_isolated()
    assert subprocess.check_output(["hostname"], text=True).strip() == "sdn-integration"
    # Guard above must succeed BEFORE attempting an open for writing. No
    # bytes are ever written, even if a broken boundary unexpectedly opens it.
    known = {
        "/opt": Path("/opt/evpn/evpn_connector.cfg"),
        "/var/lib": Path("/var/lib/dpkg/status"),
    }
    stdlib = Path(os.__file__).resolve()
    if stdlib.is_relative_to("/opt") and stdlib.is_file():
        known["/opt"] = stdlib
    for root, target in known.items():
        flags = os.O_RDWR
        if not target.is_file():
            target = Path(root, ".exordos-sdn-readonly-" + str(os.getpid()))
            flags |= os.O_CREAT | os.O_EXCL
        with pytest.raises(OSError) as failure:
            fd = os.open(target, flags, 0o600)
            os.close(fd)
        assert failure.value.errno == errno.EROFS, target


def test_stock_dhclient_script_and_resolved_hook_stay_private():
    # This must remain first: running this test without the runner is a safe
    # failing control, with NO DHCP/script/network mutations on the host.
    _assert_isolated()
    script = Path("/sbin/dhclient-script")
    if not script.exists():
        pytest.skip("stock ISC dhclient-script not installed")
    interface = "sdn-dhcp-test"
    subprocess.run(["ip", "link", "add", interface, "type", "dummy"], check=True)
    subprocess.run(["ip", "link", "set", interface, "up"], check=True)
    index = int(Path("/sys/class/net", interface, "ifindex").read_text())
    env = dict(
        os.environ,
        reason="BOUND",
        interface=interface,
        new_ip_address="192.0.2.2",
        new_subnet_mask="255.255.255.0",
        new_domain_name_servers="192.0.2.53",
        new_domain_name="sdn-test.invalid",
    )
    resolv = Path("/etc/resolv.conf")
    original = resolv.read_bytes()
    resolved = Path("/run/systemd/resolve/netif", str(index))
    disabled = []
    enabled = (
        subprocess.run(
            ["systemctl", "is-enabled", "systemd-resolved"], capture_output=True
        ).returncode
        == 0
    )
    try:
        # The real resolved hook writes its state while its private bus is absent.
        subprocess.run([str(script)], env=env, check=True)
        if enabled and Path("/etc/dhcp/dhclient-exit-hooks.d/resolved").exists():
            assert "192.0.2.53" in resolved.read_text()
        # Exercise the stock writer without private resolver enter-hook overrides.
        for hook in (
            Path("/etc/dhcp/dhclient-enter-hooks"),
            Path("/etc/dhcp/dhclient-enter-hooks.d"),
        ):
            if hook.exists():
                target = hook.with_suffix(".isolation-disabled")
                hook.rename(target)
                disabled.append((hook, target))
        env.update(
            reason="RENEW",
            old_ip_address="192.0.2.2",
            new_domain_name_servers="198.51.100.53",
        )
        subprocess.run([str(script)], env=env, check=True)
        assert "nameserver 198.51.100.53" in resolv.read_text()
        if resolved.exists():
            assert "198.51.100.53" in resolved.read_text()
        print(
            json.dumps(
                {
                    "private_ifindex": index,
                    "fake_dns": "198.51.100.53",
                    "resolv_conf": resolv.read_text(),
                    "resolved_state": resolved.read_text()
                    if resolved.exists()
                    else None,
                }
            )
        )
    finally:
        resolv.write_bytes(original)
        for hook, target in reversed(disabled):
            target.rename(hook)
        resolved.unlink(missing_ok=True)
        Path("/run/network", "isc-dhcp-v4-" + interface).unlink(missing_ok=True)
        subprocess.run(["ip", "link", "del", interface], check=True)
