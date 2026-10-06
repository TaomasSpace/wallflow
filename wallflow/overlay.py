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
        "raise": _raise_count(),
    }
    paths.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # in place, not tmp+rename: the QML's file watcher follows this inode
    paths.OVERLAY_STATE.write_text(json.dumps(state, indent=2))
    export(state)
    return state


# --- for other shells -----------------------------------------------------------------------
# Anyone can draw the cutout above their own widgets: ~/.cache/wallflow/cutout.png always
# points at the current one (absent = nothing in front), cutout.json says how to fit it and
# changes on every switch - watch that file, not the link. contrib/WallflowCutout.qml is a
# drop-in for quickshell configs.

def export(state: dict) -> None:
    cut = state.get("cutout")
    info = {"cutout": cut, "link": str(paths.CUTOUT_LINK) if cut else None,
            "wallpaper": state.get("wallpaper"), "fill": state.get("fill", "crop")}
    try:
        old = json.loads(paths.CUTOUT_INFO.read_text())
    except (OSError, ValueError):
        old = {}
    if {k: old.get(k) for k in info} == info and (bool(cut) == paths.CUTOUT_LINK.is_symlink()):
        return                                            # unchanged: don't wake watchers
    tmp = paths.CUTOUT_LINK.with_name(".cutout.png.part")
    tmp.unlink(missing_ok=True)
    if cut:
        os.symlink(cut, tmp)
        os.replace(tmp, paths.CUTOUT_LINK)
    else:
        paths.CUTOUT_LINK.unlink(missing_ok=True)
    info["rev"] = int(old.get("rev", 0)) + 1
    paths.CUTOUT_INFO.write_text(json.dumps(info, indent=2))


def _raise_count() -> int:
    try:
        return int(paths.OVERLAY_RAISE.read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


def lift(cfg: dict) -> bool:
    """`wallflow overlay raise`: put the cutout back above everything on the bottom layer
    (e.g. from your own shell's startup, after it created its widgets)."""
    paths.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    paths.OVERLAY_RAISE.write_text(str(_raise_count() + 1))
    write_state(cfg)
    return running()


# --- what the cutout covers -------------------------------------------------------------------

_LEVELS = {"0": "background", "1": "bottom", "2": "top", "3": "overlay"}


def coverage() -> list[str] | None:
    """Per monitor: which other programs' layers are below the cutout (covered) and which are
    above it. None if hyprctl isn't available."""
    if not shutil.which("hyprctl"):
        return None
    try:
        r = subprocess.run(["hyprctl", "-j", "layers"], capture_output=True, text=True, timeout=3)
        mons = json.loads(r.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    ns = "wallflow-overlay"
    out = []
    for mon, data in mons.items():
        lv = data.get("levels", {})
        bottom = [l.get("namespace", "?") for l in lv.get("1", [])]
        if ns not in bottom:
            out.append(f"{mon}: overlay not on the bottom layer here (edit mode, outputs, or not running)")
            continue
        i = bottom.index(ns)
        below = [f"{n} (background)" for n in (l.get("namespace", "?") for l in lv.get("0", []))
                 if n not in ("wallflow-overlay", "mpvpaper")] + [f"{n} (bottom)" for n in bottom[:i]]
        above = bottom[i + 1:]
        line = f"{mon}: covers {', '.join(below) or 'only the wallpaper'}"
        if above:
            line += f" · ABOVE the cutout: {', '.join(above)} — `wallflow overlay raise`"
        out.append(line)
    return out


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
        f"raise    : {'on — stays above other programs’ bottom-layer widgets' if cfg['overlay'].get('raise', True) else 'off (overlay.raise)'}",
        *[f"layers   : {l}" if i == 0 else f"           {l}" for i, l in enumerate(coverage() or [])],
        f"state    : {paths.OVERLAY_STATE}",
        f"export   : {paths.CUTOUT_INFO} (+ {paths.CUTOUT_LINK.name}) — for other shells, see README",
    ])
