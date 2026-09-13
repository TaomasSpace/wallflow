"""Applying wallpapers. Headless-safe: no Qt imports here.

Images -> caelestia / swww / hyprpaper, then wallust for colours.
Videos -> mpvpaper with an mpv IPC socket. video->video is a live `loadfile`
          swap (no teardown, no flash). Colours come from a still frame.
          If a transcode exists it is played instead of the original; if not,
          the original plays now and a background transcode swaps in when done.
"""
import json
import os
import random
import socket
import subprocess
import sys
import time

from . import addons, config, depth, paths, thumbs, transcode

_QUIET = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --- state -----------------------------------------------------------------

def read_current() -> str:
    try:
        return paths.STATE_FILE.read_text().strip()
    except OSError:
        return ""


def _save_state(path: str) -> None:
    try:
        paths.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        paths.STATE_FILE.write_text(path)
    except OSError:
        pass


def gather_wallpapers(cfg: dict, include_hidden: bool = False) -> list[str]:
    root = config.wallpaper_dir(cfg)
    exts = config.all_exts(cfg)
    hidden_name = cfg["general"]["hidden_dir_name"]
    files = []
    if cfg["general"]["recursive"]:
        for d, dirs, fs in os.walk(root):
            dirs[:] = [x for x in dirs if not x.startswith(".")]
            if not include_hidden and os.path.abspath(d) == os.path.abspath(root):
                dirs[:] = [x for x in dirs if x != hidden_name]
            files += [os.path.join(d, f) for f in fs if f.lower().endswith(exts)]
    elif root.is_dir():
        files = [str(p) for p in root.iterdir() if p.is_file() and p.name.lower().endswith(exts)]
        if include_hidden:
            hdir = root / hidden_name
            if hdir.is_dir():
                files += [str(p) for p in hdir.iterdir() if p.is_file() and p.name.lower().endswith(exts)]
    return sorted(files, key=str.lower)


# --- theming ---------------------------------------------------------------

SEQ_FILE = paths.CACHE_DIR / "sequences"


def _ensure_sequences_template() -> None:
    """Register wallflow's OSC-sequence template with wallust (once / on change)."""
    tpl = paths.WALLUST_TEMPLATES / "wallflow-sequences"
    src = paths.PKG_DIR / "templates" / "sequences"
    if tpl.exists() and tpl.read_bytes() == src.read_bytes() and paths.WALLUST_CONF.exists() \
            and "wallflow-sequences =" in paths.WALLUST_CONF.read_text():
        return
    paths.WALLUST_TEMPLATES.mkdir(parents=True, exist_ok=True)
    tpl.write_bytes(src.read_bytes())
    addons._register("sequences", "wallflow-sequences", "~/.cache/wallflow/sequences")


def terminal_ptys(cfg: dict) -> set[str]:
    """/dev/pts/* slaves belonging to known terminal emulators (theme.terminals).

    The emulator itself only holds the pty *master* (/dev/ptmx), so the slave
    has to be picked up from its descendants (shell, tmux, cava, ...)."""
    names = set(cfg["theme"]["terminals"])
    comm, ppid = {}, {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/stat") as f:
                st = f.read()
            # comm may contain spaces/parens -> split around the last ')'
            i = st.rindex(")")
            comm[pid] = st[st.index("(") + 1:i]
            ppid[pid] = st[i + 2:].split()[1]
        except (OSError, ValueError):
            continue
    ptys = set()
    for pid in comm:
        p = ppid.get(pid)
        while p in comm and p != "1":
            if comm[p] in names:
                break
            p = ppid.get(p)
        else:
            continue
        try:
            for fd in os.listdir(f"/proc/{pid}/fd"):
                try:
                    t = os.readlink(f"/proc/{pid}/fd/{fd}")
                except OSError:
                    continue
                if t.startswith("/dev/pts/"):
                    ptys.add(t)
        except OSError:
            continue
    return ptys


def broadcast(cfg: dict) -> int:
    """Push the rendered sequences into open terminals. Returns #ptys written."""
    mode = cfg["theme"]["broadcast"]
    if mode == "none" or not SEQ_FILE.exists():
        return 0
    data = SEQ_FILE.read_bytes()
    if mode == "all":
        import glob
        targets = set(glob.glob("/dev/pts/[0-9]*"))
    else:
        targets = terminal_ptys(cfg)
    n = 0
    for t in targets:
        try:
            fd = os.open(t, os.O_WRONLY | os.O_NONBLOCK | os.O_NOCTTY)
            os.write(fd, data)
            os.close(fd)
            n += 1
        except OSError:
            pass
    return n


PALETTES = {
    # name: description — wallust's built-in palettes (`wallust run --help`)
    "dark":            "dark bg, 8 colours from the image",
    "dark16":          "dark bg, 16 colours (default)",
    "darkcomp":        "dark bg, complementary accents",
    "darkcomp16":      "dark bg, complementary accents, 16 colours",
    "harddark":        "very dark bg, high contrast",
    "harddark16":      "very dark bg, high contrast, 16 colours",
    "harddarkcomp":    "very dark bg, complementary accents",
    "harddarkcomp16":  "very dark bg, complementary accents, 16 colours",
    "softdark":        "dark bg, muted colours",
    "softdark16":      "dark bg, muted colours, 16 colours",
    "softdarkcomp":    "dark bg, muted, complementary accents",
    "softdarkcomp16":  "dark bg, muted, complementary accents, 16 colours",
    "light":           "light bg, 8 colours",
    "light16":         "light bg, 16 colours",
    "lightcomp":       "light bg, complementary accents",
    "lightcomp16":     "light bg, complementary accents, 16 colours",
    "softlight":       "light bg, muted colours",
    "softlight16":     "light bg, muted colours, 16 colours",
    "softlightcomp":   "light bg, muted, complementary accents",
    "softlightcomp16": "light bg, muted, complementary accents, 16 colours",
    "ansidark":        "keep ANSI hues, tint from image",
    "ansidark16":      "keep ANSI hues, tint from image, 16 colours",
}


def wallust_args(cfg: dict) -> list[str]:
    t = cfg["theme"]
    extra = list(t.get("wallust_args", []))
    args = ["-p", t.get("palette") or "dark16"]
    if t.get("contrast", True) and not ({"-k", "--check-contrast"} & set(extra)):
        args.append("-k")
    return args + extra


def theme(src: str, cfg: dict) -> int:
    if not cfg["theme"]["enabled"] or not _has("wallust"):
        return 0
    _ensure_sequences_template()
    # -s: wallust must NOT broadcast — it hits every pty, including non-terminals
    # (Caelestia shell), which shows the raw escapes as a notification.
    r = subprocess.run(["wallust", "run", "-s", *wallust_args(cfg), src],
                       check=False, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"wallust failed ({r.returncode}): {r.stderr.strip() or r.stdout.strip()}", file=sys.stderr)
        return 0
    n = broadcast(cfg)
    for cmd in addons.reload_commands():
        subprocess.run(cmd, shell=True, check=False, **_QUIET)
    return n


def theme_source(path: str, cfg: dict) -> str:
    """wallust can't read video — theme off the cached still frame."""
    return thumbs.thumb_for(path, cfg) if config.is_video(cfg, path) else path


# --- mpv / mpvpaper --------------------------------------------------------

def mpv_command(*cmd) -> bool:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(str(paths.MPV_SOCKET))
        s.sendall((json.dumps({"command": list(cmd)}) + "\n").encode())
        s.close()
        return True
    except OSError:
        return False


def mpvpaper_running() -> bool:
    return subprocess.run(["pgrep", "-x", "mpvpaper"], **_QUIET).returncode == 0


def kill_mpvpaper() -> None:
    subprocess.run(["pkill", "-x", "mpvpaper"], check=False, **_QUIET)
    try:
        os.unlink(paths.MPV_SOCKET)
    except OSError:
        pass


def launch_mpvpaper(path: str, cfg: dict) -> None:
    kill_mpvpaper()
    opts = f"{cfg['video']['mpv_opts']} input-ipc-server={paths.MPV_SOCKET}"
    subprocess.Popen(["mpvpaper", "-o", opts, cfg["video"]["outputs"], path],
                     start_new_session=True, **_QUIET)


def play_video(path: str, cfg: dict) -> None:
    if not (mpvpaper_running() and mpv_command("loadfile", path)):
        launch_mpvpaper(path, cfg)


# --- image backends --------------------------------------------------------

def set_image(path: str, cfg: dict) -> None:
    be = cfg["image"]["backend"]
    if be == "caelestia":
        subprocess.run(["caelestia", "wallpaper", "-f", path], check=False, **_QUIET)
    elif be == "swww":
        if subprocess.run(["swww", "query"], **_QUIET).returncode != 0:
            subprocess.Popen(["swww-daemon"], start_new_session=True, **_QUIET)
        subprocess.run(["swww", "img", path, "--transition-type", "fade"], check=False, **_QUIET)
    elif be == "hyprpaper":
        subprocess.run(["hyprctl", "hyprpaper", "preload", path], check=False, **_QUIET)
        subprocess.run(["hyprctl", "hyprpaper", "wallpaper", f",{path}"], check=False, **_QUIET)
    else:
        print("no image backend configured (image.backend)", file=sys.stderr)


# --- public ----------------------------------------------------------------

def _spawn_background_transcode(path: str) -> None:
    exe = [sys.executable, str(paths.REPO_DIR / "wallflow.py")]
    subprocess.Popen([*exe, "transcode", "--file", path, "--swap"],
                     start_new_session=True, **_QUIET)


def _spawn_background_cutout(path: str) -> None:
    exe = [sys.executable, str(paths.REPO_DIR / "wallflow.py")]
    subprocess.Popen([*exe, "depth", "--file", path, "--swap"],
                     start_new_session=True, **_QUIET)


def apply(path: str, cfg: dict | None = None, do_theme: bool = True) -> None:
    cfg = cfg or config.load()
    if path.startswith("file://"):
        from urllib.parse import unquote, urlparse
        path = unquote(urlparse(path).path)
    path = os.path.abspath(path)
    _save_state(path)   # FIRST: caelestia re-sources hyprland.lua -> `restore` runs mid-apply
    if config.is_video(cfg, path):
        still = theme_source(path, cfg)
        if cfg["image"]["backend"] == "caelestia":
            # caelestia derives the shell scheme (bar, launcher, …) from *its*
            # wallpaper — feed it the still frame so videos recolour the shell too.
            set_image(still, cfg)
        if do_theme:
            theme(still, cfg)
        src = transcode.existing(path, cfg)
        if not src and transcode.wanted(path, cfg) and not transcode.codec_compatible(path, cfg):
            # a GIF or a codec mpvpaper can't reliably loop (vp9/av1/…) — playing it
            # raw wouldn't just be heavier, it'd look static/broken. Wait for the
            # one-off transcode (or someone else's already in flight) instead.
            out = transcode.run(path, cfg, wait=True)
            src = str(out) if out else path
        elif not src:
            src = path
            if transcode.wanted(path, cfg):
                _spawn_background_transcode(path)
        play_video(src, cfg)
    else:
        kill_mpvpaper()
        set_image(path, cfg)
        if do_theme:
            theme(path, cfg)
        if depth.wanted(path, cfg):
            _spawn_background_cutout(path)   # overlay swaps the cutout in when it's done
    _save_state(path)
    from . import overlay
    overlay.ensure(cfg)                      # widgets show now; cutout (if cached) too


_BACKEND_LAYERS = {"caelestia": "caelestia-background", "swww": "swww-daemon", "hyprpaper": "hyprpaper"}


def _layer_present(namespace: str) -> bool:
    r = subprocess.run(["hyprctl", "layers"], capture_output=True, text=True)
    return r.returncode == 0 and f"namespace: {namespace}" in r.stdout


def _wait_for_desktop(cfg: dict) -> None:
    """Block until outputs exist and the image backend's background layer is up,
    so mpvpaper's layer is created on top of it (login race)."""
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return
    ns = cfg["video"]["wait_for_layer"]
    if ns == "auto":
        ns = _BACKEND_LAYERS.get(cfg["image"]["backend"], "")
    deadline = time.monotonic() + float(cfg["video"]["restore_timeout"])
    while time.monotonic() < deadline:
        r = subprocess.run(["hyprctl", "monitors", "-j"], capture_output=True, text=True)
        have_outputs = r.returncode == 0 and r.stdout.strip() not in ("", "[]")
        if have_outputs and (not ns or _layer_present(ns)):
            break
        time.sleep(0.5)
    time.sleep(float(cfg["video"]["restore_delay"]))


def restore(cfg: dict | None = None) -> None:
    """Re-apply the last wallpaper. Runs at login, after every Hyprland reload
    (caelestia triggers one per image change) and after resume — so it must be
    idempotent: never spawn a second mpvpaper, always kill a stale one."""
    cfg = cfg or config.load()
    path = read_current()
    if not path or not os.path.exists(path):
        return
    from . import overlay
    if config.is_video(cfg, path):
        src = transcode.existing(path, cfg) or path
        if mpvpaper_running() and mpv_command("loadfile", src):
            overlay.ensure(cfg)
            return                                   # already up — just make sure it's this file
        _wait_for_desktop(cfg)                       # login: don't get buried under the bg layer
        launch_mpvpaper(src, cfg)
    else:
        kill_mpvpaper()                              # a video layer must never outlive an image
        if cfg["image"]["backend"] != "caelestia":   # caelestia restores its own
            set_image(path, cfg)
    overlay.ensure(cfg)


def reattach(cfg: dict | None = None) -> None:
    """After resume/hotplug mpvpaper's layer on a re-added output stays dead
    (alpha 0). Only videos need this: kill + relaunch so it binds all outputs."""
    cfg = cfg or config.load()
    path = read_current()
    if path and os.path.exists(path) and config.is_video(cfg, path):
        launch_mpvpaper(transcode.existing(path, cfg) or path, cfg)


def swap_if_current(original: str, transcoded: str) -> None:
    """Called by the background transcode: hot-swap only if still the active wallpaper."""
    if read_current() == original:
        mpv_command("loadfile", transcoded)


def cutout_ready(original: str, cfg: dict) -> None:
    """Called by the background cutout: the overlay picks it up if still current."""
    if read_current() == original:
        from . import overlay
        overlay.write_state(cfg, original)


def step(cfg: dict, delta: int = 1) -> str | None:
    files = gather_wallpapers(cfg)
    if not files:
        return None
    cur = read_current()
    i = files.index(cur) if cur in files else -1
    nxt = files[(i + delta) % len(files)]
    apply(nxt, cfg)
    return nxt


def pick_random(cfg: dict) -> str | None:
    files = [f for f in gather_wallpapers(cfg) if f != read_current()]
    if not files:
        return None
    f = random.choice(files)
    apply(f, cfg)
    return f


def _has(name: str) -> bool:
    from shutil import which
    return which(name) is not None
