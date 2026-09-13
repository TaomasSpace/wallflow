"""Auto-detection used by `wallflow setup` and `wallflow doctor`."""
import glob
import json
import os
import shutil
import subprocess
from pathlib import Path

from . import paths


def which(name: str) -> bool:
    return shutil.which(name) is not None


def _run(cmd, timeout=15):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


# --- hyprland --------------------------------------------------------------

def hyprland_running() -> bool:
    return bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))


def hyprland_config() -> tuple[Path, str]:
    """(config file, flavor) — flavor is 'lua' or 'conf'. Lua wins if both exist."""
    lua, conf = paths.HYPR_DIR / "hyprland.lua", paths.HYPR_DIR / "hyprland.conf"
    if lua.exists():
        return lua, "lua"
    return conf, "conf"


def hyprland_version() -> str:
    r = _run(["hyprctl", "version", "-j"])
    if r and r.returncode == 0:
        try:
            return json.loads(r.stdout).get("tag") or json.loads(r.stdout).get("version", "")
        except json.JSONDecodeError:
            pass
    return ""


def monitors() -> list[dict]:
    r = _run(["hyprctl", "monitors", "-j"])
    if not r or r.returncode != 0:
        return []
    try:
        return [m for m in json.loads(r.stdout) if not m.get("disabled")]
    except json.JSONDecodeError:
        return []


def max_monitor_width(default=2560) -> int:
    ms = monitors()
    return max((m.get("width", 0) for m in ms), default=default) or default


# --- image backend ---------------------------------------------------------

def image_backend() -> str:
    for cand in ("caelestia", "swww", "hyprpaper"):
        if which(cand):
            return cand
    return "none"


# --- gpu / encoders --------------------------------------------------------

def gpu_vendor() -> str:
    """'nvidia' | 'amd' | 'intel' | 'unknown' from sysfs (no lspci needed)."""
    vendors = {"0x10de": "nvidia", "0x1002": "amd", "0x8086": "intel"}
    found = set()
    for f in glob.glob("/sys/class/drm/card*/device/vendor"):
        try:
            found.add(vendors.get(Path(f).read_text().strip(), "unknown"))
        except OSError:
            pass
    for v in ("nvidia", "amd", "intel"):        # discrete first
        if v in found:
            return v
    return "unknown"


def vaapi_device() -> str:
    devs = sorted(glob.glob("/dev/dri/renderD*"))
    return devs[0] if devs else ""


def ffmpeg_encoders() -> set[str]:
    r = _run(["ffmpeg", "-hide_banner", "-encoders"])
    if not r or r.returncode != 0:
        return set()
    out = set()
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("V"):
            out.add(parts[1])
    return out


def probe_encoder(enc: str, vaapi_dev: str = "") -> bool:
    """Actually encode a few frames — presence in `-encoders` doesn't mean it works."""
    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-y"]
    vf = []
    if "vaapi" in enc:
        if not vaapi_dev:
            return False
        cmd += ["-vaapi_device", vaapi_dev]
        vf = ["-vf", "format=nv12,hwupload"]
    cmd += ["-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=0.3"]
    cmd += vf + ["-c:v", enc, "-f", "null", "-"]
    r = _run(cmd, timeout=30)
    return bool(r) and r.returncode == 0


def pick_encoder() -> tuple[str, str]:
    """(encoder, vaapi_device). 'none' if ffmpeg is missing."""
    if not which("ffmpeg"):
        return "none", ""
    have = ffmpeg_encoders()
    vendor = gpu_vendor()
    dev = vaapi_device()
    order = {
        "nvidia": ["hevc_nvenc", "h264_nvenc", "hevc_vaapi", "h264_vaapi"],
        "amd": ["hevc_vaapi", "h264_vaapi"],
        "intel": ["hevc_vaapi", "h264_vaapi", "hevc_qsv", "h264_qsv"],
        "unknown": ["hevc_vaapi", "h264_vaapi"],
    }[vendor] + ["libx265", "libx264"]
    for enc in order:
        if enc in have and probe_encoder(enc, dev):
            return enc, (dev if "vaapi" in enc else "")
    return "none", ""


# --- misc ------------------------------------------------------------------

def pictures_dir() -> Path:
    r = _run(["xdg-user-dir", "PICTURES"])
    if r and r.returncode == 0 and r.stdout.strip():
        return Path(r.stdout.strip())
    return paths.HOME / "Pictures"


def default_wallpaper_dir() -> Path:
    return pictures_dir() / "Wallpapers"


def summary() -> dict:
    cfg_file, flavor = hyprland_config()
    ms = monitors()
    return {
        "hyprland_running": hyprland_running(),
        "hyprland_version": hyprland_version(),
        "hypr_config": str(cfg_file),
        "hypr_flavor": flavor,
        "monitors": [f"{m['name']} {m['width']}x{m['height']}@{round(m.get('refreshRate', 0))}" for m in ms],
        "image_backend": image_backend(),
        "gpu": gpu_vendor(),
        "tools": {t: which(t) for t in ("mpvpaper", "ffmpeg", "ffprobe", "wallust", "caelestia", "hyprctl", "jq", "qs")},
        "pyside6": _has_pyside(),
    }


def _has_pyside() -> bool:
    try:
        import PySide6  # noqa: F401
        return True
    except ImportError:
        return False
