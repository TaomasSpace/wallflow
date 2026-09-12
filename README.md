# wallflow

Coverflow wallpaper picker for **Hyprland**. Images, videos and GIFs, hardware-decoded video
wallpapers via `mpvpaper`, colour theming via `wallust`, addons that recolour your terminal apps.

```
   curl -fsSL https://raw.githubusercontent.com/TaomasSpace/wallflow/master/install.sh | bash
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
| `SUPER + SHIFT + W` / `wallflow ui --all` | open the picker including the `hidden` folder |
| `wallflow next` / `prev` / `random` | cycle without the UI (bind these too if you like) |
| `wallflow apply <file>` | set a wallpaper directly |
| `wallflow theme list` | show wallust palettes; `set <name>` switches (saved + applied) |
| `wallflow addons list` | show addons; `install <name>` / `remove <name>` / `info <name>` |
| `wallflow transcode` | pre-transcode all video wallpapers now (otherwise it happens lazily) |
| `wallflow rename [--dry-run]` | rename wallpapers per `rename.mode` (0 off / 1 prefix / 2 full) |
| `wallflow config edit` | open the config; `config set transcode.fps 24` for one key |
| `wallflow setup --redetect` | re-run detection (new GPU, new monitor, switched backend) |
| `wallflow doctor` | print everything that was detected |
| `wallflow update` | pull the newest version from GitHub and reinstall (`--check` only looks) |

### Videos and GIFs

Videos and GIFs play through `mpvpaper` (`hwdec=auto` → NVDEC/VA-API). Switching video→video is a
live `loadfile` on the running instance, so there's no flash.

**Transcoding**: for video that's already H.264/HEVC but bigger than your largest monitor or above
`transcode.fps` (30), the original plays immediately and a background job re-encodes it to
`~/.cache/wallflow/transcoded/`; when it finishes it hot-swaps the lighter file in, and every later
pick uses it directly. For a GIF or a codec mpvpaper can't reliably loop (vp9/av1/…), playing the
original raw would look static/broken rather than just heavier — so the first pick waits briefly
for a real transcode (reusing one already in flight from `wallflow watch`'s prewarm, if there is
one) instead of ever showing the raw file. Clips already within limits and the right codec are
played as-is, no transcode. The encoder is probed at setup (`hevc_nvenc` → `hevc_vaapi` →
`libx265` …) and can be overridden in the config. Changing `fps`, `max_width`, `quality` or
`encoder` invalidates old transcodes; `wallflow transcode --prune` deletes the leftovers.

**Fullscreen pause**: a tiny watcher on Hyprland's socket2 pauses mpvpaper while any window is
fullscreen (games, videos) and resumes it after. Disable with `video.pause_on_fullscreen = false`.

### Prewarming (thumbnails + transcodes ahead of time)

By default (`general.auto_prewarm = true`) `wallflow watch` builds thumbnails and video
transcodes as soon as it sees a change in the wallpaper folder, instead of waiting for the
picker to need them. It also runs one pass immediately on start, so a fresh install/checkout
or a folder full of new wallpapers gets cached before you ever open the picker — shortens the
first `SUPER + W` after adding files. Set `auto_prewarm = false` to go back to the old lazy
behaviour (build on first pick/open only).

### Theming and addons

Every wallpaper change runs `wallust run <image>` (for videos: a still frame), which also broadcasts
colour sequences to open terminals — so `cmatrix`, `tty-clock` & co. recolour live
without any addon.

Apps that keep their own colour config get an **addon**: a wallust template plus a tiny hook into
the app's config. `wallflow addons list` shows what's available:

```
alacritty  alacritty colours (live_config_reload picks them up)
btop       btop theme from the palette, hot-reloaded
cava       6-stop gradient bars from the wallpaper palette
foot       foot terminal colours
kitty      kitty terminal colours, live reload
pipes      pipes.sh keeps the palette after its screen reset (drop-in wrapper)
```

`wallflow addons install cava,kitty` installs, renders colours for the current wallpaper right
away, and from then on re-renders (+ reloads the app where possible) on every change.
`remove` undoes exactly what `install` did. Writing your own is a folder with an `addon.toml` and a
template — see `addons/README.md`.

### Hidden wallpapers

A `hidden` subfolder is auto-created directly inside `wallpaper_dir` (`wallflow setup` creates it
if missing). It's skipped by the normal picker, `next`/`prev`/`random`, and `rename` — put
wallpapers there you don't want in the regular rotation.

`SUPER + SHIFT + W` (`wallflow ui --all`) opens the picker including `hidden`; applying one from
there works normally. Configurable: `hypr.bind_all` (default: main bind + SHIFT),
`general.hidden_dir_name` (default `"hidden"`).

### Opening on top of Discord/Spotify-style overlays

If your setup toggles apps via Hyprland special workspaces (`togglespecialworkspace`) or Hyprland's
`pin` dispatcher, those render above every normal workspace — so the picker would otherwise open
underneath them. Before showing the picker, wallflow:
- closes whichever special workspace is active **on the currently focused monitor only** (one on a
  different monitor is left alone — it won't overlap the picker, and touching it would just pull it
  onto this monitor instead of hiding it); toggle syntax is auto-detected (classic
  `hyprctl dispatch togglespecialworkspace <name>`, or the `hl.dsp.*` Lua-expression form some
  Hyprland-Lua-config builds require — same detection as the `hyprland.lua`/`hyprland.conf` flavour);
- unpins any pinned window, reopening the picker on top, then re-pins it the moment the picker closes.

Both are on by default; disable with `ui.close_special_workspaces = false` / `ui.hide_pinned_windows = false`.

### Renaming wallpapers

`wallflow rename` normalises filenames per directory, natural-sorted, collision-safe (renames go
through temp names first), and runs over the `hidden` folder too (each directory gets its own
numbering, so hidden files never collide with the main folder's). Mode is set in the config
(`rename.mode`):

```
0   off (default) — nothing is touched
1   prefix animated files with rename.animated_prefix ("animated_") — sorts by type
2   full rename: <rename.animated_wallpaper_prefix>_1, <rename.wallpaper_prefix>_1, …
```

The prefixes (`rename.animated_prefix`, `rename.wallpaper_prefix`, `rename.animated_wallpaper_prefix`)
are configurable, e.g. `wallflow config set rename.wallpaper_prefix wp`.

`wallflow rename --dry-run` prints the planned renames without touching anything.
`wallflow config set rename.mode 2` sets the mode.

When `rename.mode != 0`, two things happen automatically:
- `wallflow setup` renames immediately (safe to run any time — a no-op if already renamed).
- `wallflow watch` re-runs rename ~2s after any add/remove/move in the wallpaper folder (it also
  handles prewarming — see above; both live in the same watcher process, started/stopped by
  `setup` whenever `rename.mode != 0` **or** `auto_prewarm` is on).

### Config (`~/.config/wallflow/config.toml`)

```toml
[general]  wallpaper_dir, recursive, image_exts, video_exts, hidden_dir_name, auto_prewarm
[image]    backend = "caelestia" | "swww" | "hyprpaper" | "none"
[theme]    enabled, palette (`wallflow theme list|set`), contrast, wallust_args
[video]    outputs = "*" | "DP-1", mpv_opts, pause_on_fullscreen
[transcode] enabled, encoder = "auto"|"hevc_nvenc"|…|"none", fps, max_width, quality
[rename]   mode = 0 | 1 | 2 — see "Renaming wallpapers" above
[ui]       thumb_width, backdrop, close_special_workspaces, hide_pinned_windows
[hypr]     bind = "SUPER + W", bind_all (default: bind + SHIFT), config_file (auto), manage = true
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
wallflow/          cli · config · detect · setup · hypr · backend · transcode · thumbs · pauser · watcher · addons · ui · rename
addons/<name>/     addon.toml + wallust template
install.sh · uninstall.sh
```
