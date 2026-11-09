#!/bin/bash
# Copyright 2026 Genesis Corporation. Licensed under the Apache License, Version 2.0.
# Root networking tests must own their network AND the files/sockets used by
# dhclient hooks. A private network namespace alone still reaches host resolved.
set -euo pipefail

if [[ ${1-} != --inside ]]; then
    if [[ $EUID != 0 || $# == 0 ]]; then
        echo "usage: sudo $0 COMMAND [ARG ...]" >&2
        exit 2
    fi
    for tool in unshare mount mountpoint cp hostname ovsdb-tool ovsdb-server ovs-vswitchd ovs-vsctl ip; do
        command -v "$tool" >/dev/null
    done
    [[ $(readlink -f /var/run) == /run && -d /var/lib/dhcp ]]
    task_root=$(mktemp -d /tmp/exordos-sdn-isolated.XXXXXX)
    # Remove the private /etc only after PID 1 and its namespace tree exit.
    task_unshare=
    task_init=
    task_init_ns=
    cleanup() {
        trap - EXIT INT TERM HUP
        task_unshare=${task_unshare:-$(jobs -pr)}
        if [[ -n $task_unshare ]]; then
            if [[ -n $task_init_ns && $(readlink /proc/"$task_init"/ns/pid 2>/dev/null || :) == "$task_init_ns" ]]; then
                kill -KILL "$task_init" 2>/dev/null || :
            else
                kill -KILL "$task_unshare" 2>/dev/null || :
            fi
            wait "$task_unshare" 2>/dev/null || :
        fi
        # Also covers forced death of unshare: --kill-child kills PID 1,
        # whose exit waits for every process in its namespace to terminate.
        for ((attempt=0; attempt<100; attempt++)); do
            [[ -z $task_init_ns || $(readlink /proc/"$task_init"/ns/pid 2>/dev/null || :) != "$task_init_ns" ]] && break
            sleep .02
        done
        if [[ -n $task_init_ns && $(readlink /proc/"$task_init"/ns/pid 2>/dev/null || :) == "$task_init_ns" ]]; then
            echo "namespace still exiting; private files retained at $task_root" >&2
            return 1
        fi
        rm -rf -- "$task_root"
    }
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    trap 'exit 129' HUP
    for kind in mnt net pid uts; do
        export "SDN_ORIGINAL_${kind}=$(readlink /proc/self/ns/$kind)"
    done
    unshare --mount --net --uts --pid --fork --mount-proc --kill-child=KILL \
        "$0" --inside "$task_root" "$@" <&0 &
    task_unshare=$!
    for ((attempt=0; attempt<100; attempt++)); do
        read -r task_init < /proc/"$task_unshare"/task/"$task_unshare"/children || :
        if [[ -n $task_init ]]; then
            task_init_ns=$(readlink /proc/"$task_init"/ns/pid 2>/dev/null || :)
            break
        fi
        kill -0 "$task_unshare" 2>/dev/null || break
        sleep .01
    done
    wait "$task_unshare"
    task_unshare=
    exit 0
fi

shift
task_root=$1
shift
# Make propagation private BEFORE replacing any filesystem view.
mount --make-rprivate /
for kind in mnt net pid uts; do
    original="SDN_ORIGINAL_${kind}"
    [[ $(readlink /proc/self/ns/"$kind") != "${!original}" ]]
done
mkdir "$task_root/etc"
cp -a /etc/. "$task_root/etc/"
# Capture the original symlink's CONTENT before /run is hidden. Replacing
# only its target would miss dhclient hooks that write elsewhere under /etc.
cp -L /etc/resolv.conf "$task_root/resolv.conf"
rm -f "$task_root/etc/resolv.conf"
mv "$task_root/resolv.conf" "$task_root/etc/resolv.conf"
mount --bind "$task_root/etc" /etc
mount -t tmpfs -o mode=755 tmpfs /run
# Preserve runtimes (notably CI's /opt/hostedtoolcache), but make forgotten
# driver path overrides fail EROFS instead of writing host agent/connector data.
for path in /opt /var/lib; do
    mount --bind "$path" "$path"
    mount -o remount,bind,ro "$path"
done
mount -t tmpfs -o mode=755 tmpfs /var/lib/dhcp
# /sys/class/net must describe this network namespace, including hook ifindices.
mount -t sysfs -o ro,nosuid,nodev,noexec sysfs /sys
hostname sdn-integration
ip link set lo up

mountpoint -q /etc
mountpoint -q /run
mountpoint -q /var/lib/dhcp
mountpoint -q /var/lib
mountpoint -q /opt
[[ -f /etc/resolv.conf && ! -L /etc/resolv.conf ]]
[[ ! -e /run/dbus/system_bus_socket && ! -e /run/systemd/private ]]
unset DBUS_SYSTEM_BUS_ADDRESS DBUS_SESSION_BUS_ADDRESS SYSTEMD_BUS_ADDRESS
unset SYSTEMD_HOST SYSTEMD_MACHINE NOTIFY_SOCKET
export XDG_RUNTIME_DIR=/run/user/0
export OVS_RUNDIR=/run/openvswitch OVS_DBDIR=/run/openvswitch
export OVS_LOGDIR=/run/openvswitch OVS_SYSCONFDIR=/run/openvswitch
mkdir -p "$XDG_RUNTIME_DIR" "$OVS_RUNDIR" /run/netns /run/lock
chmod 700 "$XDG_RUNTIME_DIR"
# The test fixtures reject accidental direct root execution without this.
touch /run/exordos-sdn-isolated
export SDN_INTEGRATION_ISOLATED=1

ovsdb-tool create "$OVS_DBDIR/conf.db" /usr/share/openvswitch/vswitch.ovsschema
ovsdb-server "$OVS_DBDIR/conf.db" --remote="punix:$OVS_RUNDIR/db.sock" \
    --pidfile --detach --no-chdir --log-file
ovs-vsctl --no-wait init
ovs-vswitchd "unix:$OVS_RUNDIR/db.sock" --pidfile --detach --no-chdir --log-file
"$@"
