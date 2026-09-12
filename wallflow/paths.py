"""All filesystem locations wallflow touches, in one place (XDG-aware)."""
import os
from pathlib import Path

HOME = Path.home()

def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or (HOME / default))

CONFIG_HOME = _xdg("XDG_CONFIG_HOME", ".config")
CACHE_HOME = _xdg("XDG_CACHE_HOME", ".cache")
DATA_HOME = _xdg("XDG_DATA_HOME", ".local/share")
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")

# --- wallflow's own state -------------------------------------------------
CONFIG_DIR = CONFIG_HOME / "wallflow"
CONFIG_FILE = CONFIG_DIR / "config.toml"
ADDON_STATE_DIR = CONFIG_DIR / "addons"          # one <name>.json per installed addon

CACHE_DIR = CACHE_HOME / "wallflow"
STATE_FILE = CACHE_DIR / "current"               # path of the last applied wallpaper
THUMB_DIR = CACHE_DIR / "thumbs"
TRANSCODE_DIR = CACHE_DIR / "transcoded"
PAUSER_LOCK = CACHE_DIR / "pauser.lock"
WATCHER_LOCK = CACHE_DIR / "watcher.lock"

MPV_SOCKET = RUNTIME_DIR / "wallflow-mpv.sock"   # mpv IPC (hot-swap / pause)

# --- where the code lives --------------------------------------------------
PKG_DIR = Path(__file__).resolve().parent
REPO_DIR = PKG_DIR.parent
ADDONS_DIR = REPO_DIR / "addons"
INSTALL_DIR = DATA_HOME / "wallflow"
LAUNCHER = HOME / ".local" / "bin" / "wallflow"

# --- third parties ---------------------------------------------------------
HYPR_DIR = CONFIG_HOME / "hypr"
WALLUST_DIR = CONFIG_HOME / "wallust"
WALLUST_CONF = WALLUST_DIR / "wallust.toml"
WALLUST_TEMPLATES = WALLUST_DIR / "templates"


def launcher_cmd() -> str:
    """Absolute command used in Hyprland config (PATH isn't guaranteed there)."""
    return str(LAUNCHER) if LAUNCHER.exists() else "wallflow"


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, ADDON_STATE_DIR, CACHE_DIR, THUMB_DIR, TRANSCODE_DIR):
        d.mkdir(parents=True, exist_ok=True)
