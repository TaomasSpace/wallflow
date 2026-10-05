"""Learn your taste - a model trained on your `wallflow depth edit` masks.

A small head on top of SAM 2.1's image features + the depth map predicts what you
would put in front (pick_worker.py: train / auto / the editor's starting guess).
SAM already knows what things are, so it starts learning from a few dozen masks,
and training takes about a minute on a GPU.

  wallflow depth train            train now (reports accuracy on held-out wallpapers)
  depth.train_every = 10          retrain in the background after every N new masks
  depth.mode = "learned"          automatic cutouts come from it (falls back to "auto"
                                  until a model exists)

Files: ~/.local/share/wallflow-masks/learned.pt (+ learned.json), next to the masks
it learned from. Depth maps it computes are cached in ~/.cache/wallflow/learn/.
"""
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import paths

MODEL = paths.MASK_DIR / "learned.pt"
META = paths.MASK_DIR / "learned.json"
LOCK = paths.CACHE_DIR / "learn.lock"
LOG = paths.CACHE_DIR / "learn.log"
HASHES = paths.CACHE_DIR / "hashes.json"
DEPTH_DIR = paths.CACHE_DIR / "learn"


def info() -> dict:
    try:
        return json.loads(META.read_text())
    except (OSError, ValueError):
        return {}


def ready(cfg: dict) -> bool:
    """A model exists and was trained on the features of the configured SAM model."""
    from . import objects
    return MODEL.exists() and info().get("model") == objects.edit_model(cfg)


def stamp() -> str:
    try:
        return str(MODEL.stat().st_mtime_ns)
    except OSError:
        return "none"


def model_arg(cfg: dict) -> str:
    return str(MODEL) if ready(cfg) else ""


def depth_args(src: str, cfg: dict, h: str | None = None) -> dict:
    from . import depth, objects
    h = h or objects.content_hash(src)
    try:
        full = str(depth.depthmap_path(src, cfg))
    except OSError:
        full = ""
    return {"depth_full": full, "depth_cache": str(DEPTH_DIR / f"{h}.depth.npy"),
            "depth_model": depth.settings(src, cfg)["depth_model"]}


def training() -> bool:
    """True while a training run holds the lock."""
    paths.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh, fcntl.LOCK_UN)
            return False
        except OSError:
            return True


# --- which wallpaper belongs to which mask ---------------------------------------------------
# Masks are keyed by file content; wallpapers get renamed (rename.mode), so the current path
# is found by hashing the folder. Hashes are cached by path + size + mtime.

def _hash_index(cfg: dict) -> dict[str, str]:
    from . import depth, objects
    from .backend import gather_wallpapers
    try:
        cache = json.loads(HASHES.read_text())
    except (OSError, ValueError):
        cache = {}
    out, fresh = {}, {}
    for f in gather_wallpapers(cfg, include_hidden=True):
        if not depth.is_image(f, cfg):
            continue
        try:
            st = os.stat(f)
        except OSError:
            continue
        key = f"{f}:{st.st_size}:{st.st_mtime_ns}"
        h = cache.get(key) or objects.content_hash(f)
        fresh[key] = h
        out[h] = f
    paths.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    HASHES.write_text(json.dumps(fresh))
    return out


def jobs(cfg: dict) -> list[dict]:
    index = _hash_index(cfg)
    out = []
    for m in sorted(paths.MASK_DIR.glob("*.png")) if paths.MASK_DIR.is_dir() else []:
        src = index.get(m.stem)
        if not src:                                  # not in the folder any more? try where it was
            try:
                old = json.loads(m.with_suffix(".json").read_text()).get("src", "")
            except (OSError, ValueError):
                old = ""
            src = old if old and os.path.exists(old) else None
        if src:
            out.append({"src": src, "mask": str(m), **depth_args(src, cfg, m.stem)})
    return out


# --- training ----------------------------------------------------------------------------------

def _stream(cmd: list[str], log) -> tuple[int, dict]:
    """Run a worker; status lines overwrite each other on a terminal. Returns (rc, done-payload)."""
    tty = log is print and sys.stdout.isatty()
    res, tail, open_line = {}, [], False
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    try:
        for line in p.stdout:
            line = line.rstrip()
            if line.startswith("status: "):
                if tty:
                    sys.stdout.write("\r\033[K  " + line[8:][:120])
                    sys.stdout.flush()
                    open_line = True
                elif "step" not in line or line.endswith("loss") or "step 1/" in line:
                    log("  " + line[8:])
                continue
            if open_line:
                sys.stdout.write("\r\033[K")
                open_line = False
            if line.startswith("done: "):
                try:
                    res = json.loads(line[6:])
                except ValueError:
                    pass
            elif line.startswith("error: "):
                log(line[7:])
            elif line.startswith("("):
                log("  " + line)
            elif line:
                tail = (tail + [line])[-20:]
        rc = p.wait()
    except KeyboardInterrupt:
        p.terminate()
        p.wait()
        raise
    if open_line:
        sys.stdout.write("\r\033[K")
    if rc != 0 and tail:
        log("\n".join(tail))
    return rc, res


def train(cfg: dict, log=print, notify: bool = False) -> int:
    from . import depth, objects
    if not depth.ready() or not objects.editor_ready():
        log("the editor's packages are missing - run `wallflow depth setup` (with --gpu on NVIDIA)")
        return 1
    paths.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK, "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("a training run is already going")
        return 1
    try:
        todo = jobs(cfg)
        n_masks = objects.masks_saved()
        if len(todo) < 3:
            log(f"{len(todo)} usable mask(s) - save at least 3 in `wallflow depth edit` first")
            return 1
        log(f"training on {len(todo)} wallpaper(s) ({n_masks} mask(s) saved) with "
            f"{objects.edit_model(cfg).split('/')[-1]} …")
        if notify:
            depth.notify("Learning your 3D taste", f"training on {len(todo)} wallpapers")
        args = {"jobs": todo, "model": objects.edit_model(cfg), "out": str(MODEL)}
        rc, res = _stream([str(depth.venv_python()), str(objects.WORKER), "train", json.dumps(args)], log)
        if rc != 0 or not res:
            if notify:
                depth.notify("Training failed", f"see {LOG}" if log is not print else "", "normal")
            return 1
        meta = {"model": objects.edit_model(cfg), "n_masks": n_masks, "images": res.get("images"),
                "val_iou": res.get("val_iou"), "seconds": res.get("seconds"), "device": res.get("device"),
                "time": int(time.time())}
        META.write_text(json.dumps(meta, indent=1))
        acc = (f"matches your masks {res['val_iou'] * 100:.0f}% (IoU) on wallpapers it hadn't seen"
               if res.get("val_iou") is not None else "(10+ masks give an accuracy estimate)")
        log(f"done in {res.get('seconds')} s on {res.get('device')}: {acc}")
        if cfg["depth"]["mode"] != "learned":
            log("  it now starts the editor's guesses; for automatic cutouts too:\n"
                "  wallflow config set depth.mode learned")
        if notify:
            depth.notify("3D taste learned", f"{res.get('images')} wallpapers · {acc}")
        _refresh_current(cfg)
        return 0
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _refresh_current(cfg: dict) -> None:
    """depth.mode learned: the current wallpaper's cutout is re-made with the new model."""
    from . import backend, depth, overlay
    if cfg["depth"]["mode"] != "learned":
        return
    cur = backend.read_current()
    if cur and os.path.exists(cur) and depth.wanted(cur, cfg):
        backend._spawn_background_cutout(cur)
    overlay.ensure(cfg)


def due(cfg: dict) -> bool:
    try:
        every = int(cfg["depth"].get("train_every", 10))
    except (TypeError, ValueError):
        return False
    if every <= 0:
        return False
    from . import objects
    n = objects.masks_saved()
    last = int(info().get("n_masks", 0)) if ready(cfg) else 0
    return n >= 3 and n - last >= every


def maybe_retrain(cfg: dict) -> bool:
    """After a save: start a background training run when depth.train_every new masks piled up."""
    if not due(cfg) or training():
        return False
    paths.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG, "w") as fh:
        subprocess.Popen([sys.executable, str(paths.REPO_DIR / "wallflow.py"), "depth", "train", "--background"],
                         start_new_session=True, stdout=fh, stderr=subprocess.STDOUT)
    return True


def auto_cmd(src: str, out: str, cfg: dict) -> list[str]:
    """depth.mode learned: the automatic cutout comes from the learned model."""
    from . import depth, objects
    args = {"src": os.path.abspath(src), "out": out, "model": objects.edit_model(cfg),
            "learned": model_arg(cfg), **depth_args(src, cfg)}
    return [str(depth.venv_python()), str(objects.WORKER), "auto", json.dumps(args)]


def status_line(cfg: dict) -> str:
    from . import objects
    i = info()
    every = cfg["depth"].get("train_every", 10)
    auto = f"retrains every {every} new masks" if every else "retrains only by hand (train_every = 0)"
    if not MODEL.exists():
        n = objects.masks_saved()
        return (f"not trained yet ({n} mask(s) saved) — `wallflow depth train`"
                + (f", or it starts by itself at {every}" if every and n < every else ""))
    if not ready(cfg):
        return "trained for another edit_model — `wallflow depth train` again"
    acc = f", {i['val_iou'] * 100:.0f}% on unseen wallpapers" if i.get("val_iou") is not None else ""
    new = objects.masks_saved() - int(i.get("n_masks", 0))
    return (f"trained on {i.get('images')} wallpapers{acc} · {new} new mask(s) since · {auto}"
            + ("" if cfg["depth"]["mode"] == "learned" else " · `depth.mode learned` uses it for cutouts"))
