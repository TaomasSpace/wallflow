"""Content identity for cache keys - so renaming or moving a wallpaper never redoes work.

Cutouts, depth maps, transcodes, thumbnails, hand-picks and per-image settings used to
be keyed by path + mtime; `rename.mode` renumbering the folder therefore looked like
"every wallpaper is new". They are keyed by what the file *is* now:

    file_id = sha1(size + first MiB + last MiB)[:20]

Cheap even for big videos, and remembered per path+size+mtime in ~/.cache/wallflow/ids.json,
so it's computed once per file version. `migrate()` moves a cache file from its old
path-based name to the new one the first time it's looked up, so nothing gets recomputed
on the switch either.
"""
import hashlib
import json
import os
from pathlib import Path

from . import paths

_CHUNK = 1 << 20
_IDS = paths.CACHE_DIR / "ids.json"
_cache: dict | None = None
_dirty = False


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(_IDS.read_text())
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _save() -> None:
    global _dirty
    if not _dirty:
        return
    try:
        paths.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _IDS.with_suffix(f".{os.getpid()}.part")
        tmp.write_text(json.dumps(_cache))
        tmp.replace(_IDS)
        _dirty = False
    except OSError:
        pass


def file_id(src) -> str:
    global _dirty
    src = os.path.abspath(str(src))
    st = os.stat(src)
    key = f"{src}|{st.st_size}|{st.st_mtime_ns}"
    ids = _load()
    if key in ids:
        return ids[key]
    h = hashlib.sha1(f"{st.st_size}:".encode())
    with open(src, "rb") as f:
        h.update(f.read(_CHUNK))
        if st.st_size > 2 * _CHUNK:
            f.seek(-_CHUNK, os.SEEK_END)
            h.update(f.read(_CHUNK))
    fid = h.hexdigest()[:20]
    ids[key] = fid
    _dirty = True
    if len(ids) > 20000:                       # forget the oldest entries now and then
        for k in list(ids)[: len(ids) - 15000]:
            del ids[k]
    _save()
    return fid


def legacy(src) -> str:
    """The old key prefix (path as given + mtime) - only used to find caches to migrate."""
    return f"{src}:{os.stat(src).st_mtime_ns}"


def sha(key: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()


def migrate(new: Path, old: Path) -> Path:
    """First lookup under the new name: adopt a cache file that still has its old name."""
    if new != old and not new.exists() and old.exists():
        try:
            os.replace(old, new)
        except OSError:
            pass
    return new
