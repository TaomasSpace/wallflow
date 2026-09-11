#!/usr/bin/env bash
# Removes wallflow. Add --purge to also delete config, addons state and cache.
# Thin wrapper around `wallflow uninstall`; falls back to plain rm if the launcher is gone.
set -euo pipefail
BIN="$HOME/.local/bin/wallflow"
if [ -x "$BIN" ]; then
    exec "$BIN" uninstall --yes "$@"
fi
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/wallflow"
CFG="${XDG_CONFIG_HOME:-$HOME/.config}/wallflow"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/wallflow"
pkill -f "wallflow.py pauser" 2>/dev/null || true
pkill -x mpvpaper 2>/dev/null || true
rm -rf "$DEST" "$BIN"
if [ "${1:-}" = "--purge" ]; then
    rm -rf "$CFG" "$CACHE"
    echo "removed wallflow, config and cache"
else
    echo "removed wallflow (config in $CFG and cache in $CACHE kept; --purge deletes them)"
fi
