"""Hand-picked 3D layer - `wallflow depth edit`, the wallflow side.

You point, SAM 2.1 segments what you pointed at (pick_worker.py, inside the depth
venv). The editor (editor.py) only draws; the mask lives in the worker. "Apply"
renders it into a cutout and stores it as a *manual* selection that takes
precedence over the automatic cutout (depth.existing() asks here first).
`wallflow depth edit reset` drops it again.

Files:
  ~/.cache/wallflow/cutouts/<mkey>.manual.json      the saved selection for this image
  ~/.cache/wallflow/cutouts/<mkey>.manual-<h>.png   its cutout (name changes per save, so
                                                    the overlay's Image reloads)
  ~/.local/share/wallflow-masks/<content>.png       the mask itself = training data for a
  ~/.local/share/wallflow-masks/<content>.json      future "learn my taste" model; keyed by
                                                    file CONTENT, so renames don't lose it
mkey = image path + mtime (like every other cutout key).
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import paths

WORKER = paths.PKG_DIR / "pick_worker.py"
DEFAULT_MODEL = "facebook/sam2.1-hiera-large"


def _sha(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()


def content_hash(src: str) -> str:
    """Same key depthfg used (sha1 of the bytes, 16 hex), so its masks import 1:1."""
    h = hashlib.sha1()
    with open(src, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def edit_model(cfg: dict) -> str:
    return str(cfg["depth"].get("edit_model") or DEFAULT_MODEL)


def editor_ready() -> bool:
    """SAM 2.1 installed in the depth venv (`wallflow depth setup` does it)."""
    return any(paths.DEPTH_VENV.glob("lib/python*/site-packages/sam2/__init__.py"))


# --- keys + paths -------------------------------------------------------------------------

def mkey(src: str) -> str:
    """Hand-picks are keyed by file content (ident.py): renaming keeps them."""
    from . import ident
    return _sha(f"{ident.file_id(src)}:manual")


def _legacy_mkey(src: str) -> str:
    st = os.stat(src)
    return _sha(f"{os.path.abspath(src)}:{st.st_mtime_ns}:manual")


def mask_path(src: str) -> Path:
    return paths.MASK_DIR / f"{content_hash(src)}.png"


def view_path() -> Path:
    return paths.CACHE_DIR / "edit-view.png"


def masks_saved() -> int:
    return len(list(paths.MASK_DIR.glob("*.png"))) if paths.MASK_DIR.is_dir() else 0


# --- saved (manual) selection ---------------------------------------------------------------

def _manual_json(src: str) -> Path:
    from . import ident
    return ident.migrate(paths.CUTOUT_DIR / f"{mkey(src)}.manual.json",
                         paths.CUTOUT_DIR / f"{_legacy_mkey(src)}.manual.json")


def manual_state(src: str) -> dict | None:
    """The saved selection for this image, or None (= automatic cutout)."""
    try:
        return json.loads(_manual_json(src).read_text())
    except (OSError, ValueError):
        return None


def has_manual(src: str) -> bool:
    try:
        return _manual_json(src).exists()
    except OSError:
        return False


def manual_cutout(src: str) -> str | None:
    """Cutout of the saved selection; None if nothing was selected (= no 3D layer)."""
    st = manual_state(src)
    if not st or not st.get("cutout"):
        return None
    p = paths.CUTOUT_DIR / st["cutout"]
    return str(p) if p.exists() and p.stat().st_size > 0 else None


def new_cutout_path(src: str) -> Path:
    paths.CUTOUT_DIR.mkdir(parents=True, exist_ok=True)
    h = _sha(f"{mkey(src)}:{time.time_ns()}")[:10]
    return paths.CUTOUT_DIR / f"{mkey(src)}.manual-{h}.png"


def save_manual(src: str, cutout: str | None, coverage: float = 0.0) -> None:
    paths.CUTOUT_DIR.mkdir(parents=True, exist_ok=True)
    mk = mkey(src)
    keep = os.path.basename(cutout) if cutout else None
    prev = (manual_state(src) or {}).get("cutout")
    if prev and prev != keep:                  # e.g. one still named after the old path key
        (paths.CUTOUT_DIR / prev).unlink(missing_ok=True)
    for p in paths.CUTOUT_DIR.glob(f"{mk}.manual-*.png"):
        if p.name != keep:
            p.unlink(missing_ok=True)
    state = {"src": os.path.abspath(src), "cutout": keep, "coverage": round(float(coverage), 4),
             "time": int(time.time())}
    tmp = _manual_json(src).with_suffix(".part")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(_manual_json(src))


def reset(src: str) -> bool:
    """Back to automatic. The training mask is kept (it's your work, not a cache)."""
    mk = mkey(src)
    had = _manual_json(src).exists()
    _manual_json(src).unlink(missing_ok=True)
    for p in paths.CUTOUT_DIR.glob(f"{mk}.manual-*.png"):
        p.unlink(missing_ok=True)
    return had


def keep_names(src: str, cfg: dict) -> set[str]:
    """For depth.prune(): everything that still belongs to this image."""
    names = {f"{mkey(src)}.manual.json"}
    c = (manual_state(src) or {}).get("cutout")
    if c:
        names.add(c)
    return names


def status_line(src: str, cfg: dict) -> str:
    st = manual_state(src)
    if st is None:
        return "automatic"
    if not st.get("cutout"):
        return "hand-picked: nothing in front"
    return f"hand-picked ({st.get('coverage', 0) * 100:.0f}% of the image in front)"


def refresh_overlay(src: str, cfg: dict) -> None:
    """After a save/reset: show it right away if this is the current wallpaper."""
    from . import backend, overlay
    if backend.read_current() == os.path.abspath(src):
        overlay.ensure(cfg)


# --- worker --------------------------------------------------------------------------------------

def serve_cmd(cfg: dict) -> list[str]:
    from . import depth
    return [str(depth.venv_python()), str(WORKER), "serve", json.dumps({"model": edit_model(cfg)})]


def open_args(src: str, cfg: dict, w: int, h: int) -> dict:
    """Everything the worker may start from: your saved mask, a saved hand-pick, the
    automatic cutout, the character mask - it takes the first that exists."""
    from . import depth
    src = os.path.abspath(src)
    paths.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        auto = depth.target(src, cfg)
        auto = str(auto) if auto.exists() and auto.stat().st_size > 0 else ""
    except OSError:
        auto = ""
    try:
        subject = depth.subject_path(src, cfg)
        subject = str(subject) if subject.exists() else ""
    except OSError:
        subject = ""
    from . import learn
    ch = content_hash(src)
    return {"src": src, "view_w": int(w), "view_h": int(h), "view_out": str(view_path()),
            "mask": str(paths.MASK_DIR / f"{ch}.png"), "manual": manual_cutout(src) or "", "auto": auto,
            "subject": subject, "learned": learn.model_arg(cfg), **learn.depth_args(src, cfg, ch)}


def apply_args(src: str, out: Path) -> dict:
    paths.MASK_DIR.mkdir(parents=True, exist_ok=True)
    return {"out": str(out), "mask_out": str(mask_path(src))}


def launch_editor(src: str) -> None:
    """Detached `wallflow depth edit <src>` (the picker's E key)."""
    exe = [sys.executable, str(paths.REPO_DIR / "wallflow.py")]
    subprocess.Popen([*exe, "depth", "edit", os.path.abspath(src)], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --- import (masks made with the standalone depthfg tool, or a backup) -----------------------------

def import_masks(folder: str, cfg: dict, log=print) -> int:
    """Copy <content-hash>.png masks into the mask store and turn every one that matches a
    wallpaper into its hand-picked cutout. No SAM needed - it only renders."""
    from . import depth
    from .backend import gather_wallpapers
    src_dir = Path(os.path.expanduser(folder))
    if not src_dir.is_dir():
        log(f"no such folder: {src_dir}")
        return 1
    if not depth.ready():
        log("depth is not set up - run `wallflow depth setup`")
        return 1
    have = {p.stem: p for p in src_dir.glob("*.png")}
    if not have:
        log(f"no masks (*.png) in {src_dir}")
        return 1
    paths.MASK_DIR.mkdir(parents=True, exist_ok=True)
    jobs, names = [], []
    for f in gather_wallpapers(cfg, include_hidden=True):
        if not depth.is_image(f, cfg):
            continue
        h = content_hash(f)
        if h not in have:
            continue
        dst = paths.MASK_DIR / f"{h}.png"
        if not dst.exists():
            shutil.copyfile(have[h], dst)
            (paths.MASK_DIR / f"{h}.json").write_text(json.dumps({"src": f, "imported": str(have[h])}))
        out = new_cutout_path(f)
        jobs.append({"src": f, "mask": str(dst), "out": str(out)})
        names.append(f)
    if not jobs:
        log(f"{len(have)} mask(s) in {src_dir}, none matches a wallpaper in {cfg['general']['wallpaper_dir']}")
        return 1
    log(f"{len(jobs)} of {len(have)} mask(s) match a wallpaper - rendering their cutouts ...")
    from . import depth as _d
    p = subprocess.Popen([str(_d.venv_python()), str(WORKER), "render", json.dumps({"jobs": jobs})],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    ok = 0
    for line in p.stdout:
        line = line.rstrip()
        if line.startswith("item: "):
            r = json.loads(line[6:])
            f, j = names[r["i"] - 1], jobs[r["i"] - 1]
            if r.get("ok"):
                ok += 1
                save_manual(f, None if r.get("empty") else j["out"], r.get("coverage", 0.0))
                log(f"  {os.path.basename(f)}: {'nothing in front' if r.get('empty') else 'ok'}")
            else:
                Path(j["out"]).unlink(missing_ok=True)
                log(f"  {os.path.basename(f)}: FAILED {r.get('error')}")
        elif line.startswith("error: "):
            log(line[7:])
    p.wait()
    from . import backend
    cur = backend.read_current()
    if cur in names:
        refresh_overlay(cur, cfg)
    log(f"imported {ok} - they're hand-picked now; `wallflow depth edit <file>` to adjust one")
    from . import learn
    if ok and learn.due(cfg):
        log("enough masks to learn from: `wallflow depth train` (or it starts by itself after your next save)")
    return 0 if ok else 1
