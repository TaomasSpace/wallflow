#!/usr/bin/env bash
set -euo pipefail
DIR="$HOME/Pictures/Wallpapers"
cd "$DIR"

rename_group() {
    local pattern="$1" prefix="$2" ext="$3" i=1
    # collect, natural sort, two-pass to avoid collisions
    mapfile -t files < <(find . -maxdepth 1 -type f -iname "$pattern" -printf '%f\n' | sort -V)
    for f in "${files[@]}"; do
        mv -- "$f" ".tmp_$i.$ext"
        ((i++))
    done
    i=1
    for f in "${files[@]}"; do
        mv -- ".tmp_$i.$ext" "${prefix}${i}.${ext}"
        ((i++))
    done
}

rename_group "*.mp4" "animated_wallpaper" "mp4"
rename_group "*.jpg" "wallpaper" "jpg"
