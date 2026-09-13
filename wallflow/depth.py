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
import json
import os
import shutil
import subprocess
import sys
import time
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


def setup(gpu: bool = False, python: str = "", log=print) -> int:
    """Create the venv and pip-install rembg (+ onnxruntime). Idempotent.

    `python` = interpreter for the venv. onnxruntime wheels usually trail the newest
    CPython by a few months — if pip finds none for the system python, pass an older
    one (`--python python3.12`)."""
    vp = venv_python()
    if python and vp.exists():
        import shutil
        shutil.rmtree(paths.DEPTH_VENV, ignore_errors=True)   # rebuild with the requested interpreter
    if not vp.exists():
        log(f"creating venv {paths.DEPTH_VENV} ({python or sys.executable})")
        r = subprocess.run([python or sys.executable, "-m", "venv", str(paths.DEPTH_VENV)])
        if r.returncode != 0:
            log("could not create the venv (python3-venv missing?)")
            return 1
    extra = "gpu" if gpu else "cpu"
    log(f"installing rembg[{extra}] (a few hundred MB, one-off) …")
    r = subprocess.run([str(vp), "-m", "pip", "install", "--upgrade", "--quiet",
                        f"rembg[{extra}]", "pillow"])
    if r.returncode != 0:
        log("pip failed — if no onnxruntime wheel exists for this python yet, use an older one:\n"
            "    wallflow depth setup --python python3.12   (Arch: pacman -S python312, or `uv python install 3.12`)")
        return 1
    if gpu:
        log("note: onnxruntime-gpu needs CUDA + cuDNN libraries on the system; if cutouts fail, "
            "rerun `wallflow depth setup` without --gpu")
    log("done — the segmentation model downloads on first use (~170 MB to ~/.u2net)")
    return 0


# --- per-image on/off --------------------------------------------------------

def disabled() -> set[str]:
    try:
        return set(json.loads(paths.DEPTH_OFF_FILE.read_text()))
    except (OSError, ValueError):
        return set()


def set_enabled(src: str, on: bool) -> None:
    src = os.path.abspath(src)
    off = disabled()
    (off.discard if on else off.add)(src)
    paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    paths.DEPTH_OFF_FILE.write_text(json.dumps(sorted(off), indent=2))


def enabled_for(src: str) -> bool:
    return os.path.abspath(src) not in disabled()


def notify(title: str, body: str = "", urgency: str = "low") -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "wallflow", "-u", urgency, title, body],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --- cache -----------------------------------------------------------------

def target(src: str, cfg: dict) -> Path:
    st = os.stat(src)
    d = cfg["depth"]
    key = f"{src}:{st.st_mtime_ns}:{d['model']}:{int(bool(d['alpha_matting']))}"
    return paths.CUTOUT_DIR / (hashlib.sha1(key.encode()).hexdigest() + ".png")


def applicable(src: str, cfg: dict) -> bool:
    """Only still images get a cutout (GIF counts as video), and only if not switched
    off for this image (`wallflow depth off <file>`)."""
    return cfg["depth"]["enabled"] and not config.is_video(cfg, src) and enabled_for(src)


def is_image(src: str, cfg: dict) -> bool:
    return not config.is_video(cfg, src)


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


def pending(cfg: dict, force: bool = False) -> list[str]:
    """Images that would be segmented by `wallflow depth all`."""
    from .backend import gather_wallpapers
    return [f for f in gather_wallpapers(cfg, include_hidden=True)
            if applicable(f, cfg) and (force or wanted(f, cfg))]


def _fmt_secs(s: float) -> str:
    s = int(round(s))
    return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


def run_all(cfg: dict, force: bool = False, yes: bool = False, log=print) -> int:
    """`wallflow depth all` — segment every image that has no cutout yet. Slow on
    CPU (seconds to minutes per image with birefnet + alpha matting), so it says so,
    asks once, and then shows progress + a running ETA."""
    if not ready():
        log("depth is not set up — run `wallflow depth setup`")
        return 1
    todo = pending(cfg, force)
    if not todo:
        log("nothing to do — every image already has a cutout (or is switched off)")
        return 0
    d = cfg["depth"]
    log(f"{len(todo)} image(s) to segment with {d['model']}"
        f"{' + alpha matting' if d['alpha_matting'] else ''}.")
    log("! This can take a LONG time on CPU — roughly 5 s to 2 min per image depending on "
        "model, matting and resolution. It runs in the foreground; Ctrl-C stops it, "
        "finished cutouts are kept and it resumes where it left off next time.")
    if not yes and sys.stdin.isatty():
        if input("continue? [y/N] ").strip().lower() not in ("y", "yes"):
            log("aborted")
            return 1
    t0 = time.monotonic()
    done_n = 0
    for i, f in enumerate(todo, 1):
        name = os.path.basename(f)
        eta = ""
        if done_n:
            per = (time.monotonic() - t0) / done_n
            eta = f"  (ETA {_fmt_secs(per * (len(todo) - i + 1))})"
        log(f"[{i}/{len(todo)}] {name} …{eta}", end="", flush=True)
        t1 = time.monotonic()
        res = run(f, cfg, force=force, quiet=True, wait=True)
        done_n += 1
        log(f"  {'ok' if res else 'no subject (skipped)'}  {_fmt_secs(time.monotonic() - t1)}")
    log(f"done: {len(todo)} image(s) in {_fmt_secs(time.monotonic() - t0)}")
    return 0


def status_text(cfg: dict) -> str:
    from .backend import gather_wallpapers, read_current
    imgs = [f for f in gather_wallpapers(cfg, include_hidden=True) if is_image(f, cfg)]
    off = disabled()
    have = sum(1 for f in imgs if existing(f, cfg))
    skipped = sum(1 for f in imgs if applicable(f, cfg) and target(f, cfg).with_suffix(".skip").exists())
    cur = read_current()
    d = cfg["depth"]
    lines = [
        f"setup    : {'ready (' + str(paths.DEPTH_VENV) + ')' if ready() else 'not set up — wallflow depth setup'}",
        f"model    : {d['model']}{' + alpha matting' if d['alpha_matting'] else ''}",
        f"auto     : {'on — wallflow watch segments new images' if d['auto'] else 'off (depth.auto)'}",
        f"images   : {len(imgs)} total, {have} with cutout, {skipped} without subject, "
        f"{len([f for f in imgs if f in off])} switched off, {len(pending(cfg))} pending",
    ]
    if cur:
        state = ("video — no cutout" if config.is_video(cfg, cur) else
                 "off for this image" if cur in off else
                 "cutout ready" if existing(cur, cfg) else
                 "no subject found" if target(cur, cfg).with_suffix(".skip").exists() else "pending")
        lines.append(f"current  : {os.path.basename(cur)} — {state}")
    lines.append("           wallflow depth all | on|off [<file>] | --file <f> | --prune")
    return "\n".join(lines)


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
