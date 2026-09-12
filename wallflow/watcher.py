"""`wallflow watch` — watches the wallpaper folder and re-runs `rename` on changes.

Started by `wallflow setup` (if rename.mode != 0) and via Hyprland autostart, same
pattern as pauser.py: single-instance lock, runs forever, re-reads the config on
every debounced batch so a mode/dir change takes effect without a restart of its
own (setup restarts it anyway to pick up wallpaper_dir / recursive changes).

Uses the raw inotify syscalls via ctypes — no extra dependency.
"""
import ctypes
import ctypes.util
import fcntl
import os
import struct
import sys
import threading
import time

from . import config, paths, rename

_libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)

IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CLOSE_WRITE = 0x00000008
IN_ISDIR = 0x40000000
IN_IGNORED = 0x00008000

WATCH_MASK = IN_CREATE | IN_DELETE | IN_MOVED_FROM | IN_MOVED_TO | IN_CLOSE_WRITE

_EVENT_HDR = struct.Struct("iIII")   # wd, mask, cookie, name_len


def _check(rc: int, what: str) -> int:
    if rc == -1:
        err = ctypes.get_errno()
        raise OSError(err, f"{what}: {os.strerror(err)}")
    return rc


def _inotify_init() -> int:
    return _check(_libc.inotify_init1(0), "inotify_init1")


def _add_watch(fd: int, path: str) -> int:
    return _check(_libc.inotify_add_watch(fd, os.fsencode(path), WATCH_MASK), f"inotify_add_watch {path}")


def _single_instance():
    paths.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(paths.WATCHER_LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(0)                     # already running
    return fh                            # keep the handle alive


def _subdirs(root) -> list:
    dirs = [str(root)]
    for d, ds, _ in os.walk(root):
        ds[:] = [x for x in ds if not x.startswith(".")]
        dirs += [os.path.join(d, x) for x in ds]
    return dirs


def _watch_tree(fd: int, cfg: dict) -> dict:
    """Add a watch on the wallpaper dir, and every subdir if recursive. wd -> path."""
    root = config.wallpaper_dir(cfg)
    root.mkdir(parents=True, exist_ok=True)
    dirs = _subdirs(root) if cfg["general"]["recursive"] else [str(root)]
    wds = {}
    for d in dirs:
        try:
            wds[_add_watch(fd, d)] = d
        except OSError:
            pass
    return wds


_debounce_timer = None
_debounce_lock = threading.Lock()


def _trigger() -> None:
    cfg = config.load()
    ops = rename.plan(cfg)
    if ops:
        rename.apply(ops)


def _schedule() -> None:
    global _debounce_timer
    with _debounce_lock:
        if _debounce_timer:
            _debounce_timer.cancel()
        _debounce_timer = threading.Timer(2.0, _trigger)
        _debounce_timer.daemon = True
        _debounce_timer.start()


def _is_relevant(name: str, cfg: dict) -> bool:
    return name.lower().endswith(config.all_exts(cfg))


def main() -> None:
    _lock = _single_instance()
    cfg = config.load()
    if cfg["rename"]["mode"] == 0:
        return
    fd = _inotify_init()
    wds = _watch_tree(fd, cfg)
    while True:
        data = os.read(fd, 64 * 1024)
        pos = 0
        cfg = config.load()               # cheap; picks up mode/recursive changes live
        while pos < len(data):
            wd, mask, cookie, name_len = _EVENT_HDR.unpack_from(data, pos)
            pos += _EVENT_HDR.size
            name = data[pos:pos + name_len].split(b"\0", 1)[0].decode(errors="replace")
            pos += name_len
            if mask & IN_IGNORED:
                wds.pop(wd, None)
                continue
            if mask & IN_ISDIR and mask & IN_CREATE and cfg["general"]["recursive"]:
                parent = wds.get(wd)
                if parent:
                    new_dir = os.path.join(parent, name)
                    try:
                        wds[_add_watch(fd, new_dir)] = new_dir
                    except OSError:
                        pass
                continue
            if name and _is_relevant(name, cfg):
                _schedule()
