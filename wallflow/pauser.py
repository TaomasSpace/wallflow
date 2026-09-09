"""Pause mpvpaper while a window is fullscreen (games, videos) — saves a decode loop.
Also re-attaches mpvpaper when an output comes back (resume from sleep, hotplug).

Listens on Hyprland's socket2 (a separate process, never a bind: shelling out
from a bind blocks the compositor). Reconnects if Hyprland restarts.
"""
import fcntl
import json
import os
import socket
import subprocess
import sys
import threading
import time

from . import backend, paths


def _socket2_path() -> str:
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not sig:
        sys.exit("wallflow pauser: not inside a Hyprland session")
    for base in (paths.RUNTIME_DIR / "hypr", "/tmp/hypr"):
        p = os.path.join(base, sig, ".socket2.sock")
        if os.path.exists(p):
            return p
    sys.exit("wallflow pauser: Hyprland socket2 not found")


def _initial_state() -> bool:
    try:
        r = subprocess.run(["hyprctl", "activewindow", "-j"], capture_output=True, text=True, timeout=3)
        return bool(json.loads(r.stdout).get("fullscreen"))
    except Exception:
        return False


def _single_instance():
    paths.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(paths.PAUSER_LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(0)                     # already running
    return fh                            # keep the handle alive


_reattach_timer = None


def _schedule_reattach() -> None:
    """Debounce: both outputs come back within a second of each other."""
    global _reattach_timer
    if _reattach_timer:
        _reattach_timer.cancel()
    _reattach_timer = threading.Timer(1.5, backend.reattach)
    _reattach_timer.daemon = True
    _reattach_timer.start()


def main() -> None:
    _lock = _single_instance()
    path = _socket2_path()
    paused = None
    while True:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(path)
            state = _initial_state()
            if state != paused:
                backend.mpv_command("set_property", "pause", state)
                paused = state
            buf = ""
            for chunk in iter(lambda: s.recv(4096).decode(errors="replace"), ""):
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    if line.startswith("fullscreen>>"):
                        state = line.split(">>", 1)[1].strip() == "1"
                        backend.mpv_command("set_property", "pause", state)
                        paused = state
                    elif line.startswith("monitoradded"):     # monitoradded / monitoraddedv2
                        _schedule_reattach()
        except OSError:
            pass
        time.sleep(2)                   # Hyprland gone / restarting — retry
        if not os.path.exists(path):
            sys.exit(0)
