#!/usr/bin/env bash
# wallflow installer — deps, copy, launcher, then `wallflow setup` (detection + Hyprland wiring).
#   ./install.sh [--yes] [--wallpaper-dir DIR] [--bind 'SUPER + W'] [--no-deps] [--no-hypr]
#   one-liner: curl -fsSL https://raw.githubusercontent.com/TaomasSpace/wallflow/master/install.sh | bash
set -euo pipefail

DEST="${XDG_DATA_HOME:-$HOME/.local/share}/wallflow"
MANAGED_SRC="${XDG_DATA_HOME:-$HOME/.local/share}/wallflow-src"
REPO_URL="${WALLFLOW_REPO:-https://github.com/TaomasSpace/wallflow}"

# Piped (`curl … | bash`) or run outside a checkout: clone into a managed
# location that `wallflow update` pulls from and `wallflow uninstall` deletes.
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "$(dirname "${BASH_SOURCE[0]}")/wallflow.py" ]; then
    SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
else
    if [ -d "$MANAGED_SRC/.git" ]; then
        git -C "$MANAGED_SRC" pull --ff-only --quiet || true
    else
        git clone --quiet "$REPO_URL" "$MANAGED_SRC"
    fi
    exec bash "$MANAGED_SRC/install.sh" "$@"
fi
BIN="$HOME/.local/bin"
YES=0; NODEPS=0; SETUP_ARGS=()

for arg in "$@"; do
    case "$arg" in
        --yes|-y) YES=1; SETUP_ARGS+=(--yes) ;;
        --no-deps) NODEPS=1 ;;
        --no-hypr) SETUP_ARGS+=(--no-hypr) ;;
        --wallpaper-dir=*|--bind=*) SETUP_ARGS+=("${arg%%=*}" "${arg#*=}") ;;
        --wallpaper-dir|--bind) SETUP_ARGS+=("$arg") ;;
        -h|--help) sed -n '2,3p' "$0"; exit 0 ;;
        *) SETUP_ARGS+=("$arg") ;;
    esac
done

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m !!\033[0m %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------- deps
install_deps() {
    if have pacman; then
        say "Arch: installing dependencies"
        sudo pacman -S --needed --noconfirm python pyside6 qt6-declarative qt6-imageformats ffmpeg jq
        # the depth overlay (widgets between wallpaper and subject) renders via quickshell
        have qs || sudo pacman -S --needed --noconfirm quickshell || warn "quickshell not installed — the overlay/clock addon needs it (optional)"
        local aur=""
        for h in paru yay; do have "$h" && aur="$h" && break; done
        for pkg in mpvpaper wallust; do            # both are AUR, not in the official repos
            have "$pkg" && continue
            if [ -n "$aur" ]; then
                say "installing $pkg from AUR via $aur"
                "$aur" -S --needed --noconfirm "$pkg" || warn "$pkg install failed — install it manually"
            else
                warn "no AUR helper (paru/yay) found — install '$pkg' from the AUR yourself"
            fi
        done
    elif have apt-get; then
        say "Debian/Ubuntu: installing what's packaged"
        sudo apt-get install -y python3 python3-pyside6.qtcore python3-pyside6.qtgui python3-pyside6.qtqml \
            python3-pyside6.qtquick qml6-module-qtquick qml6-module-qtquick-window ffmpeg jq || \
            warn "some packages failed — see README for manual steps"
        have mpvpaper || warn "mpvpaper is not packaged here: build it from https://github.com/GhostNaN/mpvpaper"
        have wallust  || warn "wallust missing: cargo install wallust  (or a release binary)"
        have qs       || warn "quickshell missing (optional, overlay/clock): https://quickshell.org"
    elif have dnf; then
        say "Fedora: installing what's packaged"
        sudo dnf install -y python3 python3-pyside6 ffmpeg jq || warn "some packages failed"
        have mpvpaper || warn "mpvpaper: try 'dnf install mpvpaper' or build from source"
        have wallust  || warn "wallust missing: cargo install wallust"
        have qs       || warn "quickshell missing (optional, overlay/clock): https://quickshell.org"
    else
        warn "unknown distro — make sure python3, PySide6, mpvpaper, ffmpeg, wallust are installed"
    fi
}

[ "$NODEPS" = 1 ] || install_deps

# ---------------------------------------------------------------- copy
say "installing to $DEST"
mkdir -p "$DEST" "$BIN"
if have rsync; then
    rsync -a --delete --exclude .git --exclude __pycache__ "$SRC/" "$DEST/"
else
    rm -rf "$DEST"; mkdir -p "$DEST"; cp -r "$SRC"/. "$DEST"/; rm -rf "$DEST/.git"
fi

# remember the source checkout so `wallflow update` can pull + reinstall
{
    echo "path=$SRC"
    echo "url=$(git -C "$SRC" remote get-url origin 2>/dev/null || echo https://github.com/TaomasSpace/wallflow)"
} > "$DEST/.source"

cat > "$BIN/wallflow" << LAUNCH
#!/usr/bin/env bash
exec python3 "$DEST/wallflow.py" "\$@"
LAUNCH
chmod +x "$BIN/wallflow"

case ":$PATH:" in
    *":$BIN:"*) ;;
    *) warn "$BIN is not on your PATH — add it (fish: fish_add_path ~/.local/bin; bash/zsh: export PATH=\"\$HOME/.local/bin:\$PATH\")"
       warn "the Hyprland bind/autostart use the absolute path, so they work regardless." ;;
esac

# ---------------------------------------------------------------- setup
say "running setup"
"$BIN/wallflow" setup "${SETUP_ARGS[@]}"

echo
say "done. Press your bind (default SUPER+W) or run 'wallflow'."
echo "    wallflow addons list       # terminal colour addons (cava, kitty, foot, …)"
echo "    wallflow transcode         # pre-transcode all video wallpapers now (optional)"
echo "    wallflow addons install clock  # clock widget on the depth overlay"
echo "    wallflow depth setup       # once: subject cutouts, so widgets sit *behind* the character"
echo "    wallflow config edit       # tweak anything"
