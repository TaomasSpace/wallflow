"""Addons = wallust templates that recolour terminal apps from the wallpaper palette.

Each addon is a folder in addons/ with an addon.toml:

    name        = "kitty"
    description = "…"
    requires    = ["kitty"]                   # binaries that should exist (warn only)
    template    = "wallflow-colors.conf"      # file in the addon folder (optional if bin is set)
    target      = "~/.config/kitty/wallflow-colors.conf"   # where wallust renders it
    reload      = "pkill -USR1 kitty"         # run after every wallpaper change (optional)
    note        = "…"                         # printed after install (optional)
    warning     = "…"                         # printed with a "!" after install and in `info` (optional)
    bin         = "wallflow-pipes"            # script in the addon folder -> ~/.local/bin (optional)
    system_bin  = true                        # also -> /usr/local/bin via sudo, so it shadows
                                              # /usr/bin for processes that don't have ~/.local/bin
                                              # in PATH (Hyprland exec, autostart hooks) (optional)
    [include]                                 # hook the rendered file into the app's config
    file     = "~/.config/kitty/kitty.conf"
    line     = "include wallflow-colors.conf"
    position = "bottom"                       # top | bottom
    replace  = "^color_theme\\s*="            # regex: replace a matching line instead (optional)
    conflict = "^\\[general\\]"               # regex: if it matches, don't touch — print hint (optional)

Install = copy template into ~/.config/wallust/templates, register it in
wallust.toml under [templates], patch the include, record what was done in
~/.config/wallflow/addons/<name>.json so `remove` can undo it precisely.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

from . import paths

PREFIX = "wallflow-"
BIN_DIR = Path.home() / ".local" / "bin"
SYSTEM_BIN_DIR = Path("/usr/local/bin")


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(p))


def available() -> dict[str, dict]:
    out = {}
    for d in sorted(paths.ADDONS_DIR.iterdir()) if paths.ADDONS_DIR.is_dir() else []:
        meta = d / "addon.toml"
        if meta.is_file():
            with open(meta, "rb") as f:
                a = tomllib.load(f)
            a["_dir"] = d
            out[a["name"]] = a
    return out


def installed() -> dict[str, dict]:
    out = {}
    if paths.ADDON_STATE_DIR.is_dir():
        for f in paths.ADDON_STATE_DIR.glob("*.json"):
            out[f.stem] = json.loads(f.read_text())
    return out


def reload_commands() -> list[str]:
    return [s["reload"] for s in installed().values() if s.get("reload")]


# --- wallust.toml ----------------------------------------------------------

def _wallust_entry(name: str, template_name: str, target: str) -> str:
    return f'{PREFIX}{name} = {{ template = "{template_name}", target = "{target}" }}'


def _digest(a: dict) -> str:
    """Hash of the addon's source files — install() stores it, refresh() compares it."""
    h = hashlib.sha256()
    for f in sorted(p for p in a["_dir"].iterdir() if p.is_file()):
        h.update(f.name.encode()); h.update(f.read_bytes())
    return h.hexdigest()[:16]


def _register(name: str, template_name: str, target: str) -> None:
    paths.WALLUST_DIR.mkdir(parents=True, exist_ok=True)
    text = paths.WALLUST_CONF.read_text() if paths.WALLUST_CONF.exists() else ""
    lines = [l for l in text.splitlines() if not l.startswith(f"{PREFIX}{name} =")]
    entry = _wallust_entry(name, template_name, target)
    for i, l in enumerate(lines):
        if l.strip() == "[templates]":
            lines.insert(i + 1, entry)
            break
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines += ["[templates]", entry]
    paths.WALLUST_CONF.write_text("\n".join(lines) + "\n")


def _unregister(name: str) -> None:
    if not paths.WALLUST_CONF.exists():
        return
    lines = [l for l in paths.WALLUST_CONF.read_text().splitlines()
             if not l.startswith(f"{PREFIX}{name} =")]
    paths.WALLUST_CONF.write_text("\n".join(lines) + "\n")


# --- include patching ------------------------------------------------------

def _patch_include(inc: dict, state: dict) -> str | None:
    """Returns a hint string if manual action is needed, else None."""
    file = _expand(inc["file"])
    line = inc["line"]
    file.parent.mkdir(parents=True, exist_ok=True)
    text = file.read_text() if file.exists() else ""
    if line in text:
        return None
    if inc.get("conflict") and re.search(inc["conflict"], text, re.M):
        return f"{file} already contains a matching section — add manually:\n    {line}"
    state["include_file"] = str(file)
    state["include_line"] = line
    if inc.get("replace"):
        pat = re.compile(inc["replace"], re.M)
        m = pat.search(text)
        if m:
            state["replaced_line"] = text[m.start():text.find("\n", m.start()) if "\n" in text[m.start():] else len(text)]
            text = pat.sub(line, text, count=1)
            file.write_text(text)
            return None
    if inc.get("position", "bottom") == "top":
        text = line + "\n" + text
    else:
        text = (text.rstrip("\n") + "\n" if text else "") + line + "\n"
    file.write_text(text)
    return None


def _unpatch_include(state: dict) -> None:
    file = Path(state.get("include_file", ""))
    line = state.get("include_line")
    if not line or not file.exists():
        return
    text = file.read_text()
    if "replaced_line" in state:
        text = text.replace(line, state["replaced_line"], 1)
    else:
        text = "\n".join(l for l in text.splitlines() if l != line) + "\n"
    file.write_text(text)


# --- public ----------------------------------------------------------------

def install(name: str, log=print) -> bool:
    a = available().get(name)
    if not a:
        log(f"unknown addon {name!r} — see `wallflow addons list`")
        return False
    if not shutil.which("wallust"):
        log("wallust is not installed; addons need it (theme.enabled must be true).")
        return False
    missing = [b for b in a.get("requires", []) if not shutil.which(b)]
    if missing:
        log(f"note: {', '.join(missing)} not found in PATH — installing anyway")

    prev = installed().get(name, {})
    state = {"name": name, "reload": a.get("reload", ""), "digest": _digest(a)}
    # re-installing on top: keep what `remove` needs to undo the first install
    for k in ("backup", "include_file", "include_line", "replaced_line"):
        if k in prev:
            state[k] = prev[k]

    # 0. helper script -> ~/.local/bin (optional)
    if a.get("bin"):
        BIN_DIR.mkdir(parents=True, exist_ok=True)
        dst = BIN_DIR / a["bin"]
        shutil.copyfile(a["_dir"] / a["bin"], dst)
        dst.chmod(0o755)
        state["bin"] = str(dst)
        if a.get("system_bin"):
            sdst = SYSTEM_BIN_DIR / a["bin"]
            log(f"installing {sdst} (sudo) so it is found without ~/.local/bin in PATH")
            r = subprocess.run(["sudo", "install", "-Dm755", str(dst), str(sdst)])
            if r.returncode == 0:
                state["system_bin"] = str(sdst)
            else:
                log(f"note: could not install {sdst}; call ~/.local/bin/{a['bin']} by absolute "
                    "path from Hyprland hooks instead")

    hint = None
    if a.get("template"):
        # 1. template -> ~/.config/wallust/templates/wallflow-<name>.<ext>
        src = a["_dir"] / a["template"]
        tname = f"{PREFIX}{name}{src.suffix}"
        paths.WALLUST_TEMPLATES.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, paths.WALLUST_TEMPLATES / tname)

        # 2. back up a pre-existing target we're about to overwrite (only once)
        target = _expand(a["target"])
        state.update(template=tname, target=str(target))
        if target.exists() and name not in installed():
            bak = target.with_name(target.name + ".wallflow-bak")
            shutil.copyfile(target, bak)
            state["backup"] = str(bak)
        target.parent.mkdir(parents=True, exist_ok=True)

        # 3. register + include
        _register(name, tname, a["target"])
        hint = _patch_include(a["include"], state) if "include" in a else None

    paths.ADDON_STATE_DIR.mkdir(parents=True, exist_ok=True)
    (paths.ADDON_STATE_DIR / f"{name}.json").write_text(json.dumps(state, indent=2))
    log(f"installed addon {name}")
    if hint:
        log("  ! " + hint)
    if a.get("note"):
        log("  " + a["note"])
    if a.get("warning"):
        log("  ! " + a["warning"].replace("\n", "\n    "))
    return True


def refresh(log=print) -> list[str]:
    """Re-install every installed addon whose files changed since it was installed
    (run by `wallflow update`). Returns the names that were refreshed."""
    avail = available()
    done = []
    for name, state in installed().items():
        a = avail.get(name)
        if not a:
            log(f"addon {name}: no longer shipped, leaving as is")
            continue
        if state.get("digest") == _digest(a):
            continue
        log(f"addon {name}: updated, re-installing")
        if install(name, log):
            done.append(name)
    return done


def remove(name: str, log=print, purge: bool = False) -> bool:
    """Undo an install. Default restores the app's config from the backup we took;
    purge=True deletes the rendered file and the backup instead (clean slate)."""
    state = installed().get(name)
    if not state:
        log(f"addon {name!r} is not installed")
        return False
    if state.get("bin"):
        Path(state["bin"]).unlink(missing_ok=True)
    if state.get("system_bin") and Path(state["system_bin"]).exists():
        subprocess.run(["sudo", "rm", "-f", state["system_bin"]])
    if state.get("template"):
        _unregister(name)
        (paths.WALLUST_TEMPLATES / state["template"]).unlink(missing_ok=True)
        _unpatch_include(state)
        target = Path(state["target"])
        if purge:
            if state.get("backup"):
                Path(state["backup"]).unlink(missing_ok=True)
            if not state.get("include_file") == str(target):
                target.unlink(missing_ok=True)
        elif state.get("backup") and Path(state["backup"]).exists():
            shutil.move(state["backup"], target)
        elif target.exists() and not state.get("include_file") == str(target):
            target.unlink()
    (paths.ADDON_STATE_DIR / f"{name}.json").unlink(missing_ok=True)
    log(f"{'purged' if purge else 'removed'} addon {name}")
    return True


def remove_all(log=print, purge: bool = False) -> None:
    for name in list(installed()):
        remove(name, log, purge)


def list_text() -> str:
    av, inst = available(), installed()
    if not av:
        return "no addons found in " + str(paths.ADDONS_DIR)
    w = max(len(n) for n in av)
    rows = []
    for n, a in av.items():
        status = "installed" if n in inst else "available"
        missing = [b for b in a.get("requires", []) if not shutil.which(b)]
        if missing and n not in inst:
            status += f" (needs {', '.join(missing)})"
        rows.append(f"  {n.ljust(w)}  {status.ljust(22)}  {a.get('description', '')}")
    return "\n".join(rows)


def info_text(name: str) -> str:
    a = available().get(name)
    if not a:
        return f"unknown addon {name!r}"
    lines = [f"{a['name']} — {a.get('description', '')}",
             f"  requires : {', '.join(a.get('requires', [])) or '-'}",
             f"  renders  : {a.get('target') or '-'}",
             f"  reload   : {a.get('reload') or '-'}"]
    if a.get("bin"):
        lines.append(f"  bin      : ~/.local/bin/{a['bin']}"
                     + (f", /usr/local/bin/{a['bin']} (sudo)" if a.get("system_bin") else ""))
    if "include" in a:
        lines.append(f"  include  : {a['include']['line']}  ->  {a['include']['file']}")
    if a.get("note"):
        lines.append(f"  note     : {a['note']}")
    if a.get("warning"):
        lines.append("  warning  : " + a["warning"].replace("\n", "\n             "))
    return "\n".join(lines)
