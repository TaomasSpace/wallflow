"""Addons = wallust templates that recolour terminal apps from the wallpaper palette.

Each addon is a folder in addons/ with an addon.toml:

    name        = "kitty"
    description = "…"
    requires    = ["kitty"]                   # binaries that should exist (warn only)
    template    = "wallflow-colors.conf"      # file in the addon folder
    target      = "~/.config/kitty/wallflow-colors.conf"   # where wallust renders it
    reload      = "pkill -USR1 kitty"         # run after every wallpaper change (optional)
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
import json
import os
import re
import shutil
import tomllib
from pathlib import Path

from . import paths

PREFIX = "wallflow-"


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

    # 1. template -> ~/.config/wallust/templates/wallflow-<name>.<ext>
    src = a["_dir"] / a["template"]
    tname = f"{PREFIX}{name}{src.suffix}"
    paths.WALLUST_TEMPLATES.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, paths.WALLUST_TEMPLATES / tname)

    # 2. back up a pre-existing target we're about to overwrite (only once)
    target = _expand(a["target"])
    state = {"name": name, "template": tname, "target": str(target), "reload": a.get("reload", "")}
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
    return True


def remove(name: str, log=print) -> bool:
    state = installed().get(name)
    if not state:
        log(f"addon {name!r} is not installed")
        return False
    _unregister(name)
    (paths.WALLUST_TEMPLATES / state["template"]).unlink(missing_ok=True)
    _unpatch_include(state)
    target = Path(state["target"])
    if state.get("backup") and Path(state["backup"]).exists():
        shutil.move(state["backup"], target)
    elif target.exists() and not state.get("include_file") == str(target):
        target.unlink()
    (paths.ADDON_STATE_DIR / f"{name}.json").unlink(missing_ok=True)
    log(f"removed addon {name}")
    return True


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
             f"  renders  : {a['target']}",
             f"  reload   : {a.get('reload') or '-'}"]
    if "include" in a:
        lines.append(f"  include  : {a['include']['line']}  ->  {a['include']['file']}")
    if a.get("note"):
        lines.append(f"  note     : {a['note']}")
    return "\n".join(lines)
