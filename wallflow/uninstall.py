"""`wallflow uninstall [--purge]` — remove everything install.sh and the addons put in place.

Order matters: addons first (they need the code + state), then the Hyprland block,
then processes, then the code itself. --purge also wipes config, cache and the
addon backups, i.e. leaves no trace.
"""
import shutil
import subprocess
import sys

from . import addons, overlay, paths


def run(purge: bool = False, yes: bool = False) -> int:
    what = "wallflow, its addons, Hyprland block and launcher"
    if purge:
        what += f", plus {paths.CONFIG_DIR} and {paths.CACHE_DIR}"
    if not yes:
        try:
            ans = input(f"remove {what}? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("aborted")
            return 1

    addons.remove_all(purge=purge)

    try:
        from . import setup
        setup.remove()
    except Exception as e:                     # no Hyprland block / config — fine
        print(f"note: Hyprland block not removed ({e})", file=sys.stderr)

    overlay.stop()
    subprocess.run(["pkill", "-f", "wallflow.py pauser"], check=False)
    subprocess.run(["pkill", "-f", "wallflow.py watch"], check=False)
    subprocess.run(["pkill", "-x", "mpvpaper"], check=False)

    paths.LAUNCHER.unlink(missing_ok=True)
    shutil.rmtree(paths.INSTALL_DIR, ignore_errors=True)
    shutil.rmtree(paths.DEPTH_VENV, ignore_errors=True)     # the rembg venv is code, not data
    # the clone made by the curl one-liner / `wallflow update` is ours to delete;
    # a checkout the user made themselves (anywhere else) is not
    managed_src = paths.DATA_HOME / "wallflow-src"
    if managed_src.is_dir():
        shutil.rmtree(managed_src, ignore_errors=True)
    if purge:
        shutil.rmtree(paths.CONFIG_DIR, ignore_errors=True)
        shutil.rmtree(paths.CACHE_DIR, ignore_errors=True)
        print("removed wallflow, config and cache")
    else:
        print(f"removed wallflow (kept {paths.CONFIG_DIR} and {paths.CACHE_DIR}; --purge deletes them)")
    return 0
