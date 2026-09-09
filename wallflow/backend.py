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

from . import addons, config, paths, thumbs, transcode

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


def gather_wallpapers(cfg: dict) -> list[str]:
    root = config.wallpaper_dir(cfg)
    exts = config.all_exts(cfg)
    files = []
    if cfg["general"]["recursive"]:
        for d, dirs, fs in os.walk(root):
            dirs[:] = [x for x in dirs if not x.startswith(".")]
            files += [os.path.join(d, f) for f in fs if f.lower().endswith(exts)]
    elif root.is_dir():
        files = [str(p) for p in root.iterdir() if p.is_file() and p.name.lower().endswith(exts)]
    return sorted(files, key=str.lower)


# --- theming ---------------------------------------------------------------

def theme_engine(cfg: dict) -> str:
    e = cfg["theme"]["engine"]
    if e == "auto":
        e = "caelestia" if cfg["image"]["backend"] == "caelestia" else "wallust"
    return e


SEQ_FILE = paths.CACHE_DIR / "sequences"


def _ensure_sequences_template() -> None:
    """Register wallflow's OSC-sequence template with wallust (once)."""
    tpl = paths.WALLUST_TEMPLATES / "wallflow-sequences"
    src = paths.PKG_DIR / "templates" / "sequences"
    if tpl.exists() and tpl.read_bytes() == src.read_bytes():
        return
    paths.WALLUST_TEMPLATES.mkdir(parents=True, exist_ok=True)
    tpl.write_bytes(src.read_bytes())
    addons._register("sequences", "wallflow-sequences", "~/.cache/wallflow/sequences")


def terminal_ptys(cfg: dict) -> set[str]:
    """/dev/pts/* held open by known terminal emulators."""
    names = set(cfg["theme"]["terminals"])
    ptys = set()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm") as f:
                if f.read().strip() not in names:
                    continue
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


def theme(src: str, cfg: dict, via_backend: bool = True) -> None:
    """Recolour from image `src`. `via_backend`=False when the image backend
    already themed (it just set this exact image)."""
    if not cfg["theme"]["enabled"]:
        return
    engine = theme_engine(cfg)
    if engine == "caelestia" and via_backend and _has("caelestia"):
        subprocess.run(["caelestia", "wallpaper", "-f", src], check=False, **_QUIET)
    if _has("wallust") and (engine == "wallust" or addons.installed()):
        # wallust never broadcasts itself (-s): it would also hit non-terminal ptys
        # (Caelestia shell -> stray notification). We render the sequences through a
        # template and push them only to terminal emulators.
        _ensure_sequences_template()
        subprocess.run(["wallust", "run", "-s", *cfg["theme"]["wallust_args"], src],
                       check=False, **_QUIET)
        if engine == "wallust":
            broadcast(cfg)
    for cmd in addons.reload_commands():
        subprocess.run(cmd, shell=True, check=False, **_QUIET)


def theme_source(path: str, cfg: dict) -> str:
    """wallust/caelestia can't read video — use the cached still frame."""
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


def apply(path: str, cfg: dict | None = None, do_theme: bool = True) -> None:
    cfg = cfg or config.load()
    if path.startswith("file://"):
        from urllib.parse import unquote, urlparse
        path = unquote(urlparse(path).path)
    path = os.path.abspath(path)
    if config.is_video(cfg, path):
        if do_theme:
            theme(theme_source(path, cfg), cfg)          # caelestia: still frame + scheme
        src = transcode.existing(path, cfg) or path
        play_video(src, cfg)
        if src == path and transcode.wanted(path, cfg):
            _spawn_background_transcode(path)
    else:
        kill_mpvpaper()
        set_image(path, cfg)                              # caelestia themes here already
        if do_theme:
            theme(path, cfg, via_backend=False)
    _save_state(path)


def restore(cfg: dict | None = None) -> None:
    """Re-apply the last wallpaper at login (video: no re-theme, it's cheap)."""
    cfg = cfg or config.load()
    path = read_current()
    if not path or not os.path.exists(path):
        return
    if config.is_video(cfg, path):
        launch_mpvpaper(transcode.existing(path, cfg) or path, cfg)
    elif cfg["image"]["backend"] != "caelestia":     # caelestia restores its own
        set_image(path, cfg)


def swap_if_current(original: str, transcoded: str) -> None:
    """Called by the background transcode: hot-swap only if still the active wallpaper."""
    if read_current() == original:
        mpv_command("loadfile", transcoded)


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
