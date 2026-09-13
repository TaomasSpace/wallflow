"""Subject cutouts for the depth overlay.

The wallpaper's foreground (a character, usually) is segmented once into a
transparent PNG and drawn *above* the overlay widgets by `wallflow overlay`
(templates/overlay.qml) — so a clock sits between background and subject.

Static images only. Videos/GIFs get no cutout (a per-frame matte is a v2 problem);
widgets then simply sit on top of the video.

Segmentation = rembg (isnet-anime by default; `depth.model` in config.toml).
It lives in its own venv (~/.local/share/wallflow-depth) so wallflow itself stays
dependency-light: `wallflow depth setup [--gpu]` creates it, and everything here
is a no-op until it exists. Cutouts cache in ~/.cache/wallflow/cutouts/<hash>.png,
keyed on source path+mtime+model — like transcodes, with a `.skip` marker for
images where no subject was found (landscapes).
"""
import fcntl
import hashlib
import os
import subprocess
import sys
from pathlib import Path

from . import config, paths

# Runs inside the venv (own python, own site-packages); wallflow never imports rembg.
_WORKER = r"""
import sys
from io import BytesIO
src, dst, model, am = sys.argv[1:5]
from rembg import new_session, remove
from PIL import Image
with open(src, "rb") as f:
    data = f.read()
out = remove(data, session=new_session(model), post_process_mask=True,
             alpha_matting=(am == "1"))
img = Image.open(BytesIO(out)).convert("RGBA")
alpha = img.getchannel("A")
hist = alpha.histogram()
opaque = sum(hist[16:])                      # pixels that are more than faintly visible
if opaque < 0.005 * img.width * img.height:  # < 0.5 %: nothing worth occluding with
    sys.exit(3)
img.save(dst, "PNG", optimize=False)
"""


# --- venv ------------------------------------------------------------------

def venv_python() -> Path:
    return paths.DEPTH_VENV / "bin" / "python"


def ready() -> bool:
    return venv_python().exists()


def setup(gpu: bool = False, log=print) -> int:
    """Create the venv and pip-install rembg (+ onnxruntime). Idempotent."""
    vp = venv_python()
    if not vp.exists():
        log(f"creating venv {paths.DEPTH_VENV}")
        r = subprocess.run([sys.executable, "-m", "venv", str(paths.DEPTH_VENV)])
        if r.returncode != 0:
            log("could not create the venv (python3-venv missing?)")
            return 1
    extra = "gpu" if gpu else "cpu"
    log(f"installing rembg[{extra}] (a few hundred MB, one-off) …")
    r = subprocess.run([str(vp), "-m", "pip", "install", "--upgrade", "--quiet",
                        f"rembg[{extra}]", "pillow"])
    if r.returncode != 0:
        log("pip failed — if it's a python-version wheel problem, try a slightly older "
            "python for the venv: rm -rf " + str(paths.DEPTH_VENV) + " && python3.12 -m venv " + str(paths.DEPTH_VENV))
        return 1
    if gpu:
        log("note: onnxruntime-gpu needs CUDA + cuDNN libraries on the system; if cutouts fail, "
            "rerun `wallflow depth setup` without --gpu")
    log("done — the segmentation model downloads on first use (~170 MB to ~/.u2net)")
    return 0


# --- cache -----------------------------------------------------------------

def target(src: str, cfg: dict) -> Path:
    st = os.stat(src)
    d = cfg["depth"]
    key = f"{src}:{st.st_mtime_ns}:{d['model']}:{int(bool(d['alpha_matting']))}"
    return paths.CUTOUT_DIR / (hashlib.sha1(key.encode()).hexdigest() + ".png")


def applicable(src: str, cfg: dict) -> bool:
    """Only still images get a cutout (GIF counts as video)."""
    return cfg["depth"]["enabled"] and not config.is_video(cfg, src)


def existing(src: str, cfg: dict) -> str | None:
    """Cached cutout for this wallpaper, or None."""
    if not applicable(src, cfg):
        return None
    t = target(src, cfg)
    return str(t) if t.exists() and t.stat().st_size > 0 else None


def wanted(src: str, cfg: dict) -> bool:
    """True if there's neither a cutout nor a skip-marker yet (and we can make one)."""
    if not applicable(src, cfg) or not ready():
        return False
    dst = target(src, cfg)
    return not dst.exists() and not dst.with_suffix(".skip").exists()


def run(src: str, cfg: dict, force: bool = False, quiet: bool = True, wait: bool = False) -> Path | None:
    """Segment one image. Returns the cutout path, or None (no subject / failed).

    Same per-target flock as transcode.run(): apply() and `wallflow depth` may hit
    the same file at once; the second caller backs off (or waits, wait=True)."""
    if not ready():
        if not quiet:
            print("depth is not set up — run `wallflow depth setup`", file=sys.stderr)
        return None
    paths.CUTOUT_DIR.mkdir(parents=True, exist_ok=True)
    dst = target(src, cfg)
    skip = dst.with_suffix(".skip")
    if dst.exists() and not force:
        return dst
    lock_fh = open(dst.with_suffix(".lock"), "w")
    try:
        flags = fcntl.LOCK_EX if wait else (fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            fcntl.flock(lock_fh, flags)
        except OSError:
            return dst if dst.exists() else None
        if dst.exists() and not force:
            return dst
        if skip.exists() and not force:
            return None
        tmp = dst.with_suffix(".part.png")
        d = cfg["depth"]
        cmd = [str(venv_python()), "-c", _WORKER, src, str(tmp), d["model"],
               "1" if d["alpha_matting"] else "0"]
        out = subprocess.DEVNULL if quiet else None
        r = subprocess.run(cmd, stdout=out, stderr=out)
        if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            skip.unlink(missing_ok=True)
            tmp.replace(dst)
            return dst
        tmp.unlink(missing_ok=True)
        if r.returncode == 3:                 # ran fine, found no subject
            skip.touch()
        return None
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


def run_all(cfg: dict, force: bool = False, log=print) -> None:
    from .backend import gather_wallpapers
    imgs = [f for f in gather_wallpapers(cfg) if applicable(f, cfg)]
    if not ready():
        log("depth is not set up — run `wallflow depth setup`")
        return
    for i, f in enumerate(imgs, 1):
        if not force and not wanted(f, cfg):
            log(f"[{i}/{len(imgs)}] cached      {os.path.basename(f)}")
            continue
        log(f"[{i}/{len(imgs)}] segmenting  {os.path.basename(f)} …")
        res = run(f, cfg, force=force, quiet=False)
        log(f"          -> {'ok' if res else 'no subject found (skipped)'}")


def prune(cfg: dict) -> int:
    """Delete cutouts whose source no longer exists / settings changed."""
    from .backend import gather_wallpapers
    keep = set()
    for f in gather_wallpapers(cfg):
        if not config.is_video(cfg, f):
            t = target(f, cfg)
            keep.update({t.name, t.with_suffix(".skip").name})
    n = 0
    for p in paths.CUTOUT_DIR.glob("*") if paths.CUTOUT_DIR.is_dir() else []:
        if p.name not in keep:
            p.unlink(missing_ok=True)
            n += 1
    return n
