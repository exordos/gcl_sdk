#!/usr/bin/env bash
#    Copyright 2026 Genesis Corporation.
#    Licensed under the Apache License, Version 2.0 (the "License")
#
# One-time preparation for the SDN fabric integration tier: fetches the
# gobgp binaries and builds the evpn_connector virtualenv into the cache
# directory the tests look in (override with SDN_FABRIC_CACHE).
#
# Usage:
#   EVPN_CONNECTOR_PATH=/path/to/evpn_connector ./prepare.sh
set -euo pipefail

CACHE="${SDN_FABRIC_CACHE:-$HOME/.cache/exordos-sdn-fabric}"
# The version the installation actually ships (exordos_core's install.sh),
# because a tier that proves the fabric on a different daemon than the one
# deployed proves it about nothing: the gRPC service was renamed between
# gobgp 3 and 4, so a mismatched pair does not merely behave differently,
# it does not talk at all. Keep this in step with install.sh.
GOBGP_URL="${GOBGP_URL:-https://github.com/osrg/gobgp/releases/download/v3.37.0/gobgp_3.37.0_linux_amd64.tar.gz}"
GOBGP_SHA256="${GOBGP_SHA256:-e20b2a155fe14450b9fe37e5c1a1d1bfe101eb479645f5bbea860a8fde30e522}"
EVPN_SRC="${EVPN_CONNECTOR_PATH:?set EVPN_CONNECTOR_PATH to the evpn_connector checkout}"

mkdir -p "$CACHE/bin"
if [ ! -x "$CACHE/bin/gobgpd" ]; then
    tmp="$(mktemp -d)"
    curl -fsSL --retry 5 --retry-all-errors -o "$tmp/gobgp.tar.gz" "$GOBGP_URL"
    echo "$GOBGP_SHA256  $tmp/gobgp.tar.gz" | sha256sum -c -
    tar -xzf "$tmp/gobgp.tar.gz" -C "$CACHE/bin" gobgpd gobgp
    rm -rf "$tmp"
fi

if [ ! -x "$CACHE/venv/bin/evpn-connector" ]; then
    # On the host python: the connector's modern dependency band needs a
    # newer interpreter than the 2.7-compatible one it also still supports.
    python3 -m venv "$CACHE/venv"
    # The distro's bundled pip cannot parse some of the dependency metadata.
    "$CACHE/venv/bin/pip" install -q --upgrade pip
    PBR_VERSION=0.0.1 "$CACHE/venv/bin/pip" install -q setuptools "$EVPN_SRC"
fi

echo "fabric cache ready at $CACHE"
