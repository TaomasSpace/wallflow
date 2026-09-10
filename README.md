# wallflow

Coverflow wallpaper picker for **Hyprland**. Images, videos and GIFs, hardware-decoded video
wallpapers via `mpvpaper`, colour theming via `wallust`, addons that recolour your terminal apps.

```
git clone https://github.com/<you>/wallflow && cd wallflow && ./install.sh
```

That's it. The installer

- installs the dependencies (Arch fully automatic incl. AUR `wallust`; Debian/Fedora best-effort),
- copies itself to `~/.local/share/wallflow` and puts `wallflow` in `~/.local/bin`,
- detects your image backend (caelestia / swww / hyprpaper), GPU + working hardware encoder,
  largest monitor, Hyprland config flavour (`hyprland.lua` or `hyprland.conf`),
- writes the config to `~/.config/wallflow/config.toml`,
- adds **autostart** (restore last wallpaper + fullscreen pauser) and a **keybind** (`SUPER + W`)
  to your Hyprland config inside a marked block, and reloads Hyprland.

The only question it asks is the wallpaper folder (default `~/Pictures/Wallpapers`).
`./install.sh --yes` asks nothing. Other flags: `--wallpaper-dir DIR`, `--bind 'SUPER + SHIFT + W'`,
`--no-deps`, `--no-hypr`.

## Using it

| | |
|---|---|
| `SUPER + W` / `wallflow` | open the picker — ←/→ or scroll, Enter apply, R random, Esc |
| `wallflow next` / `prev` / `random` | cycle without the UI (bind these too if you like) |
| `wallflow apply <file>` | set a wallpaper directly |
| `wallflow addons list` | show addons; `install <name>` / `remove <name>` / `info <name>` |
| `wallflow transcode` | pre-transcode all video wallpapers now (otherwise it happens lazily) |
| `wallflow config edit` | open the config; `config set transcode.fps 24` for one key |
| `wallflow setup --redetect` | re-run detection (new GPU, new monitor, switched backend) |
| `wallflow doctor` | print everything that was detected |
| `wallflow update` | pull the newest version from GitHub and reinstall (`--check` only looks) |

### Videos and GIFs

Videos and GIFs play through `mpvpaper` (`hwdec=auto` → NVDEC/VA-API). Switching video→video is a
live `loadfile` on the running instance, so there's no flash.

**Transcoding** is automatic: the first time you pick a video that's bigger than your largest
monitor, above `transcode.fps` (30), a GIF, or not H.264/HEVC, the original plays immediately and a
background job re-encodes it to `~/.cache/wallflow/transcoded/`. When the job finishes it hot-swaps
the lighter file in; every later pick uses it directly. Clips that are already within limits are
played as-is. The encoder is probed at setup (`hevc_nvenc` → `hevc_vaapi` → `libx265` …) and can be
overridden in the config. Changing `fps`, `max_width`, `quality` or `encoder` invalidates old
transcodes; `wallflow transcode --prune` deletes the leftovers.

**Fullscreen pause**: a tiny watcher on Hyprland's socket2 pauses mpvpaper while any window is
fullscreen (games, videos) and resumes it after. Disable with `video.pause_on_fullscreen = false`.

### Theming and addons

Every wallpaper change runs `wallust run <image>` (for videos: a still frame), which also broadcasts
colour sequences to open terminals — so `pipes.sh`, `cmatrix`, `tty-clock` & co. recolour live
without any addon.

Apps that keep their own colour config get an **addon**: a wallust template plus a tiny hook into
the app's config. `wallflow addons list` shows what's available:

```
alacritty  alacritty colours (live_config_reload picks them up)
btop       btop theme from the palette (restart btop)
cava       6-stop gradient bars from the wallpaper palette
foot       foot terminal colours
kitty      kitty terminal colours, live reload
```

`wallflow addons install cava,kitty` installs, renders colours for the current wallpaper right
away, and from then on re-renders (+ reloads the app where possible) on every change.
`remove` undoes exactly what `install` did. Writing your own is a folder with an `addon.toml` and a
template — see `addons/README.md`.

### Config (`~/.config/wallflow/config.toml`)

```toml
[general]  wallpaper_dir, recursive, image_exts, video_exts
[image]    backend = "caelestia" | "swww" | "hyprpaper" | "none"
[theme]    enabled, wallust_args = ["-p", "dark16"]
[video]    outputs = "*" | "DP-1", mpv_opts, pause_on_fullscreen
[transcode] enabled, encoder = "auto"|"hevc_nvenc"|…|"none", fps, max_width, quality
[ui]       thumb_width, backdrop
[hypr]     bind = "SUPER + W", config_file (auto), manage = true
```

Everything the installer detected is just a default here — edit and rerun `wallflow setup` (it keeps
your values) or `wallflow config set hypr.bind 'SUPER + SHIFT + W'` (rewrites the Hyprland block).

## Requirements

`python ≥ 3.11`, `PySide6` (+ Qt Quick), `mpvpaper`, `ffmpeg`, optional `wallust` (theming),
`qt6-imageformats` (webp/tiff thumbnails). Arch: all handled by `install.sh`. Elsewhere `mpvpaper`
and `wallust` may need a manual build (`cargo install wallust`).

Hyprland config: the managed block is written in Lua for `hyprland.lua` and hyprlang for
`hyprland.conf`. Uninstall with `./uninstall.sh` (`--purge` also removes config and cache).

## Layout

```
wallflow.py        entry point
wallflow/          cli · config · detect · setup · hypr · backend · transcode · thumbs · pauser · addons · ui
addons/<name>/     addon.toml + wallust template
tools/             rename-wallpapers.sh — sequential naming for a wallpaper folder
install.sh · uninstall.sh
```
