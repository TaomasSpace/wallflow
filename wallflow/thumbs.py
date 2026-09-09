"""Cached JPEG thumbnails. Also used as the still frame wallust themes videos from."""
import hashlib
import os
import shutil
import subprocess

from . import config, paths


def thumb_path(src: str, width: int) -> str:
    st = os.stat(src)
    key = f"{src}:{st.st_mtime_ns}:{width}"
    return str(paths.THUMB_DIR / (hashlib.sha1(key.encode()).hexdigest() + ".jpg"))


def _video_thumb(src: str, tp: str, width: int) -> str:
    if not shutil.which("ffmpeg"):
        return src
    cmd = ["ffmpeg", "-y", "-ss", "1", "-i", src, "-frames:v", "1",
           "-vf", f"scale={width}:-2", "-q:v", "3", tp]
    quiet = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    r = subprocess.run(cmd, **quiet)
    if r.returncode != 0 or not os.path.exists(tp):
        cmd[cmd.index("-ss") + 1] = "0"        # clip shorter than 1s
        subprocess.run(cmd, **quiet)
    return tp if os.path.exists(tp) else src


def _image_thumb(src: str, tp: str, width: int) -> str:
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage
    except ImportError:
        # headless (theme-only) path without Qt: ffmpeg can scale images too
        return _video_thumb(src, tp, width) if shutil.which("ffmpeg") else src
    img = QImage(src)
    if img.isNull():
        return src
    if img.width() > width:
        img = img.scaledToWidth(width, Qt.SmoothTransformation)
    img.save(tp, "JPEG", 85)
    return tp if os.path.exists(tp) else src


def thumb_for(src: str, cfg: dict | None = None) -> str:
    cfg = cfg or config.load()
    width = int(cfg["ui"]["thumb_width"])
    tp = thumb_path(src, width)
    if os.path.exists(tp):
        return tp
    paths.THUMB_DIR.mkdir(parents=True, exist_ok=True)
    if config.is_video(cfg, src):
        return _video_thumb(src, tp, width)
    return _image_thumb(src, tp, width)
