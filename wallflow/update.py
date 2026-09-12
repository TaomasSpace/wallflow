"""`wallflow update` — pull the latest version and reinstall.

install.sh records the checkout it ran from in ~/.local/share/wallflow/.source.
If that checkout is gone, we clone the recorded URL to ~/.local/share/wallflow-src.
"""
import subprocess
import sys
from pathlib import Path

from . import __version__, config, paths

SOURCE_FILE = paths.INSTALL_DIR / ".source"
FALLBACK_SRC = paths.DATA_HOME / "wallflow-src"
DEFAULT_URL = "https://github.com/TaomasSpace/wallflow"


def _source() -> tuple[Path | None, str]:
    path, url = None, DEFAULT_URL
    if SOURCE_FILE.exists():
        for line in SOURCE_FILE.read_text().splitlines():
            k, _, v = line.partition("=")
            if k == "path" and v:
                path = Path(v)
            elif k == "url" and v:
                url = v
    return path, url


def _git(repo: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def run(check_only: bool = False) -> int:
    src, url = _source()
    if not src or not (src / ".git").is_dir():
        if check_only:
            print("no git checkout recorded; run `wallflow update` to clone one")
            return 1
        src = FALLBACK_SRC
        if not (src / ".git").is_dir():
            print(f"cloning {url} -> {src}")
            r = subprocess.run(["git", "clone", "--depth", "1", url, str(src)])
            if r.returncode:
                return r.returncode

    before = _git(src, "rev-parse", "--short", "HEAD").stdout.strip()
    r = _git(src, "fetch", "--quiet")
    if r.returncode:
        print(r.stderr.strip() or "git fetch failed")
        return 1
    ref = "@{u}"
    if _git(src, "rev-parse", "--verify", "-q", ref).returncode:      # no tracking branch set
        branch = _git(src, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        ref = f"origin/{branch}"
    upstream = _git(src, "rev-parse", "--short", ref).stdout.strip()
    if check_only:
        print(f"installed {__version__} @ {before}, remote @ {upstream}"
              + ("  (up to date)" if before == upstream else "  (update available)"))
        return 0
    if before == upstream:
        print(f"already up to date ({before})")
    else:
        # remote may have been force-pushed: hard-align to upstream, but refuse
        # to throw away uncommitted local edits
        if _git(src, "status", "--porcelain", "--untracked-files=no").stdout.strip():
            print(f"{src} has uncommitted changes — commit/stash them, then rerun")
            return 1
        r = _git(src, "reset", "--hard", "--quiet", ref)
        if r.returncode:
            print(r.stderr.strip())
            return 1
        print(f"updated {before} -> {upstream}")
    print("reinstalling …")
    r = subprocess.run(["bash", str(src / "install.sh"), "--no-deps", "--yes"])
    if r.returncode == 0:
        # re-install addons whose files changed (runs the freshly installed code)
        subprocess.run([str(paths.LAUNCHER), "addons", "refresh"], check=False)
        subprocess.run(["pkill", "-f", "wallflow.py pauser"], check=False)
        subprocess.Popen([str(paths.LAUNCHER), "pauser"], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cfg = config.load()
        subprocess.run(["pkill", "-f", "wallflow.py watch"], check=False)
        if cfg["rename"]["mode"] != 0:
            subprocess.Popen([str(paths.LAUNCHER), "watch"], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode
