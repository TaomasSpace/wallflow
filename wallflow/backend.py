"""Applying wallpapers. Headless-safe: no Qt imports here.

Images -> caelestia / swww / hyprpaper, then wallust for colours.
Videos -> mpvpaper with an mpv IPC socket. video->video is a live `loadfile`
          swap (no teardown, no flash). Colours come from a still frame.
          If a transcode exists it is played instead of the original; if not,
          the original plays now and a background transcode swaps in when done.
"""
import json
import os
import random
import socket
import subprocess
import sys
import time

from . import addons, config, paths, thumbs, transcode

_QUIET = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --- state -----------------------------------------------------------------

def read_current() -> str:
    try:
        return paths.STATE_FILE.read_text().strip()
    except OSError:
        return ""


def _save_state(path: str) -> None:
    try:
        paths.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        paths.STATE_FILE.write_text(path)
    except OSError:
        pass


def gather_wallpapers(cfg: dict) -> list[str]:
    root = config.wallpaper_dir(cfg)
    exts = config.all_exts(cfg)
    files = []
    if cfg["general"]["recursive"]:
        for d, dirs, fs in os.walk(root):
            dirs[:] = [x for x in dirs if not x.startswith(".")]
            files += [os.path.join(d, f) for f in fs if f.lower().endswith(exts)]
    elif root.is_dir():
        files = [str(p) for p in root.iterdir() if p.is_file() and p.name.lower().endswith(exts)]
    return sorted(files, key=str.lower)


# --- theming ---------------------------------------------------------------

def theme(src: str, cfg: dict) -> None:
    if not cfg["theme"]["enabled"] or not _has("wallust"):
        return
    # No -s: wallust must broadcast the colour sequences so open terminals recolour live.
    # Only its stdout is discarded (it otherwise leaks as a notification).
    subprocess.run(["wallust", "run", *cfg["theme"]["wallust_args"], src], check=False, **_QUIET)
    for cmd in addons.reload_commands():
        subprocess.run(cmd, shell=True, check=False, **_QUIET)


def theme_source(path: str, cfg: dict) -> str:
    """wallust can't read video — theme off the cached still frame."""
    return thumbs.thumb_for(path, cfg) if config.is_video(cfg, path) else path


# --- mpv / mpvpaper --------------------------------------------------------

def mpv_command(*cmd) -> bool:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(str(paths.MPV_SOCKET))
        s.sendall((json.dumps({"command": list(cmd)}) + "\n").encode())
        s.close()
        return True
    except OSError:
        return False


def mpvpaper_running() -> bool:
    return subprocess.run(["pgrep", "-x", "mpvpaper"], **_QUIET).returncode == 0


def kill_mpvpaper() -> None:
    subprocess.run(["pkill", "-x", "mpvpaper"], check=False, **_QUIET)
    try:
        os.unlink(paths.MPV_SOCKET)
    except OSError:
        pass


def launch_mpvpaper(path: str, cfg: dict) -> None:
    kill_mpvpaper()
    opts = f"{cfg['video']['mpv_opts']} input-ipc-server={paths.MPV_SOCKET}"
    subprocess.Popen(["mpvpaper", "-o", opts, cfg["video"]["outputs"], path],
                     start_new_session=True, **_QUIET)


def play_video(path: str, cfg: dict) -> None:
    if not (mpvpaper_running() and mpv_command("loadfile", path)):
        launch_mpvpaper(path, cfg)


# --- image backends --------------------------------------------------------

def set_image(path: str, cfg: dict) -> None:
    be = cfg["image"]["backend"]
    if be == "caelestia":
        subprocess.run(["caelestia", "wallpaper", "-f", path], check=False, **_QUIET)
    elif be == "swww":
        if subprocess.run(["swww", "query"], **_QUIET).returncode != 0:
            subprocess.Popen(["swww-daemon"], start_new_session=True, **_QUIET)
        subprocess.run(["swww", "img", path, "--transition-type", "fade"], check=False, **_QUIET)
    elif be == "hyprpaper":
        subprocess.run(["hyprctl", "hyprpaper", "preload", path], check=False, **_QUIET)
        subprocess.run(["hyprctl", "hyprpaper", "wallpaper", f",{path}"], check=False, **_QUIET)
    else:
        print("no image backend configured (image.backend)", file=sys.stderr)


# --- public ----------------------------------------------------------------

def _spawn_background_transcode(path: str) -> None:
    exe = [sys.executable, str(paths.REPO_DIR / "wallflow.py")]
    subprocess.Popen([*exe, "transcode", "--file", path, "--swap"],
                     start_new_session=True, **_QUIET)


def apply(path: str, cfg: dict | None = None, do_theme: bool = True) -> None:
    cfg = cfg or config.load()
    if path.startswith("file://"):
        from urllib.parse import unquote, urlparse
        path = unquote(urlparse(path).path)
    path = os.path.abspath(path)
    _save_state(path)   # FIRST: caelestia re-sources hyprland.lua -> `restore` runs mid-apply
    if config.is_video(cfg, path):
        if do_theme:
            theme(theme_source(path, cfg), cfg)
        src = transcode.existing(path, cfg) or path
        play_video(src, cfg)
        if src == path and transcode.wanted(path, cfg):
            _spawn_background_transcode(path)
    else:
        kill_mpvpaper()
        set_image(path, cfg)
        if do_theme:
            theme(path, cfg)
    _save_state(path)


_BACKEND_LAYERS = {"caelestia": "caelestia-background", "swww": "swww-daemon", "hyprpaper": "hyprpaper"}


def _layer_present(namespace: str) -> bool:
    r = subprocess.run(["hyprctl", "layers"], capture_output=True, text=True)
    return r.returncode == 0 and f"namespace: {namespace}" in r.stdout


def _wait_for_desktop(cfg: dict) -> None:
    """Block until outputs exist and the image backend's background layer is up,
    so mpvpaper's layer is created on top of it (login race)."""
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return
    ns = cfg["video"]["wait_for_layer"]
    if ns == "auto":
        ns = _BACKEND_LAYERS.get(cfg["image"]["backend"], "")
    deadline = time.monotonic() + float(cfg["video"]["restore_timeout"])
    while time.monotonic() < deadline:
        r = subprocess.run(["hyprctl", "monitors", "-j"], capture_output=True, text=True)
        have_outputs = r.returncode == 0 and r.stdout.strip() not in ("", "[]")
        if have_outputs and (not ns or _layer_present(ns)):
            break
        time.sleep(0.5)
    time.sleep(float(cfg["video"]["restore_delay"]))


def restore(cfg: dict | None = None) -> None:
    """Re-apply the last wallpaper. Runs at login, after every Hyprland reload
    (caelestia triggers one per image change) and after resume — so it must be
    idempotent: never spawn a second mpvpaper, always kill a stale one."""
    cfg = cfg or config.load()
    path = read_current()
    if not path or not os.path.exists(path):
        return
    if config.is_video(cfg, path):
        src = transcode.existing(path, cfg) or path
        if mpvpaper_running() and mpv_command("loadfile", src):
            return                                   # already up — just make sure it's this file
        _wait_for_desktop(cfg)                       # login: don't get buried under the bg layer
        launch_mpvpaper(src, cfg)
    else:
        kill_mpvpaper()                              # a video layer must never outlive an image
        if cfg["image"]["backend"] != "caelestia":   # caelestia restores its own
            set_image(path, cfg)


def reattach(cfg: dict | None = None) -> None:
    """After resume/hotplug mpvpaper's layer on a re-added output stays dead
    (alpha 0). Only videos need this: kill + relaunch so it binds all outputs."""
    cfg = cfg or config.load()
    path = read_current()
    if path and os.path.exists(path) and config.is_video(cfg, path):
        launch_mpvpaper(transcode.existing(path, cfg) or path, cfg)


def swap_if_current(original: str, transcoded: str) -> None:
    """Called by the background transcode: hot-swap only if still the active wallpaper."""
    if read_current() == original:
        mpv_command("loadfile", transcoded)


def step(cfg: dict, delta: int = 1) -> str | None:
    files = gather_wallpapers(cfg)
    if not files:
        return None
    cur = read_current()
    i = files.index(cur) if cur in files else -1
    nxt = files[(i + delta) % len(files)]
    apply(nxt, cfg)
    return nxt


def pick_random(cfg: dict) -> str | None:
    files = [f for f in gather_wallpapers(cfg) if f != read_current()]
    if not files:
        return None
    f = random.choice(files)
    apply(f, cfg)
    return f


def _has(name: str) -> bool:
    from shutil import which
    return which(name) is not None
