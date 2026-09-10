#!/usr/bin/env bash
# wallflow addon: drop-in wrapper that shadows pipes.sh in ~/.local/bin.
# pipes.sh's own screen reset does a terminal RIS, which makes foot fall back
# to the palette it loaded at startup. We disable that reset and instead
# restart pipes.sh every $WALLFLOW_PIPES_INTERVAL seconds (default 300),
# re-applying the current wallpaper colours. All arguments pass through.
self="$(readlink -f "$0")"
real=""
IFS=: read -ra dirs <<< "$PATH"
for d in "${dirs[@]}"; do
    c="$d/pipes.sh"
    [ -x "$c" ] && [ "$(readlink -f "$c")" != "$self" ] && { real="$c"; break; }
done
[ -n "$real" ] || { echo "wallflow pipes: real pipes.sh not found in PATH" >&2; exit 127; }
interval="${WALLFLOW_PIPES_INTERVAL:-300}"
while :; do
    timeout "$interval" "$real" "$@" -r 0
    cat ~/.cache/wallflow/sequences 2>/dev/null
    printf '\e[H\e[2J'
done
