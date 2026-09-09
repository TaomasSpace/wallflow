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


def render(flavor: str, cfg: dict) -> str:
    exe = paths.launcher_cmd()
    mods, key = _bind_parts(cfg["hypr"]["bind"])
    pauser = cfg["video"]["pause_on_fullscreen"]
    if flavor == "lua":
        lines = [
            f"-- {BEGIN}",
            'hl.on("hyprland.start", function()',
            f'    hl.exec_cmd("{exe} restore")',
        ]
        if pauser:
            lines.append(f'    hl.exec_cmd("{exe} pauser")')
        lines += [
            "end)",
            f'hl.bind("{mods.replace(" ", " + ")} + {key}", hl.dsp.exec_cmd("{exe}"))',
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
        lines += [
            f"bind = {mods}, {key}, exec, {exe}",
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
    mods, key = _bind_parts(cfg["hypr"]["bind"])
    text = _block_re(flavor).sub("", file.read_text())
    pat = re.compile(rf"{re.escape(mods.replace(' ', r'\s*\+?\s*'))}\s*[+,]\s*{re.escape(key)}\b", re.I)
    return [l.strip() for l in text.splitlines() if pat.search(l)]


def reload() -> None:
    if detect.hyprland_running():
        subprocess.run(["hyprctl", "reload"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
