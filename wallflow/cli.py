"""wallflow command line.

  wallflow                     open the picker
  wallflow next|prev|random    cycle without the UI
  wallflow apply <file>        set a wallpaper directly
  wallflow restore             re-apply last wallpaper (autostart)
  wallflow pauser              fullscreen watcher (autostart)
  wallflow transcode           pre-transcode every video wallpaper
  wallflow theme               re-run wallust + addon reloads
  wallflow addons …            list / install / remove / info
  wallflow config …            show / get / set / edit / path
  wallflow setup               (re)detect + wire up Hyprland
  wallflow doctor              show what was detected
"""
import argparse
import json
import os
import subprocess
import sys

from . import __version__, addons, backend, config, paths, transcode


def _cmd_ui(a, cfg):
    from . import ui
    return ui.run(cfg)


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
    cur = backend.read_current()
    if not cur:
        sys.exit("no wallpaper set yet")
    backend.theme(backend.theme_source(cur, cfg), cfg)


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


def _cmd_addons(a, cfg):
    if a.action == "list":
        print(addons.list_text())
    elif a.action == "info":
        print(addons.info_text(a.name))
    elif a.action == "install":
        for n in a.name.split(","):
            if addons.install(n.strip()) and backend.read_current():
                _cmd_theme(a, cfg)        # render colours right away
    elif a.action == "remove":
        for n in a.name.split(","):
            addons.remove(n.strip())


def _cmd_config(a, cfg):
    if a.action == "show":
        print(config.dumps(cfg))
    elif a.action == "path":
        print(paths.CONFIG_FILE)
    elif a.action == "get":
        print(config.get(cfg, a.key))
    elif a.action == "set":
        config.save(config.set_value(cfg, a.key, a.value))
        print(f"{a.key} = {config.get(config.load(), a.key)}")
        if a.key.startswith("hypr."):
            from . import hypr
            hypr.install(config.load()); hypr.reload()
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


def _cmd_doctor(a, cfg):
    from . import detect
    s = detect.summary()
    s["config"] = str(paths.CONFIG_FILE) + ("" if paths.CONFIG_FILE.exists() else " (missing — run setup)")
    s["wallpaper_dir"] = str(config.wallpaper_dir(cfg))
    s["wallpapers"] = len(backend.gather_wallpapers(cfg))
    s["current"] = backend.read_current() or "-"
    s["encoder"] = cfg["transcode"]["encoder"]
    s["addons_installed"] = sorted(addons.installed())
    print(json.dumps(s, indent=2))


def build_parser():
    p = argparse.ArgumentParser(prog="wallflow", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"wallflow {__version__}")
    sp = p.add_subparsers(dest="cmd")

    sp.add_parser("ui").set_defaults(fn=_cmd_ui)
    x = sp.add_parser("apply"); x.add_argument("file"); x.set_defaults(fn=_cmd_apply)
    sp.add_parser("next").set_defaults(fn=_cmd_step(1))
    sp.add_parser("prev").set_defaults(fn=_cmd_step(-1))
    sp.add_parser("random").set_defaults(fn=_cmd_random)
    sp.add_parser("restore").set_defaults(fn=_cmd_restore)
    sp.add_parser("reattach", help="relaunch mpvpaper on all outputs (after resume)").set_defaults(fn=_cmd_reattach)
    sp.add_parser("pauser").set_defaults(fn=_cmd_pauser)
    sp.add_parser("theme").set_defaults(fn=_cmd_theme)

    x = sp.add_parser("transcode")
    x.add_argument("--file", help="one file instead of the whole folder")
    x.add_argument("--swap", action="store_true", help="hot-swap into mpvpaper when done (used internally)")
    x.add_argument("--force", action="store_true")
    x.add_argument("--prune", action="store_true", help="delete stale transcodes")
    x.add_argument("-v", "--verbose", action="store_true")
    x.set_defaults(fn=_cmd_transcode)

    x = sp.add_parser("addons")
    x.add_argument("action", choices=["list", "install", "remove", "info"])
    x.add_argument("name", nargs="?", help="addon name (comma-separate for several)")
    x.set_defaults(fn=_cmd_addons)

    x = sp.add_parser("config")
    x.add_argument("action", nargs="?", default="show", choices=["show", "get", "set", "edit", "path"])
    x.add_argument("key", nargs="?"); x.add_argument("value", nargs="?")
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
    return p


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)
    cfg = config.load()
    if a.cmd is None:
        a.fn = _cmd_ui
    if a.cmd == "addons" and a.action != "list" and not a.name:
        p.error("addon name required")
    if a.cmd == "config" and a.action in ("get", "set") and not a.key:
        p.error("config key required (e.g. transcode.fps)")
    if a.cmd == "config" and a.action == "set" and a.value is None:
        p.error("value required")
    rc = a.fn(a, cfg)
    sys.exit(rc or 0)
