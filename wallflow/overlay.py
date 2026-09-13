"""The depth overlay: a click-through layer-shell window on the `bottom` layer
(above mpvpaper / the image backend on `background`, below every real window)
that draws installed widget addons and, on top of them, the current wallpaper's
subject cutout (depth.py). Rendered by quickshell (`qs -p templates/overlay.qml`)
— it speaks wlr-layer-shell natively; PySide6 has no bindings for it.

State flows one way: Python writes ~/.cache/wallflow/overlay.json (cutout path,
widget list, the [overlay] config section); the QML watches that file and
hot-reloads. No IPC, no restarts on wallpaper change.

Edit mode (`wallflow overlay edit`): the window jumps to the `top` layer and
accepts input, widgets become draggable; on drop the QML runs
`wallflow config set overlay.<k> <v> …` (exe path comes from the state file),
which rewrites the state and everything settles. `wallflow overlay done` / Esc
ends it.
"""
import json
import os
import shutil
import subprocess
import sys

from . import addons, config, depth, paths

_QUIET = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def qs_bin() -> str | None:
    return shutil.which("qs") or shutil.which("quickshell")


def widgets() -> list[dict]:
    out = []
    for name, st in sorted(addons.installed().items()):
        if st.get("widget") and os.path.exists(st["widget"]):
            out.append({"name": name, "qml": "file://" + st["widget"]})
    return out


def write_state(cfg: dict | None = None, current: str | None = None) -> dict:
    """Rewrite overlay.json from the current wallpaper + config. Cheap; call freely."""
    from . import backend
    cfg = cfg or config.load()
    cur = current or backend.read_current()
    state = {
        "exe": [sys.executable, str(paths.REPO_DIR / "wallflow.py")],
        "edit": paths.OVERLAY_EDIT.exists(),
        "wallpaper": cur,
        "cutout": depth.existing(cur, cfg) if cur and os.path.exists(cur) else None,
        "widgets": widgets(),
        "outputs": cfg["overlay"]["outputs"],
        "fill": cfg["overlay"]["fill"],
        "config": cfg["overlay"],
    }
    paths.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # in place, not tmp+rename: the QML's file watcher follows this inode
    paths.OVERLAY_STATE.write_text(json.dumps(state, indent=2))
    return state


def _pid() -> int:
    try:
        return int(paths.OVERLAY_PID.read_text().strip())
    except (OSError, ValueError):
        return 0


def running() -> bool:
    pid = _pid()
    if not pid:
        return False
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b"overlay.qml" in f.read()
    except OSError:
        return False


def useful(cfg: dict) -> bool:
    """Nothing to draw = don't start a process for it."""
    return bool(widgets()) or depth.ready()


def start(cfg: dict | None = None, log=print) -> bool:
    """Idempotent: (re)write the state and make sure one overlay process runs."""
    cfg = cfg or config.load()
    if not cfg["overlay"]["enabled"]:
        return False
    write_state(cfg)
    if running():
        return True
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and not os.environ.get("WAYLAND_DISPLAY"):
        return False
    qs = qs_bin()
    if not qs:
        log("overlay needs quickshell (`qs`) — install it, then `wallflow overlay start`")
        return False
    if not useful(cfg):
        log("nothing to overlay yet: `wallflow depth setup` and/or `wallflow addons install clock`")
        return False
    env = dict(os.environ, WALLFLOW_OVERLAY_STATE=str(paths.OVERLAY_STATE))
    p = subprocess.Popen([qs, "-p", str(paths.OVERLAY_QML)], env=env,
                         start_new_session=True, **_QUIET)
    paths.OVERLAY_PID.write_text(str(p.pid))
    return True


def stop() -> bool:
    pid = _pid()
    was = running()
    if was:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    paths.OVERLAY_PID.unlink(missing_ok=True)
    return was


def edit(cfg: dict, on: bool | None = None) -> bool:
    """Toggle (or set) edit mode. Returns the new state."""
    cur = paths.OVERLAY_EDIT.exists()
    new = (not cur) if on is None else on
    if new:
        paths.OVERLAY_EDIT.touch()
    else:
        paths.OVERLAY_EDIT.unlink(missing_ok=True)
    start(cfg, log=lambda *_: None)
    return new


def restart(cfg: dict | None = None, log=print) -> bool:
    stop()
    return start(cfg, log)


def ensure(cfg: dict) -> None:
    """From apply()/restore(): keep the state fresh, revive the process if it died."""
    try:
        if cfg["overlay"]["enabled"]:
            start(cfg, log=lambda *_: None)
        else:
            write_state(cfg)
    except Exception as e:                       # never let the overlay break a wallpaper change
        print(f"overlay: {e}", file=sys.stderr)


def status_text(cfg: dict) -> str:
    st = write_state(cfg)
    return "\n".join([
        f"enabled  : {cfg['overlay']['enabled']}",
        f"running  : {running()} (pid {_pid() or '-'}){' — EDIT MODE' if st['edit'] else ''}",
        f"quickshell: {qs_bin() or 'not found'}",
        f"depth    : {'ready' if depth.ready() else 'not set up (wallflow depth setup)'}",
        f"cutout   : {st['cutout'] or '-'}",
        f"widgets  : {', '.join(w['name'] for w in st['widgets']) or '-'}",
        f"state    : {paths.OVERLAY_STATE}",
    ])
