"""~/.config/wallflow/config.toml — everything auto-detected at setup, all overridable."""
import copy
import json
import os
import tomllib
from pathlib import Path

from . import paths

DEFAULTS: dict = {
    "general": {
        "wallpaper_dir": "~/Pictures/Wallpapers",
        "recursive": True,
        # extensions treated as image / video (GIF is a video: it plays in mpvpaper)
        "image_exts": [".jpg", ".jpeg", ".png", ".webp", ".avif", ".bmp", ".tif", ".tiff", ".jxl"],
        "video_exts": [".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v", ".gif"],
        # subfolder (directly under wallpaper_dir) auto-created and excluded from the
        # normal picker/cycling; only shown via the "show hidden" bind (default SUPER+SHIFT+W)
        "hidden_dir_name": "hidden",
    },
    "image": {
        "backend": "caelestia",       # caelestia | swww | hyprpaper | none
    },
    "theme": {
        "enabled": True,
        # engine = who recolours terminals/apps from the wallpaper:
        #   "caelestia": caelestia's own scheme (persistent, survives terminal resets);
        #                wallust then only renders addon templates (-s, no sequences)
        #   "wallust":   wallust broadcasts colour sequences to open terminals itself
        "engine": "wallust",          # "caelestia" = let caelestia's scheme drive it instead
        # where wallflow pushes the colour sequences: "terminals" = only ptys owned by
        # known terminal emulators (no stray notifications), "all" = every /dev/pts, "none"
        "broadcast": "terminals",
        "terminals": ["foot", "kitty", "alacritty", "wezterm-gui", "ghostty", "st", "urxvt",
                      "konsole", "gnome-terminal-server", "xfce4-terminal", "tilix"],
        "palette": "dark16",          # wallust palette — `wallflow theme list` / `wallflow theme set <name>`
        "contrast": True,             # wallust -k: lift slots that would be unreadable on the background
        "wallust_args": [],           # extra raw wallust args
    },
    "video": {
        "outputs": "*",               # mpvpaper output(s): "*" = all, or e.g. "DP-1"
        "mpv_opts": "no-audio loop hwdec=auto",
        "pause_on_fullscreen": True,
        # At login mpvpaper must be created AFTER the image backend's background
        # layer, or it ends up underneath it (same layer level, later surface wins).
        # restore waits for this layer namespace (from `hyprctl layers`); "" = don't
        # wait, "auto" = pick by image.backend. restore_delay = extra seconds after that.
        "wait_for_layer": "auto",
        "restore_timeout": 20,
        "restore_delay": 0.5,
    },
    "transcode": {
        "enabled": True,
        "encoder": "auto",            # auto | hevc_nvenc | hevc_vaapi | libx265 | ... | none
        "fps": 30,
        "max_width": 2560,            # set from your largest monitor at setup
        "quality": 28,                # cq/crf/qp — lower = better/bigger
        "vaapi_device": "",           # auto-detected; e.g. /dev/dri/renderD128
    },
    "rename": {
        # 0 = off, 1 = prefix animated files with animated_prefix (sort by type only),
        # 2 = full rename to <animated_wallpaper_prefix>_N / <wallpaper_prefix>_N
        "mode": 0,
        "animated_prefix": "animated_",             # mode 1 prefix
        "wallpaper_prefix": "wallpaper",             # mode 2 base name for stills
        "animated_wallpaper_prefix": "animated_wallpaper",  # mode 2 base name for animated
    },
    "addons": {
        # pipes addon: pipes.sh is restarted every N seconds with the current palette
        # (its own reset would drop the terminal back to the startup colours)
        "pipes_interval": 60,
    },
    "ui": {
        "thumb_width": 900,
        "backdrop": "#e6101216",
    },
    "hypr": {
        "bind": "SUPER + W",          # Lua form; .conf gets "SUPER, W"
        "bind_all": "",                # "show hidden" bind; "" = bind's mods + SHIFT
        "config_file": "",            # auto: hyprland.lua > hyprland.conf
        "manage": True,               # write autostart/bind block into the config
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    if paths.CONFIG_FILE.exists():
        with open(paths.CONFIG_FILE, "rb") as f:
            return _merge(DEFAULTS, tomllib.load(f))
    return copy.deepcopy(DEFAULTS)


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    return json.dumps(str(v))


def dumps(cfg: dict) -> str:
    lines = ["# wallflow config — edit freely; `wallflow setup` only fills in missing keys", ""]
    for section, body in cfg.items():
        lines.append(f"[{section}]")
        for k, v in body.items():
            lines.append(f"{k} = {_fmt(v)}")
        lines.append("")
    return "\n".join(lines)


def save(cfg: dict) -> None:
    paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    paths.CONFIG_FILE.write_text(dumps(cfg))


def get(cfg: dict, dotted: str):
    sec, _, key = dotted.partition(".")
    return cfg[sec][key]


def parse_value(raw: str):
    """CLI value -> typed value. 'true', '30', '2.5', '[a,b]' or plain string."""
    s = raw.strip()
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    for cast in (int, float):
        try:
            return cast(s)
        except ValueError:
            pass
    if s.startswith("["):
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return [x.strip() for x in s.strip("[]").split(",") if x.strip()]
    return s


def set_value(cfg: dict, dotted: str, raw: str) -> dict:
    sec, _, key = dotted.partition(".")
    if sec not in DEFAULTS or key not in DEFAULTS[sec]:
        raise KeyError(f"unknown key {dotted!r}")
    cfg.setdefault(sec, {})[key] = parse_value(raw)
    return cfg


# --- convenience -----------------------------------------------------------

def wallpaper_dir(cfg: dict) -> Path:
    return Path(os.path.expanduser(cfg["general"]["wallpaper_dir"]))


def is_video(cfg: dict, path) -> bool:
    return os.path.splitext(str(path))[1].lower() in cfg["general"]["video_exts"]


def all_exts(cfg: dict) -> tuple:
    return tuple(cfg["general"]["image_exts"]) + tuple(cfg["general"]["video_exts"])
