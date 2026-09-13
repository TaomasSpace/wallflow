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
# argv: src dst mode near feather subject_model depth_model alpha_matting depthmap_path
import sys, os
from io import BytesIO
import numpy as np
from PIL import Image
src, dst, mode, near, feather, smodel, dmodel, am, dmap, minsep, margin = sys.argv[1:12]
auto_near = str(near).lower() == "auto"
near = None if auto_near else float(near)
feather = float(feather); minsep = float(minsep); margin = float(margin)
img = Image.open(src).convert("RGB")
W, H = img.size
rgb = np.asarray(img)

def box(a, r):
    # O(1) box filter via integral image, replicate borders
    a = np.pad(a, ((r, r), (r, r)), mode="edge")
    c = np.cumsum(np.cumsum(a, 0), 1)
    c = np.pad(c, ((1, 0), (1, 0)))
    d = 2 * r + 1
    return (c[d:, d:] - c[:-d, d:] - c[d:, :-d] + c[:-d, :-d]) / (d * d)

def guided(alpha, guide, r, eps):
    # He et al. guided filter, grey guide: snaps the soft depth mask to real image edges
    mI, mp = box(guide, r), box(alpha, r)
    cov = box(guide * alpha, r) - mI * mp
    var = box(guide * guide, r) - mI * mI
    a = cov / (var + eps); b = mp - a * mI
    return np.clip(box(a, r) * guide + box(b, r), 0, 1).astype(np.float32)

def otsu(d):
    # threshold that best splits the depth histogram into two groups; also returns
    # how well it splits (between-class / total variance, 0..1 — low = smooth gradient,
    # i.e. no distinct foreground)
    h, edges = np.histogram(d, bins=256, range=(0.0, 1.0))
    h = h.astype(np.float64); p = h / h.sum()
    w0 = np.cumsum(p); mu = np.cumsum(p * np.arange(256))
    muT = mu[-1]
    w1 = 1.0 - w0
    with np.errstate(divide="ignore", invalid="ignore"):
        sb = (muT * w0 - mu) ** 2 / (w0 * w1)
    sb[~np.isfinite(sb)] = 0
    ks = np.flatnonzero(sb >= sb.max() - 1e-9)   # a flat maximum = an empty gap: cut in its middle
    k = (ks[0] + ks[-1]) / 2.0
    var = ((np.arange(256) - muT) ** 2 * p).sum()
    return (k + 0.5) / 256.0, float(sb.max() / var) if var > 0 else 0.0

def load_depth():
    if os.path.exists(dmap):
        return np.load(dmap).astype(np.float32)
    if True:
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        path = hf_hub_download(dmodel, "onnx/model.onnx")
        prov = [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider") if p in ort.get_available_providers()]
        sess = ort.InferenceSession(path, providers=prov)
        name = sess.get_inputs()[0].name
        mean = np.array([0.485, 0.456, 0.406], np.float32); std = np.array([0.229, 0.224, 0.225], np.float32)
        def infer(nw, nh):
            x = np.asarray(img.resize((nw, nh), Image.BICUBIC)).astype(np.float32) / 255.0
            x = ((x - mean) / std).transpose(2, 0, 1)[None]
            return sess.run(None, {name: x})[0][0]
        sc = 518.0 / max(W, H)
        nw = max(14, int(round(W * sc / 14)) * 14); nh = max(14, int(round(H * sc / 14)) * 14)
        try:
            out = infer(nw, nh)                      # dynamic shapes (keeps aspect)
        except Exception:
            out = infer(518, 518)                    # static export
        out = np.asarray(out, np.float32)
        if out.ndim == 3: out = out[0]
        out = (out - out.min()) / (out.max() - out.min() + 1e-6)   # relative inverse depth: 1 = nearest
        d = np.asarray(Image.fromarray(out, "F").resize((W, H), Image.BILINEAR), np.float32)
        np.save(dmap, d.astype(np.float16))
        return d

threshold = None      # the depth value actually used as the near/far cut (for the preview)
separation = 1.0

def depth_alpha(d):
    global threshold, separation
    if auto_near:
        # look at a downscaled copy: cheaper, and it de-emphasises thin edge artefacts
        small = np.asarray(Image.fromarray(d, "F").resize((max(1, W // 8), max(1, H // 8))), np.float32)
        threshold, separation = otsu(small)
    else:
        threshold = 1.0 - near
    a = np.clip((d - threshold) / max(feather, 1e-3) + 0.5, 0, 1)
    guide = (rgb.astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)) / 255.0
    r = max(4, int(round(0.004 * max(W, H))))
    return guided(a, guide, r, 1e-3)

def subject_alpha():
    from rembg import new_session, remove
    with open(src, "rb") as f:
        data = f.read()
    m = remove(data, session=new_session(smodel), post_process_mask=True,
               alpha_matting=(am == "1"), only_mask=True)
    return np.asarray(Image.open(BytesIO(m)).convert("L"), np.float32) / 255.0

preview = mode.startswith("preview:")
if preview:
    mode = mode.split(":", 1)[1]
decision = mode
if mode == "subject":
    alpha = subject_alpha()
elif mode == "both":
    alpha = np.maximum(depth_alpha(load_depth()), subject_alpha())
elif mode == "auto":
    # subject first: the salient object (character, bike, mountain) is the layer;
    # depth only ADDS what is clearly nearer than the subject's own nearest parts
    # (a fan in front of the character, grass or foam in front of a face). Floors
    # and walls the subject stands on are about as near as the subject -> excluded.
    d = load_depth()
    sa = subject_alpha()
    if (sa > 0.5).sum() >= 0.005 * W * H:
        q = float(np.percentile(d[sa > 0.5], 80))
        threshold = min(1.0, q + margin)
        occ = np.clip((d - threshold) / max(feather, 1e-3) + 0.5, 0, 1)
        guide = (rgb.astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)) / 255.0
        occ = guided(occ, guide, max(4, int(round(0.004 * max(W, H)))), 1e-3)
        share = float((occ > 0.5).sum()) / (W * H)
        if share >= 0.003:
            decision = f"auto: subject + {share * 100:.1f}% of the image nearer than it"
            alpha = np.maximum(sa, occ)
        else:
            decision = "auto: subject only (nothing nearer than it)"
            alpha = sa
    else:
        da = depth_alpha(d)
        if auto_near and separation < minsep:
            decision = "auto: no subject, no distinct foreground -> nothing"
            alpha = np.zeros_like(da)
        else:
            decision = "auto: no subject found -> nearest depth group"
            alpha = da
elif mode == "near":
    # depth decides what is in front; the subject model decides whether the
    # character belongs to that front group (-> union) or sits behind it (-> depth only)
    d = load_depth()
    da = depth_alpha(d)
    sa = subject_alpha()
    if auto_near and separation < minsep and (sa > 0.5).sum() < 0.005 * W * H:
        decision = "nothing distinct in front"
        alpha = np.zeros_like(da)
    elif (sa > 0.5).sum() >= 0.005 * W * H:
        subj_depth = float(np.median(d[sa > 0.5]))
        if subj_depth >= threshold - 0.08:
            decision = "near: character is in front -> depth + subject"
            alpha = np.maximum(da, sa)
        else:
            decision = "near: character is behind the foreground -> depth only"
            alpha = da
    else:
        decision = "near: no character found -> depth only"
        alpha = da
else:
    alpha = depth_alpha(load_depth())

if preview:
    # left: the depth map (bright = near) with the current threshold as a red band;
    # right: what would be cut out, over a green backdrop
    sc = 1280.0 / W
    pw, ph = int(W * sc), int(H * sc)
    d = np.load(dmap).astype(np.float32) if os.path.exists(dmap) else np.zeros((H, W), np.float32)
    dm = np.asarray(Image.fromarray((d * 255).astype(np.uint8)).resize((pw, ph)), np.uint8)
    left = np.dstack([dm, dm, dm]).astype(np.float32)
    if threshold is not None:
        band = (np.abs(np.asarray(Image.fromarray(np.ascontiguousarray(d, np.float32), "F").resize((pw, ph)), np.float32) - threshold) < 0.01)
        left[band] = [255, 40, 40]
    print(f"threshold {threshold if threshold is None else round(threshold, 3)}  "
          f"separation {separation:.2f}  {decision}")
    small = np.asarray(img.resize((pw, ph)), np.float32)
    a = np.asarray(Image.fromarray(np.ascontiguousarray(alpha, np.float32), "F").resize((pw, ph)), np.float32)[..., None]
    right = small * a + np.array([40, 200, 60], np.float32) * (1 - a)
    both = np.nan_to_num(np.concatenate([left, right], 1)).clip(0, 255).astype(np.uint8)
    Image.fromarray(both).save(dst, "PNG")
    sys.exit(0)

print(decision)
if (alpha > 0.06).sum() < 0.005 * W * H:      # < 0.5 %: nothing worth occluding with
    sys.exit(3)
out = np.dstack([rgb, (alpha * 255 + 0.5).astype(np.uint8)])
Image.fromarray(out, "RGBA").save(dst, "PNG", optimize=False)
"""


last_error = ""      # stderr tail of the last failed worker run (for callers that were quiet)


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
                        f"rembg[{extra}]", "pillow", "numpy", "huggingface_hub"])
    if r.returncode != 0:
        log("pip failed — if no onnxruntime wheel exists for this python yet, use an older one:\n"
            "    wallflow depth setup --python python3.12   (Arch: pacman -S python312, or `uv python install 3.12`)")
        return 1
    if gpu:
        log("note: onnxruntime-gpu needs CUDA + cuDNN libraries on the system; if cutouts fail, "
            "rerun `wallflow depth setup` without --gpu")
    log("done — models download on first use (rembg -> ~/.u2net, Depth Anything -> ~/.cache/huggingface)")
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


# --- per-image tuning ---------------------------------------------------------
# One threshold can't fit every picture: a floor-level shot's "nearest 30 %" is the
# floor, a portrait's is the nose. `wallflow depth tune near=0.5 mode=both [file]`
# overrides any [depth] key for that image only.

TUNABLE = ("mode", "near", "feather", "model", "alpha_matting", "depth_model", "min_separation",
           "occluder_margin")


def tunes() -> dict[str, dict]:
    try:
        return json.loads(paths.DEPTH_TUNE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def set_tune(src: str, values: dict | None) -> dict:
    """values=None clears the override. Returns the image's effective override."""
    src = os.path.abspath(src)
    t = tunes()
    if values is None:
        t.pop(src, None)
    else:
        cur = t.get(src, {})
        cur.update(values)
        t[src] = cur
    paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    paths.DEPTH_TUNE_FILE.write_text(json.dumps(t, indent=2))
    return t.get(src, {})


def settings(src: str, cfg: dict) -> dict:
    """[depth] with this image's overrides applied."""
    d = dict(cfg["depth"])
    d.update(tunes().get(os.path.abspath(src), {}))
    return d


def notify(title: str, body: str = "", urgency: str = "low") -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "wallflow", "-u", urgency, title, body],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --- cache -----------------------------------------------------------------

def target(src: str, cfg: dict) -> Path:
    st = os.stat(src)
    d = settings(src, cfg)
    key = f"{src}:{st.st_mtime_ns}:{d['mode']}"
    if d["mode"] in ("subject", "both", "auto", "near"):
        key += f":{d['model']}:{int(bool(d['alpha_matting']))}"
    if d["mode"] in ("depth", "both", "auto", "near"):
        near = d["near"]
        key += f":{d['depth_model']}:{near if isinstance(near, str) else f'{float(near):.3f}'}:{float(d['feather']):.3f}"
    if d["mode"] in ("auto", "near"):
        key += f":{float(d['min_separation']):.2f}"
    if d["mode"] == "auto":
        key += f":{float(d['occluder_margin']):.3f}"
    return paths.CUTOUT_DIR / (hashlib.sha1(key.encode()).hexdigest() + ".png")


def depthmap_path(src: str, cfg: dict) -> Path:
    """The raw depth map is cached on its own: retuning depth.near/feather then
    only redoes the (fast) thresholding, not the model."""
    st = os.stat(src)
    key = f"{src}:{st.st_mtime_ns}:{settings(src, cfg)['depth_model']}"
    return paths.CUTOUT_DIR / (hashlib.sha1(key.encode()).hexdigest() + ".depth.npy")


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
        cmd = _worker_cmd(src, str(tmp), cfg)
        global last_error
        last_error = ""
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if not quiet and r.stdout.strip():
            print(r.stdout.strip())
        if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            skip.unlink(missing_ok=True)
            tmp.replace(dst)
            return dst
        tmp.unlink(missing_ok=True)
        if r.returncode == 3:                 # ran fine, found nothing in front
            skip.touch()
            return None
        last_error = (r.stderr or "").strip().splitlines()[-1:] and (r.stderr or "").strip().splitlines()[-1] or f"exit {r.returncode}"
        if not quiet:
            print(r.stderr, file=sys.stderr)
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


def _worker_cmd(src: str, dst: str, cfg: dict, preview: bool = False) -> list[str]:
    d = settings(src, cfg)
    return [str(venv_python()), "-c", _WORKER, src, dst,
            ("preview:" if preview else "") + d["mode"],
            str(d["near"]), str(d["feather"]), d["model"], d["depth_model"],
            "1" if d["alpha_matting"] else "0", str(depthmap_path(src, cfg)),
            str(d["min_separation"]), str(d["occluder_margin"])]


def preview(src: str, cfg: dict, log=print) -> Path | None:
    """Render depth map + resulting cutout side by side and open it."""
    if not ready():
        log("depth is not set up — run `wallflow depth setup`")
        return None
    paths.CUTOUT_DIR.mkdir(parents=True, exist_ok=True)
    out = paths.CACHE_DIR / "depth-preview.png"
    r = subprocess.run(_worker_cmd(src, str(out), cfg, preview=True),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        log(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else f"preview failed ({r.returncode})")
        return None
    d = settings(src, cfg)
    log(f"{out}\n  mode {d['mode']}, near {d['near']}, feather {d['feather']} -> {r.stdout.strip()}\n"
        "  (left: depth map, bright = near, red = threshold · right: the cutout)")
    if shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", str(out)], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out


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
    what = {"depth": f"depth ({d['depth_model'].split('/')[-1]}, near {d['near']})",
            "subject": f"subject ({d['model']}{' + alpha matting' if d['alpha_matting'] else ''})"}
    what["both"] = what["depth"] + " + " + what["subject"]
    log(f"{len(todo)} image(s) to cut out, mode {what.get(d['mode'], d['mode'])}.")
    log("! This can take a LONG time on CPU — roughly 5 s to 2 min per image depending on "
        "model, matting and resolution. It runs in the foreground; Ctrl-C stops it, "
        "finished cutouts are kept and it resumes where it left off next time.")
    if not yes and sys.stdin.isatty():
        if input("continue? [y/N] ").strip().lower() not in ("y", "yes"):
            log("aborted")
            return 1
    t0 = time.monotonic()
    done_n = failures = 0
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
        if res:
            verdict = "ok"
        elif last_error:
            verdict = f"FAILED: {last_error}"
            failures += 1
        else:
            verdict = "nothing in front (skipped)"
        log(f"  {verdict}  {_fmt_secs(time.monotonic() - t1)}")
        if failures >= 3 and done_n == failures:
            log("! every image fails — the venv is probably missing something. Run "
                "`wallflow depth setup` again, then `wallflow depth --file <img> -v` for the full error.")
            return 1
    log(f"done: {len(todo)} image(s) in {_fmt_secs(time.monotonic() - t0)}"
        + (f", {failures} failed" if failures else ""))
    return 1 if failures else 0


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
        f"mode     : {d['mode']}  (auto = subject + what's nearer than it · near = nearest depth group · depth · subject · both)",
        f"models   : depth {d['depth_model']} (near {d['near']}, feather {d['feather']})"
        f" · subject {d['model']}{' + alpha matting' if d['alpha_matting'] else ''}",
        f"auto     : {'on — wallflow watch segments new images' if d['auto'] else 'off (depth.auto)'}",
        f"images   : {len(imgs)} total, {have} with cutout, {skipped} without subject, "
        f"{len([f for f in imgs if f in off])} switched off, {len(pending(cfg))} pending",
    ]
    if cur:
        state = ("video — no cutout" if config.is_video(cfg, cur) else
                 "off for this image" if cur in off else
                 "cutout ready" if existing(cur, cfg) else
                 "nothing in front" if target(cur, cfg).with_suffix(".skip").exists() else "pending")
        tune = tunes().get(cur)
        lines.append(f"current  : {os.path.basename(cur)} — {state}"
                     + (f"  tuned: {' '.join(f'{k}={v}' for k, v in tune.items())}" if tune else ""))
    lines.append(f"tuned    : {len(tunes())} image(s) with their own settings (depth tune)")
    lines.append("           wallflow depth all | on|off [file] | tune k=v … [file] | map [file] | --prune")
    return "\n".join(lines)


def prune(cfg: dict) -> int:
    """Delete cutouts whose source no longer exists / settings changed."""
    from .backend import gather_wallpapers
    keep = set()
    for f in gather_wallpapers(cfg, include_hidden=True):
        if not config.is_video(cfg, f):
            t = target(f, cfg)
            keep.update({t.name, t.with_suffix(".skip").name, depthmap_path(f, cfg).name})
    n = 0
    for p in paths.CUTOUT_DIR.glob("*") if paths.CUTOUT_DIR.is_dir() else []:
        if p.name not in keep:
            p.unlink(missing_ok=True)
            n += 1
    return n
