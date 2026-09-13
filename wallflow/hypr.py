"""Writes a managed block (autostart + keybind + layer rule) into the Hyprland config.

Supports the Lua config (hyprland.lua) and classic hyprlang (hyprland.conf).
The block is delimited by markers so re-running setup replaces it in place and
uninstall removes it cleanly; nothing else in the file is touched.
"""
import json
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
    watch = cfg["rename"]["mode"] != 0 or cfg["general"]["auto_prewarm"] or cfg["depth"]["auto"]
    overlay = cfg["overlay"]["enabled"]
    edit = _bind_parts(cfg["hypr"]["bind_edit"]) if cfg["hypr"].get("bind_edit") else None
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
        if overlay:
            lines.append(f'    hl.exec_cmd("{exe} overlay start")')
        lines += [
            "end)",
            f'hl.bind("{mods.replace(" ", " + ")} + {key}", hl.dsp.exec_cmd("{exe} ui"))',
            f'hl.bind("{mods_all.replace(" ", " + ")} + {key_all}", hl.dsp.exec_cmd("{exe} ui --all"))',
        ]
        if edit:
            lines.append(f'hl.bind("{edit[0].replace(" ", " + ")} + {edit[1]}", hl.dsp.exec_cmd("{exe} overlay edit"))')
        lines += [
            'hl.layer_rule({ match = { namespace = "mpvpaper" }, name = "wallflow-mpvpaper-noanim", no_anim = true })',
            'hl.layer_rule({ match = { namespace = "wallflow-overlay" }, name = "wallflow-overlay-noanim", no_anim = true })',
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
        if overlay:
            lines.append(f"exec-once = {exe} overlay start")
        lines += [
            f"bind = {mods}, {key}, exec, {exe} ui",
            f"bind = {mods_all}, {key_all}, exec, {exe} ui --all",
        ]
        if edit:
            lines.append(f"bind = {edit[0]}, {edit[1]}, exec, {exe} overlay edit")
        lines += [
            "layerrule = noanim, mpvpaper",
            "layerrule = noanim, wallflow-overlay",
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


def close_special_workspaces(cfg: dict | None = None) -> None:
    """Close only the special workspace shown on the *currently focused* monitor.
    One shown on a different monitor is left alone — it won't overlap the picker
    (which opens on the focused monitor), and toggling it from here would just pull
    it onto this monitor instead of hiding it. Works for any special workspace name
    (communication, music, …) — read fresh from hyprctl each time, nothing hardcoded.

    On a Hyprland build with Lua-config support, `hyprctl dispatch` expects a Lua
    expression (hl.dsp.*) instead of classic `<dispatcher> <args>` syntax — detected
    the same way as the config-file flavor (hyprland.lua vs hyprland.conf).
    """
    if not detect.hyprland_running():
        return
    try:
        r = subprocess.run(["hyprctl", "monitors", "-j"], capture_output=True, text=True, timeout=2)
        mons = json.loads(r.stdout)
    except Exception:
        return
    flavor = _target(cfg)[1] if cfg is not None else detect.hyprland_config()[1]
    lua = flavor == "lua"
    for m in mons:
        if not m.get("focused"):
            continue
        name = (m.get("specialWorkspace") or {}).get("name") or ""
        name = name.removeprefix("special:")
        if not name:
            continue
        cmd = (["hyprctl", "dispatch", f'hl.dsp.workspace.toggle_special("{name}")'] if lua
               else ["hyprctl", "dispatch", "togglespecialworkspace", name])
        subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _clients() -> list:
    try:
        r = subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True, timeout=2)
        return json.loads(r.stdout)
    except Exception:
        return []


def unpin_pinned_windows() -> list[str]:
    """A pinned window (Hyprland's `pin` dispatcher — how a lot of Discord/Spotify
    quick-toggle setups show it) stays on top of every workspace regardless of focus,
    so the picker opens underneath it. Unpin them and return their addresses so they
    can be re-pinned with restore_pinned_windows() once the picker closes."""
    if not detect.hyprland_running():
        return []
    addrs = [c["address"] for c in _clients() if c.get("pinned") and c.get("address")]
    for addr in addrs:
        subprocess.run(["hyprctl", "dispatch", "pin", f"address:{addr}"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return addrs


def restore_pinned_windows(addrs: list[str]) -> None:
    if not addrs or not detect.hyprland_running():
        return
    still = {c["address"] for c in _clients()}
    for addr in addrs:
        if addr in still:
            subprocess.run(["hyprctl", "dispatch", "pin", f"address:{addr}"], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
