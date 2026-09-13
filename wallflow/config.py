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
        # build thumbnails + transcodes as soon as the watcher sees a change, instead of
        # only lazily on first open/pick — shortens picker startup after adding wallpapers
        "auto_prewarm": True,
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
    "depth": {
        # foreground cutout for the overlay (needs `wallflow depth setup` once; no-op until then)
        "enabled": True,
        # what goes on the 3D layer:
        #   "auto"    = the salient subject (rembg: character, bike, mountain …) plus whatever
        #               the depth map (Depth Anything V2) says is clearly NEARER than the
        #               subject (a fan in front of it, foam over a face) — floors/walls the
        #               subject stands on are as near as the subject and stay out
        #   "near"    = depth-first: the nearest depth group per image (Otsu split); the
        #               subject model only decides whether the character joins it
        #   "depth"   = the near group only, no character logic
        #   "subject" = the salient subject/character (rembg), regardless of depth
        #   "both"    = union of depth and subject
        "mode": "auto",
        "depth_model": "onnx-community/depth-anything-v2-small",   # …-base / …-large: better, slower
        "near": "auto",               # "auto" = per-image histogram split; or a number = fraction of the
                                      # depth range from the nearest point (0.3 = nearest 30 %)
        "feather": 0.06,              # softness of the near/far boundary (fraction of the depth range)
        "min_separation": 0.30,       # auto/near: below this near/far distinctness (0..1) and with no subject,
                                      # the image is treated as having nothing in front (smooth landscapes)
        "occluder_margin": 0.05,      # auto: how much nearer than the subject's nearest parts something must be
                                      # to be added in front of it (bigger = only clear occluders)
        "model": "isnet-anime",       # rembg model for subject/both: isnet-anime | isnet-general-use | birefnet-general
        "alpha_matting": False,       # subject/both: finer edges (hair), several times slower
        "auto": False,                # `wallflow watch` segments new images as they appear (slow on CPU!)
        "notify": True,               # desktop notification while a cutout is being made in the background
    },
    "overlay": {
        # the click-through layer between wallpaper and windows (quickshell)
        "enabled": True,
        "outputs": "*",               # "*" = every monitor, or "DP-1,HDMI-A-1"
        "fill": "crop",               # how the cutout is fitted: crop (= cover, what the backends do) | fit
        # clock widget (`wallflow addons install clock`) — fractions of the screen, px, Qt formats
        "clock_x": 0.5,
        "clock_y": 0.12,
        "clock_size": 140,
        "clock_format": "HH:mm",
        "clock_date_format": "dddd, d MMMM",   # "" = no date line
        "clock_font": "",             # "" = system sans
        "clock_weight": "light",      # thin | light | normal | bold | black
        "clock_color": "#ffffff",
        "clock_opacity": 0.92,
        "clock_shadow": True,
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
        # close any active Discord/Spotify-style special workspace before the
        # picker opens, so it doesn't appear underneath it
        "close_special_workspaces": True,
        # same idea for pinned windows (Hyprland's `pin` dispatcher) — unpinned
        # while the picker is open, re-pinned the moment it closes
        "hide_pinned_windows": True,
    },
    "hypr": {
        "bind": "SUPER + W",          # Lua form; .conf gets "SUPER, W"
        "bind_all": "",                # "show hidden" bind; "" = bind's mods + SHIFT
        "bind_edit": "",               # toggle overlay edit mode (drag widgets); "" = no bind
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
