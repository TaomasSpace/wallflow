"""Downscale/cap video wallpapers to display resolution + fps so the decoder loops cheaply.

Output lives in ~/.cache/wallflow/transcoded/<hash>.mp4. The hash covers the
source (path+mtime) and every setting that changes the output, so editing
config.toml naturally invalidates old transcodes. Sources that are already
within limits get a `.skip` marker and are played as-is.
"""
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from . import config, paths


def _settings(cfg: dict) -> dict:
    t = cfg["transcode"]
    return {"enc": t["encoder"], "fps": int(t["fps"]), "w": int(t["max_width"]), "q": int(t["quality"])}


def target(src: str, cfg: dict) -> Path:
    st = os.stat(src)
    s = _settings(cfg)
    key = f"{src}:{st.st_mtime_ns}:{s['w']}:{s['fps']}:{s['enc']}:{s['q']}"
    return paths.TRANSCODE_DIR / (hashlib.sha1(key.encode()).hexdigest() + ".mp4")


def existing(src: str, cfg: dict) -> str | None:
    """Transcoded file to play, or None (play the original)."""
    if not cfg["transcode"]["enabled"] or cfg["transcode"]["encoder"] in ("none", ""):
        return None
    t = target(src, cfg)
    return str(t) if t.exists() and t.stat().st_size > 0 else None


def wanted(src: str, cfg: dict) -> bool:
    """True if we have neither a transcode nor a skip-marker for this source."""
    t = cfg["transcode"]
    if not t["enabled"] or t["encoder"] in ("none", "", "auto") or not shutil.which("ffmpeg"):
        return False
    dst = target(src, cfg)
    return not dst.exists() and not dst.with_suffix(".skip").exists()


def _probe(src: str) -> dict:
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=width,height,r_frame_rate,codec_name", "-of", "json", src]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        st = json.loads(r.stdout)["streams"][0]
        num, _, den = st.get("r_frame_rate", "0/1").partition("/")
        fps = float(num) / float(den or 1)
        return {"width": int(st.get("width", 0)), "fps": fps, "codec": st.get("codec_name", "")}
    except Exception:
        return {}


def _needs_transcode(src: str, cfg: dict, info: dict) -> bool:
    if not info:
        return True
    s = _settings(cfg)
    if src.lower().endswith(".gif"):
        return True                      # gif decode is CPU-only; always convert
    if info["width"] > s["w"] or info["fps"] > s["fps"] + 0.5:
        return True
    return info["codec"] not in ("hevc", "h264")   # e.g. vp9/av1 with no hwdec


def codec_compatible(src: str, cfg: dict) -> bool:
    """True if mpvpaper can loop/animate the raw file correctly as-is — even if it's
    still worth transcoding down for size/fps. False (a GIF, or a codec mpvpaper
    can't reliably loop — vp9/av1/etc.) means playing it untranscoded doesn't just
    look worse, it looks static/broken, so it must never be shown before the
    one-off transcode finishes (see apply() in backend.py)."""
    if src.lower().endswith(".gif"):
        return False
    info = _probe(src)
    return bool(info) and info.get("codec") in ("hevc", "h264")


def _encoder_args(cfg: dict) -> tuple[list[str], list[str], str]:
    """(global args before -i, codec args after filters, extra filter suffix)."""
    s = _settings(cfg)
    enc, q = s["enc"], s["q"]
    if "nvenc" in enc:
        return [], ["-pix_fmt", "yuv420p", "-c:v", enc, "-preset", "p5", "-cq", str(q)], ""
    if "vaapi" in enc:
        dev = cfg["transcode"]["vaapi_device"] or "/dev/dri/renderD128"
        return ["-vaapi_device", dev], ["-c:v", enc, "-qp", str(q)], ",format=nv12,hwupload"
    if "qsv" in enc:
        return [], ["-pix_fmt", "nv12", "-c:v", enc, "-global_quality", str(q)], ""
    preset = ["-preset", "medium"]
    return [], ["-pix_fmt", "yuv420p", "-c:v", enc, "-crf", str(q)] + preset, ""


def run(src: str, cfg: dict, force: bool = False, quiet: bool = True, wait: bool = False) -> Path | None:
    """Transcode one file. Returns the output path, or None if skipped/failed.

    `wallflow watch`'s prewarm and the on-demand transcode from `apply()` can both
    target the exact same file within seconds of each other (e.g. right after adding
    it). A per-target flock serializes them so the second caller never races the
    first one's .part.mp4 — stale .lock files are harmless and get swept up by
    `prune()` like any other file that doesn't match a current target.

    wait=True blocks for the lock (rather than backing off immediately) and, once
    held, re-checks for a result before doing its own encode — for callers that
    need a finished file, not just "started one" (see apply()'s codec_compatible
    branch: playing the source before it's transcoded would look broken, not just
    unoptimized, so it's worth waiting on someone else's in-flight encode)."""
    paths.TRANSCODE_DIR.mkdir(parents=True, exist_ok=True)
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
            return dst if dst.exists() else None      # non-wait mode: another process has this one
        if dst.exists() and not force:                # finished while we waited for the lock
            return dst
        if skip.exists() and not force:                # decided (by whoever held the lock) not needed
            return None
        info = _probe(src)
        if not force and not _needs_transcode(src, cfg, info):
            skip.touch()
            return None
        s = _settings(cfg)
        pre, codec, vf_extra = _encoder_args(cfg)
        vf = f"scale=w='min({s['w']},iw)':h=-2,fps={s['fps']}{vf_extra}"
        tmp = dst.with_suffix(".part.mp4")
        cmd = ["ffmpeg", "-y", "-hide_banner", "-v", "error", *pre, "-i", src,
               "-vf", vf, *codec, "-an", "-movflags", "+faststart", str(tmp)]
        out = subprocess.DEVNULL if quiet else None
        r = subprocess.run(cmd, stdout=out, stderr=out)
        if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(dst)
            return dst
        tmp.unlink(missing_ok=True)
        return None
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()
        # not unlinked here — removing it while still open would race a process that
        # opened the same path a moment earlier; prune() cleans it up later instead


def run_all(cfg: dict, force: bool = False, log=print) -> None:
    from .backend import gather_wallpapers
    vids = [f for f in gather_wallpapers(cfg) if config.is_video(cfg, f)]
    for i, f in enumerate(vids, 1):
        if not force and not wanted(f, cfg):
            log(f"[{i}/{len(vids)}] cached   {os.path.basename(f)}")
            continue
        log(f"[{i}/{len(vids)}] encoding {os.path.basename(f)} …")
        res = run(f, cfg, force=force)
        log(f"          -> {'ok' if res else 'skipped (already within limits)'}")


def prune(cfg: dict) -> int:
    """Delete transcodes whose source no longer exists / settings changed."""
    from .backend import gather_wallpapers
    keep = set()
    for f in gather_wallpapers(cfg):
        if config.is_video(cfg, f):
            t = target(f, cfg)
            keep.update({t.name, t.with_suffix(".skip").name})
    n = 0
    for p in paths.TRANSCODE_DIR.glob("*"):
        if p.name not in keep:
            p.unlink(missing_ok=True)
            n += 1
    return n


def resolve_encoder(cfg: dict, save: bool = True) -> str:
    """Turn encoder='auto' into a concrete, probed encoder (and persist it)."""
    if cfg["transcode"]["encoder"] != "auto":
        return cfg["transcode"]["encoder"]
    from . import detect
    enc, dev = detect.pick_encoder()
    cfg["transcode"]["encoder"] = enc
    cfg["transcode"]["vaapi_device"] = dev
    if save:
        config.save(cfg)
    return enc
