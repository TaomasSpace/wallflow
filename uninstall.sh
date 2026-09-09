#!/usr/bin/env bash
# Removes wallflow. Add --purge to also delete config, addons state and cache.
set -euo pipefail
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/wallflow"
BIN="$HOME/.local/bin/wallflow"
CFG="${XDG_CONFIG_HOME:-$HOME/.config}/wallflow"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/wallflow"

if [ -x "$BIN" ]; then
    for a in $("$BIN" addons list 2>/dev/null | awk '$2=="installed"{print $1}'); do
        "$BIN" addons remove "$a" || true
    done
    "$BIN" setup --remove || true
fi
pkill -f "wallflow.py pauser" 2>/dev/null || true
pkill -x mpvpaper 2>/dev/null || true
rm -rf "$DEST" "$BIN"
if [ "${1:-}" = "--purge" ]; then
    rm -rf "$CFG" "$CACHE"
    echo "removed wallflow, config and cache"
else
    echo "removed wallflow (config in $CFG and cache in $CACHE kept; --purge deletes them)"
fi
