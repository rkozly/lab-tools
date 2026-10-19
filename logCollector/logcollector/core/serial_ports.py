"""
Serial console ports: which COM port belongs to which device role.

The ports come from the MobaXterm sessions on this PC. A session named
"<ROLE> - COM<N>" (e.g. "CPE2 - COM12") maps ROLE to COM<N>. An environment
variable serial_port_<role>=COM<N> overrides a session.
"""

import os
import re
import shutil

from ..config import MOBAXTERM_INI_ENV, SERIAL_ENV_PREFIX
from .adb import load_env

_SESSION_RE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*-")
_COM_RE = re.compile(r"\bCOM(\d+)\b", re.IGNORECASE)
_ENV_RE = re.compile(rf"^{re.escape(SERIAL_ENV_PREFIX)}(\w+)$", re.IGNORECASE)


def parse_mobaxterm_sessions(text: str) -> dict[str, str]:
    """Extract {ROLE: "COM<N>"} from the [Bookmarks*] sections of MobaXterm.ini.

    Example lines:
        [Bookmarks_1]
        SubRep=Serial
        CPE2 - COM12=#131#8%-2%115200%3%0%0%1%2%COM12  (USB Serial Port (COM12))%...
    The port is taken from the session name, or from its settings if the
    name does not contain one. The first session for a role wins.
    """
    result = {}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].lower()
            continue
        if not section.startswith("bookmarks") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        role = _SESSION_RE.match(name)
        com = _COM_RE.search(name) or _COM_RE.search(value)
        if role and com:
            result.setdefault(role.group(1).upper(), f"COM{com.group(1)}")
    return result


def ports_from_env(env: dict[str, str]) -> dict[str, str]:
    """Extract {ROLE: "COM<N>"} from serial_port_<role> variables."""
    result = {}
    for name, value in env.items():
        m = _ENV_RE.match(name)
        if m and value.strip():
            result[m.group(1).upper()] = value.strip().upper()
    return result


def find_mobaxterm_ini(env: dict[str, str]) -> str | None:
    """Return the path of MobaXterm.ini, or None if it cannot be found."""
    candidates = []
    for name, value in env.items():
        if name.lower() == MOBAXTERM_INI_ENV and value.strip():
            candidates.append(value.strip())
    for var, sub in (("APPDATA", r"MobaXterm"),
                     ("USERPROFILE", r"Documents\MobaXterm"),
                     ("ProgramFiles(x86)", r"Mobatek\MobaXterm"),
                     ("ProgramFiles", r"Mobatek\MobaXterm")):
        base = env.get(var) or os.environ.get(var)
        if base:
            candidates.append(os.path.join(base, sub, "MobaXterm.ini"))
    exe = shutil.which("MobaXterm")  # portable version on PATH
    if exe:
        candidates.append(os.path.join(os.path.dirname(exe), "MobaXterm.ini"))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def load_port_map() -> tuple[dict[str, str], str]:
    """Return ({ROLE: "COM<N>"}, description of where the ports came from)."""
    env = load_env()
    ports = {}
    source = "MobaXterm.ini not found"
    ini = find_mobaxterm_ini(env)
    if ini:
        try:
            with open(ini, "rb") as f:
                ports = parse_mobaxterm_sessions(f.read().decode("utf-8", errors="replace"))
            source = f"MobaXterm sessions ({ini})"
        except OSError as e:
            source = f"cannot read {ini}: {e.strerror or e}"
    ports.update(ports_from_env(env))
    return ports, source


def available_ports() -> list[str]:
    """COM ports present on this PC (empty if pyserial is not installed)."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    names = [p.device for p in list_ports.comports()]
    return sorted(names, key=lambda n: (len(n), n))
