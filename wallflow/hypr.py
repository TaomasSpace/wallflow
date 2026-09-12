"""Writes a managed block (autostart + keybind + layer rule) into the Hyprland config.

Supports the Lua config (hyprland.lua) and classic hyprlang (hyprland.conf).
The block is delimited by markers so re-running setup replaces it in place and
uninstall removes it cleanly; nothing else in the file is touched.
"""
import re
import subprocess
from pathlib import Path

from . import detect, paths

BEGIN = ">>> wallflow (managed — edit config.toml + `wallflow setup` instead) >>>"
END = "<<< wallflow <<<"


def _bind_parts(bind: str) -> tuple[str, str]:
    """'SUPER + W' / 'SUPER SHIFT, W' / 'SUPER, W' -> ('SUPER SHIFT', 'W')."""
    s = bind.replace("+", " ").replace(",", " ")
    parts = s.split()
    if len(parts) < 2:
        return "SUPER", parts[0] if parts else "W"
    return " ".join(parts[:-1]).upper(), parts[-1]


def _all_bind_parts(cfg: dict) -> tuple[str, str]:
    """The 'show hidden too' bind — explicit hypr.bind_all, or the main bind + SHIFT."""
    raw = cfg["hypr"].get("bind_all") or ""
    if raw:
        return _bind_parts(raw)
    mods, key = _bind_parts(cfg["hypr"]["bind"])
    mod_list = mods.split()
    if "SHIFT" not in mod_list:
        mod_list.append("SHIFT")
    return " ".join(mod_list), key


def render(flavor: str, cfg: dict) -> str:
    exe = paths.launcher_cmd()
    mods, key = _bind_parts(cfg["hypr"]["bind"])
    mods_all, key_all = _all_bind_parts(cfg)
    pauser = cfg["video"]["pause_on_fullscreen"]
    watch = cfg["rename"]["mode"] != 0
    if flavor == "lua":
        lines = [
            f"-- {BEGIN}",
            'hl.on("hyprland.start", function()',
            f'    hl.exec_cmd("{exe} restore")',
        ]
        if pauser:
            lines.append(f'    hl.exec_cmd("{exe} pauser")')
        if watch:
            lines.append(f'    hl.exec_cmd("{exe} watch")')
        lines += [
            "end)",
            f'hl.bind("{mods.replace(" ", " + ")} + {key}", hl.dsp.exec_cmd("{exe} ui"))',
            f'hl.bind("{mods_all.replace(" ", " + ")} + {key_all}", hl.dsp.exec_cmd("{exe} ui --all"))',
            'hl.layer_rule({ match = { namespace = "mpvpaper" }, name = "wallflow-mpvpaper-noanim", no_anim = true })',
            f"-- {END}",
        ]
    else:
        lines = [
            f"# {BEGIN}",
            f"exec-once = {exe} restore",
        ]
        if pauser:
            lines.append(f"exec-once = {exe} pauser")
        if watch:
            lines.append(f"exec-once = {exe} watch")
        lines += [
            f"bind = {mods}, {key}, exec, {exe} ui",
            f"bind = {mods_all}, {key_all}, exec, {exe} ui --all",
            "layerrule = noanim, mpvpaper",
            f"# {END}",
        ]
    return "\n".join(lines) + "\n"


def _block_re(flavor: str) -> re.Pattern:
    c = "--" if flavor == "lua" else "#"
    return re.compile(
        rf"\n?{re.escape(c)} {re.escape(BEGIN)}.*?{re.escape(c)} {re.escape(END)}\n?",
        re.S,
    )


def install(cfg: dict) -> Path:
    file, flavor = _target(cfg)
    file.parent.mkdir(parents=True, exist_ok=True)
    text = file.read_text() if file.exists() else ""
    text = _block_re(flavor).sub("\n", text).rstrip("\n")
    text = (text + "\n\n" if text else "") + render(flavor, cfg)
    file.write_text(text)
    return file


def remove(cfg: dict) -> bool:
    file, flavor = _target(cfg)
    if not file.exists():
        return False
    text = file.read_text()
    new = _block_re(flavor).sub("\n", text)
    if new != text:
        file.write_text(new.rstrip("\n") + "\n")
        return True
    return False


def _target(cfg: dict) -> tuple[Path, str]:
    override = cfg["hypr"].get("config_file")
    if override:
        p = Path(override).expanduser()
        return p, ("lua" if p.suffix == ".lua" else "conf")
    return detect.hyprland_config()


def bind_conflicts(cfg: dict) -> list[str]:
    """Lines in the config that already use the same key combo (outside our block)."""
    file, flavor = _target(cfg)
    if not file.exists():
        return []
    text = _block_re(flavor).sub("", file.read_text())
    lines = text.splitlines()
    hits = []
    for mods, key in (_bind_parts(cfg["hypr"]["bind"]), _all_bind_parts(cfg)):
        pat = re.compile(rf"{re.escape(mods.replace(' ', r'\s*\+?\s*'))}\s*[+,]\s*{re.escape(key)}\b", re.I)
        hits += [l.strip() for l in lines if pat.search(l)]
    return hits


def reload() -> None:
    if detect.hyprland_running():
        subprocess.run(["hyprctl", "reload"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
