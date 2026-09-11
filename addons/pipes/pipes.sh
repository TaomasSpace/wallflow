#!/usr/bin/env bash
# wallflow addon: drop-in wrapper that shadows pipes.sh in ~/.local/bin.
# pipes.sh's own screen reset does a terminal RIS, which makes foot fall back
# to the palette it loaded at startup. We disable that reset and instead
# restart pipes.sh every addons.pipes_interval seconds (config.toml, default 60;
# $WALLFLOW_PIPES_INTERVAL overrides),
# re-applying the current wallpaper colours. All arguments pass through.
# Installed in both ~/.local/bin and /usr/local/bin — skip every copy of this
# wrapper (marker below), not just $0, or the two would exec each other forever.
# wallflow-pipes-wrapper
real=""
IFS=: read -ra dirs <<< "$PATH"
for d in "${dirs[@]}"; do
    c="$d/pipes.sh"
    [ -x "$c" ] || continue
    grep -qs "wallflow-pipes-wrapper" "$c" && continue
    real="$c"; break
done
[ -n "$real" ] || [ ! -x /usr/bin/pipes.sh ] || real=/usr/bin/pipes.sh
[ -n "$real" ] || { echo "wallflow pipes: real pipes.sh not found in PATH" >&2; exit 127; }
wf="$HOME/.local/bin/wallflow"; [ -x "$wf" ] || wf=wallflow
interval_from_config() {
    local v
    v="$("$wf" config get addons.pipes_interval 2>/dev/null)"
    [[ "$v" =~ ^[0-9]+$ ]] && echo "$v" || echo 60
}
while :; do
    # env var wins; otherwise re-read the config every round so edits apply live
    interval="${WALLFLOW_PIPES_INTERVAL:-$(interval_from_config)}"
    timeout --foreground "$interval" "$real" "$@" -r 0
    cat ~/.cache/wallflow/sequences 2>/dev/null
    printf '\e[H\e[2J'
done
