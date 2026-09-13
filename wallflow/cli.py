"""wallflow command line.

  wallflow                     open the picker
  wallflow ui --all            open the picker, including the hidden folder
  wallflow next|prev|random    cycle without the UI
  wallflow apply <file>        set a wallpaper directly
  wallflow restore             re-apply last wallpaper (autostart)
  wallflow pauser              fullscreen watcher (autostart)
  wallflow transcode           pre-transcode every video wallpaper
  wallflow depth               3D-cutout status (setup, model, how many done/pending)
  wallflow depth setup [--gpu] install the segmentation venv (rembg) for subject cutouts
  wallflow depth all [-y]      segment every image wallpaper now (slow — it warns and shows ETA)
  wallflow depth on|off [file] switch the cutout on/off per image (default: current wallpaper)
  wallflow overlay start|stop|restart|status   the widget/subject layer (quickshell)
  wallflow overlay edit|done   drag widgets around with the mouse / stop
  wallflow rename [--dry-run]  rename wallpapers per [rename].mode
  wallflow theme               re-run wallust + addon reloads
  wallflow theme list          show wallust palettes (current marked)
  wallflow theme set <name>    switch palette, save + re-theme
  wallflow addons …            list / install / remove / info
  wallflow config …            show / get / set / edit / path
  wallflow setup               (re)detect + wire up Hyprland
  wallflow doctor              show what was detected
  wallflow update [--check]    pull the latest version from git and reinstall
"""
import argparse
import json
import os
import subprocess
import sys

from . import __version__, addons, backend, config, depth, overlay, paths, rename, transcode


def _cmd_ui(a, cfg):
    from . import hypr, ui
    if cfg["ui"]["close_special_workspaces"]:
        hypr.close_special_workspaces(cfg)
    pinned = hypr.unpin_pinned_windows() if cfg["ui"]["hide_pinned_windows"] else []
    try:
        return ui.run(cfg, include_hidden=getattr(a, "all", False))
    finally:
        hypr.restore_pinned_windows(pinned)


def _cmd_apply(a, cfg):
    p = os.path.expanduser(a.file)
    if not os.path.exists(p):
        sys.exit(f"no such file: {p}")
    backend.apply(p, cfg)


def _cmd_step(delta):
    def f(a, cfg):
        r = backend.step(cfg, delta)
        print(r or "no wallpapers found")
    return f


def _cmd_random(a, cfg):
    print(backend.pick_random(cfg) or "no wallpapers found")


def _cmd_restore(a, cfg):
    backend.restore(cfg)


def _cmd_reattach(a, cfg):
    backend.reattach(cfg)


def _cmd_pauser(a, cfg):
    from . import pauser
    pauser.main()


def _cmd_theme(a, cfg):
    action = getattr(a, "action", None)
    if action == "list":
        cur = cfg["theme"]["palette"]
        for n, d in backend.PALETTES.items():
            print(f"{'*' if n == cur else ' '} {n:16} {d}")
        return
    if action == "set":
        if a.name not in backend.PALETTES:
            sys.exit(f"unknown palette {a.name!r} — see `wallflow theme list`")
        cfg["theme"]["palette"] = a.name
        config.save(cfg)
        print(f"palette = {a.name}")
    cur = backend.read_current()
    if not cur:
        sys.exit("no wallpaper set yet")
    n = backend.theme(backend.theme_source(cur, cfg), cfg)
    print(f"themed from {cur}; sequences pushed to {n} terminal pty(s)")


def _cmd_transcode(a, cfg):
    transcode.resolve_encoder(cfg)
    if a.prune:
        print(f"pruned {transcode.prune(cfg)} stale file(s)")
        return
    if a.file:
        src = os.path.abspath(os.path.expanduser(a.file))
        out = transcode.run(src, cfg, force=a.force, quiet=not a.verbose)
        if out and a.swap:
            backend.swap_if_current(src, str(out))
        print(out or "not needed / failed")
        return
    transcode.run_all(cfg, force=a.force)


def _cmd_depth(a, cfg):
    if a.action == "setup":
        return depth.setup(gpu=a.gpu, python=a.python)
    if a.prune:
        print(f"pruned {depth.prune(cfg)} stale file(s)")
        return
    if a.action in ("on", "off"):
        src = os.path.abspath(os.path.expanduser(a.name)) if a.name else backend.read_current()
        if not src:
            sys.exit("no wallpaper set and no file given")
        depth.set_enabled(src, a.action == "on")
        print(f"3D cutout {a.action} for {os.path.basename(src)}")
        if a.action == "on" and depth.wanted(src, cfg) and src == backend.read_current():
            backend._spawn_background_cutout(src)
        overlay.ensure(cfg)                # hides / shows it live
        return
    if a.file:
        src = os.path.abspath(os.path.expanduser(a.file))
        bg = a.swap and cfg["depth"]["notify"] and depth.wanted(src, cfg)
        if bg:                             # background job from apply(): tell the user why the fans spin
            depth.notify("Creating 3D cutout", f"{os.path.basename(src)} — this can take a while on CPU")
        out = depth.run(src, cfg, force=a.force, quiet=not a.verbose)
        if out and a.swap:
            backend.cutout_ready(src, cfg)
        if bg:
            depth.notify("3D cutout ready" if out else "No subject found",
                         os.path.basename(src) + ("" if out else " — widgets stay on top"))
        print(out or "no subject found / not set up")
        return
    if a.action == "all":
        rc = depth.run_all(cfg, force=a.force, yes=a.yes)
        overlay.write_state(cfg)           # the current wallpaper's cutout may just have appeared
        return rc
    print(depth.status_text(cfg))


def _cmd_overlay(a, cfg):
    if a.action == "edit":
        on = overlay.edit(cfg)
        print("edit mode on — drag widgets; Esc or `wallflow overlay done` to finish" if on else "edit mode off")
    elif a.action == "done":
        overlay.edit(cfg, on=False)
        print("edit mode off")
    elif a.action == "start":
        print("overlay running" if overlay.start(cfg) else "overlay not started")
    elif a.action == "stop":
        print("stopped" if overlay.stop() else "not running")
    elif a.action == "restart":
        print("overlay running" if overlay.restart(cfg) else "overlay not started")
    else:
        print(overlay.status_text(cfg))


def _cmd_watch(a, cfg):
    from . import watcher
    watcher.main()


def _cmd_rename(a, cfg):
    ops = rename.plan(cfg)
    if not ops:
        print("mode 0 (off) or nothing to do")
        return
    if a.dry_run:
        for old, new in ops:
            print(f"{old} -> {new}")
        return
    rename.apply(ops)
    print(f"renamed {len(ops)} file(s)")


def _cmd_addons(a, cfg):
    if a.action == "list":
        print(addons.list_text())
    elif a.action == "info":
        print(addons.info_text(a.name))
    elif a.action == "install":
        av = addons.available()
        for n in a.name.split(","):
            n = n.strip()
            if not addons.install(n):
                continue
            if av[n].get("template") and backend.read_current():
                _cmd_theme(a, cfg)        # render colours right away
            if av[n].get("widget"):
                overlay.start(cfg)        # show it right away (also refreshes a running overlay)
    elif a.action == "remove":
        if a.name == "all":
            addons.remove_all(purge=a.purge)
        else:
            for n in a.name.split(","):
                addons.remove(n.strip(), purge=a.purge)
        overlay.write_state(cfg)
    elif a.action == "refresh":
        if addons.refresh() and backend.read_current():
            _cmd_theme(a, cfg)
        else:
            print("addons up to date")


def _cmd_config(a, cfg):
    if a.action == "show":
        print(config.dumps(cfg))
    elif a.action == "path":
        print(paths.CONFIG_FILE)
    elif a.action == "get":
        print(config.get(cfg, a.key))
    elif a.action == "set":
        pairs = [a.key, a.value, *(a.more or [])]
        if len(pairs) % 2:
            sys.exit("config set takes key/value pairs")
        keys = pairs[::2]
        for k, v in zip(keys, pairs[1::2]):
            config.set_value(cfg, k, v)
        config.save(cfg)
        for k in keys:
            print(f"{k} = {config.get(cfg, k)}")
        if any(k.startswith("hypr.") for k in keys):
            from . import hypr
            hypr.install(config.load()); hypr.reload()
        if any(k.startswith(("overlay.", "depth.")) for k in keys):
            overlay.ensure(config.load())      # live: the overlay watches its state file
        if any(k.startswith("depth.") for k in keys):
            cur = backend.read_current()       # retune -> recompute the current cutout right away
            if cur and depth.wanted(cur, config.load()):
                backend._spawn_background_cutout(cur)
                print("  recomputing the cutout for the current wallpaper in the background")
        if "depth.auto" in keys and cfg["depth"]["auto"]:
            print("! depth.auto: `wallflow watch` will now segment every new image in the background "
                  "(and any image still missing a cutout) — slow on CPU, you'll get a notification "
                  "while it runs. Restarting the watcher.")
            subprocess.run(["pkill", "-f", "wallflow.py watch"], check=False)
            try:
                subprocess.Popen([paths.launcher_cmd(), "watch"], start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                print("  (start it with `wallflow watch` — launcher not found)")
    elif a.action == "edit":
        if not paths.CONFIG_FILE.exists():
            config.save(cfg)
        subprocess.call([os.environ.get("EDITOR", "nano"), str(paths.CONFIG_FILE)])


def _cmd_setup(a, cfg):
    from . import setup
    if a.remove:
        setup.remove()
        return
    return setup.run(a)


def _cmd_update(a, cfg):
    from . import update
    return update.run(check_only=a.check)


def _cmd_uninstall(a, cfg):
    from . import uninstall
    return uninstall.run(purge=a.purge, yes=a.yes)


def _cmd_doctor(a, cfg):
    from . import detect
    s = detect.summary()
    s["config"] = str(paths.CONFIG_FILE) + ("" if paths.CONFIG_FILE.exists() else " (missing — run setup)")
    s["wallpaper_dir"] = str(config.wallpaper_dir(cfg))
    s["wallpapers"] = len(backend.gather_wallpapers(cfg))
    s["current"] = backend.read_current() or "-"
    s["encoder"] = cfg["transcode"]["encoder"]
    s["addons_installed"] = sorted(addons.installed())
    s["terminal_ptys"] = sorted(backend.terminal_ptys(cfg))
    s["depth"] = "ready" if depth.ready() else "not set up (wallflow depth setup)"
    s["overlay"] = "running" if overlay.running() else "not running"
    print(json.dumps(s, indent=2))


def build_parser():
    p = argparse.ArgumentParser(prog="wallflow", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"wallflow {__version__}")
    sp = p.add_subparsers(dest="cmd")

    x = sp.add_parser("ui")
    x.add_argument("--all", action="store_true", help="also show wallpapers in the hidden folder")
    x.set_defaults(fn=_cmd_ui)
    x = sp.add_parser("apply"); x.add_argument("file"); x.set_defaults(fn=_cmd_apply)
    sp.add_parser("next").set_defaults(fn=_cmd_step(1))
    sp.add_parser("prev").set_defaults(fn=_cmd_step(-1))
    sp.add_parser("random").set_defaults(fn=_cmd_random)
    sp.add_parser("restore").set_defaults(fn=_cmd_restore)
    sp.add_parser("reattach", help="relaunch mpvpaper on all outputs (after resume)").set_defaults(fn=_cmd_reattach)
    sp.add_parser("pauser").set_defaults(fn=_cmd_pauser)
    sp.add_parser("watch", help="watch the wallpaper folder and auto-rename on change (autostart)").set_defaults(fn=_cmd_watch)
    x = sp.add_parser("theme")
    x.add_argument("action", nargs="?", choices=["list", "set"])
    x.add_argument("name", nargs="?", help="palette name (for set)")
    x.set_defaults(fn=_cmd_theme)

    x = sp.add_parser("transcode")
    x.add_argument("--file", help="one file instead of the whole folder")
    x.add_argument("--swap", action="store_true", help="hot-swap into mpvpaper when done (used internally)")
    x.add_argument("--force", action="store_true")
    x.add_argument("--prune", action="store_true", help="delete stale transcodes")
    x.add_argument("-v", "--verbose", action="store_true")
    x.set_defaults(fn=_cmd_transcode)

    x = sp.add_parser("depth", help="3D subject cutouts for the overlay (rembg in its own venv)")
    x.add_argument("action", nargs="?", choices=["status", "setup", "all", "on", "off"])
    x.add_argument("name", nargs="?", help="on/off: image file (default: current wallpaper)")
    x.add_argument("--yes", "-y", action="store_true", help="all: skip the 'this takes long' prompt")
    x.add_argument("--gpu", action="store_true", help="setup: onnxruntime-gpu (needs CUDA/cuDNN)")
    x.add_argument("--python", default="", help="setup: interpreter for the venv, e.g. python3.12")
    x.add_argument("--file", help="one image instead of the whole folder")
    x.add_argument("--swap", action="store_true", help="hand the cutout to the overlay when done (internal)")
    x.add_argument("--force", action="store_true")
    x.add_argument("--prune", action="store_true", help="delete stale cutouts")
    x.add_argument("-v", "--verbose", action="store_true")
    x.set_defaults(fn=_cmd_depth)

    x = sp.add_parser("overlay", help="widget + subject layer between wallpaper and windows")
    x.add_argument("action", nargs="?", default="status",
                   choices=["start", "stop", "restart", "status", "edit", "done"])
    x.set_defaults(fn=_cmd_overlay)
    sp.add_parser("3d", help="alias for `wallflow depth all`").set_defaults(
        fn=lambda a, cfg: _cmd_depth(argparse.Namespace(action="all", name=None, yes=False, gpu=False,
                                                         python="", file=None, swap=False, force=False,
                                                         prune=False, verbose=False), cfg))

    x = sp.add_parser("rename", help="rename wallpapers per [rename].mode (0 off / 1 prefix / 2 full)")
    x.add_argument("--dry-run", action="store_true", help="print planned renames without touching files")
    x.set_defaults(fn=_cmd_rename)

    x = sp.add_parser("addons")
    x.add_argument("action", choices=["list", "install", "remove", "info", "refresh"])
    x.add_argument("name", nargs="?", help="addon name (comma-separate for several; 'all' for remove)")
    x.add_argument("--purge", action="store_true",
                   help="remove: delete the rendered file + backup instead of restoring the original")
    x.set_defaults(fn=_cmd_addons)

    x = sp.add_parser("config")
    x.add_argument("action", nargs="?", default="show", choices=["show", "get", "set", "edit", "path"])
    x.add_argument("key", nargs="?"); x.add_argument("value", nargs="?")
    x.add_argument("more", nargs="*", help="further key value pairs (set)")
    x.set_defaults(fn=_cmd_config)

    x = sp.add_parser("setup")
    x.add_argument("--wallpaper-dir")
    x.add_argument("--bind", help="e.g. 'SUPER + W'")
    x.add_argument("--yes", "-y", action="store_true", help="no prompts")
    x.add_argument("--redetect", action="store_true", help="re-run detection over existing config")
    x.add_argument("--no-hypr", action="store_true", help="don't touch the Hyprland config")
    x.add_argument("--remove", action="store_true", help="remove the Hyprland block")
    x.set_defaults(fn=_cmd_setup)

    sp.add_parser("doctor").set_defaults(fn=_cmd_doctor)
    x = sp.add_parser("update")
    x.add_argument("--check", action="store_true", help="only report whether an update exists")
    x.set_defaults(fn=_cmd_update)
    x = sp.add_parser("uninstall", help="remove wallflow (addons, Hyprland block, launcher, code)")
    x.add_argument("--purge", action="store_true", help="also delete config, cache and addon backups")
    x.add_argument("--yes", "-y", action="store_true", help="no confirmation prompt")
    x.set_defaults(fn=_cmd_uninstall)
    return p


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)
    cfg = config.load()
    if a.cmd is None:
        a.fn = _cmd_ui
    if a.cmd == "theme" and a.action == "set" and not a.name:
        p.error("palette name required — see `wallflow theme list`")
    if a.cmd == "addons" and a.action not in ("list", "refresh") and not a.name:
        p.error("addon name required")
    if a.cmd == "config" and a.action in ("get", "set") and not a.key:
        p.error("config key required (e.g. transcode.fps)")
    if a.cmd == "config" and a.action == "set" and a.value is None:
        p.error("value required")
    rc = a.fn(a, cfg)
    sys.exit(rc or 0)
