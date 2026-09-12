"""Wallpaper renaming — mode set in config [rename].mode.

  0  off (default) — nothing is touched
  1  prefix animated files with rename.animated_prefix (sorts by type, keeps original names)
  2  full rename: <animated_wallpaper_prefix>_<n> / <wallpaper_prefix>_<n>, per directory

Runs over the hidden folder too (each directory is renamed independently, so hidden
files get their own numbering and never collide with the main folder's).
"""
import os
import re
import tempfile

from . import backend, config

_NAT = re.compile(r"(\d+)")


def _natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in _NAT.split(s)]


def _by_dir(files: list[str]) -> dict:
    by_dir: dict[str, list[str]] = {}
    for f in files:
        by_dir.setdefault(os.path.dirname(f), []).append(f)
    return by_dir


def _mode1_ops(files: list[str], cfg: dict) -> list[tuple[str, str]]:
    prefix = cfg["rename"]["animated_prefix"]
    ops = []
    for f in files:
        d, name = os.path.split(f)
        if config.is_video(cfg, f) and not name.startswith(prefix):
            ops.append((f, os.path.join(d, prefix + name)))
    return ops


def _mode2_ops(files: list[str], cfg: dict) -> list[tuple[str, str]]:
    """Two-pass (via temp names) to avoid collisions, like the old shell script."""
    by_dir = _by_dir(files)
    ops: list[tuple[str, str]] = []
    prefixes = (cfg["rename"]["animated_wallpaper_prefix"], cfg["rename"]["wallpaper_prefix"])
    for d, group in by_dir.items():
        animated = sorted((f for f in group if config.is_video(cfg, f)), key=_natural_key)
        stills = sorted((f for f in group if not config.is_video(cfg, f)), key=_natural_key)
        for prefix, flist in zip(prefixes, (animated, stills)):
            for i, f in enumerate(flist, start=1):
                ext = os.path.splitext(f)[1]
                target = os.path.join(d, f"{prefix}_{i}{ext}")
                if os.path.abspath(f) != os.path.abspath(target):
                    ops.append((f, target))
    return ops


def plan(cfg: dict) -> list[tuple[str, str]]:
    """Return (old, new) pairs the current mode would apply. Doesn't touch disk."""
    mode = cfg["rename"]["mode"]
    if mode == 0:
        return []
    files = backend.gather_wallpapers(cfg, include_hidden=True)
    if mode == 1:
        return _mode1_ops(files, cfg)
    if mode == 2:
        return _mode2_ops(files, cfg)
    raise ValueError(f"unknown rename mode {mode!r} (expected 0, 1 or 2)")


def apply(ops: list[tuple[str, str]]) -> None:
    """Rename via temp names first so overlapping old/new names never collide."""
    if not ops:
        return
    tmp_names = []
    for old, _ in ops:
        d = os.path.dirname(old)
        fd, tmp = tempfile.mkstemp(prefix=".wallflow_rename_", dir=d or ".")
        os.close(fd)
        os.remove(tmp)
        os.rename(old, tmp)
        tmp_names.append(tmp)
    for tmp, (_, new) in zip(tmp_names, ops):
        os.rename(tmp, new)


def run(cfg: dict) -> list[tuple[str, str]]:
    ops = plan(cfg)
    apply(ops)
    return ops
