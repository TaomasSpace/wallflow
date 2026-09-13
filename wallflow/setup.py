"""`wallflow setup` — detect the environment, write config, wire up Hyprland.

Idempotent: re-running keeps user-changed values and only fills in what's
missing, unless --redetect is passed.
"""
import os
import subprocess
import sys
from pathlib import Path

from . import config, depth, detect, hypr, overlay, paths, rename as rename_mod, transcode


def _log(msg: str) -> None:
    print(f"  {msg}")


def run(args) -> int:
    fresh = not paths.CONFIG_FILE.exists()
    cfg = config.load()
    redetect = fresh or args.redetect

    # --- wallpaper dir (the one thing worth asking) ------------------------
    if args.wallpaper_dir:
        wdir = Path(args.wallpaper_dir).expanduser()
    elif not fresh and not args.redetect:
        wdir = config.wallpaper_dir(cfg)
    else:
        default = detect.default_wallpaper_dir()
        if args.yes or not sys.stdin.isatty():
            wdir = default
        else:
            ans = input(f"Wallpaper folder [{default}]: ").strip()
            wdir = Path(ans).expanduser() if ans else default
    wdir.mkdir(parents=True, exist_ok=True)
    (wdir / cfg["general"]["hidden_dir_name"]).mkdir(exist_ok=True)
    cfg["general"]["wallpaper_dir"] = str(wdir).replace(str(paths.HOME), "~", 1)
    _log(f"wallpapers   : {wdir}")

    # --- detection ---------------------------------------------------------
    if redetect:
        cfg["image"]["backend"] = detect.image_backend()
        cfg["theme"]["enabled"] = detect.which("wallust")
        if detect.hyprland_running():
            cfg["transcode"]["max_width"] = detect.max_monitor_width(cfg["transcode"]["max_width"])
        cfg["transcode"]["encoder"] = "auto"
    if args.bind:
        cfg["hypr"]["bind"] = args.bind
    if args.no_hypr:
        cfg["hypr"]["manage"] = False

    _log(f"image backend: {cfg['image']['backend']}")
    _log(f"theming      : {'wallust' if cfg['theme']['enabled'] else 'off (wallust not found)'}")
    if cfg["transcode"]["encoder"] == "auto":
        _log("probing video encoders (a few seconds) …")
        enc = transcode.resolve_encoder(cfg, save=False)
        if enc == "none":
            cfg["transcode"]["enabled"] = False
    _log(f"gpu / encoder: {detect.gpu_vendor()} / {cfg['transcode']['encoder']}"
         f"  (max {cfg['transcode']['max_width']}px, {cfg['transcode']['fps']} fps)")
    _log(f"overlay      : {'quickshell found' if overlay.qs_bin() else 'off (quickshell not found)'}"
         f", depth {'ready' if depth.ready() else 'not set up — `wallflow depth setup` (optional)'}")

    config.save(cfg)
    _log(f"config       : {paths.CONFIG_FILE}")
    paths.ensure_dirs()

    # --- hyprland ----------------------------------------------------------
    if cfg["hypr"]["manage"]:
        conflicts = hypr.bind_conflicts(cfg)
        if conflicts:
            print(f"  ! {cfg['hypr']['bind']} is already bound in your config:")
            for c in conflicts[:3]:
                print(f"      {c}")
            print("    change it with:  wallflow setup --bind 'SUPER + SHIFT + W'")
        f = hypr.install(cfg)
        _log(f"hyprland     : autostart + bind ({cfg['hypr']['bind']}) written to {f}")
        hypr.reload()
    else:
        _log("hyprland     : not managed (hypr.manage = false)")

    # --- rename ---------------------------------------------------------
    mode = cfg["rename"]["mode"]
    if mode != 0:
        ops = rename_mod.plan(cfg)
        rename_mod.apply(ops)
        _log(f"rename       : mode {mode} — renamed {len(ops)} file(s)")

    # --- start the fullscreen pauser now (autostart covers next login) -----
    if cfg["video"]["pause_on_fullscreen"] and detect.hyprland_running():
        subprocess.Popen([paths.launcher_cmd(), "pauser"], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # --- (re)start the folder watcher — restart always, to pick up wallpaper_dir
    # / recursive changes; it exits immediately if mode is 0 and prewarm is off ---
    need_watch = mode != 0 or cfg["general"]["auto_prewarm"] or cfg["depth"]["auto"]
    subprocess.run(["pkill", "-f", "wallflow.py watch"], check=False)
    if need_watch:
        subprocess.Popen([paths.launcher_cmd(), "watch"], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # --- overlay: start now if there is something to draw (autostart covers next login)
    if cfg["overlay"]["enabled"] and detect.hyprland_running():
        overlay.start(cfg, log=lambda *_: None)

    missing = [t for t in ("mpvpaper", "ffmpeg") if not detect.which(t)]
    if missing:
        print(f"  ! missing: {', '.join(missing)} — video wallpapers need them")
    if not detect._has_pyside():
        print("  ! PySide6 not importable — the picker UI won't start (see README)")
    return 0


def remove() -> None:
    cfg = config.load()
    if hypr.remove(cfg):
        print("  removed wallflow block from Hyprland config")
        hypr.reload()
    subprocess.run(["pkill", "-f", "wallflow.py watch"], check=False)
    overlay.stop()
