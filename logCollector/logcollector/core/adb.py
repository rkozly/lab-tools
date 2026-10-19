"""
ADB helpers: locating adb, parsing `adb devices`, and mapping device
roles (AP, CPE1, ...) to serial numbers from environment variables.
"""

import os
import re
import shutil
import sys

from ..config import AP_ROLE, ENV_PREFIX
from .process import start_detached


class AdbNotFoundError(Exception):
    pass


def find_adb() -> str | None:
    """Return the full path of adb, or None if it is not installed."""
    return shutil.which("adb")


_server_started = False


def ensure_server(adb: str) -> None:
    """Start the adb server once, detached from our output pipes.

    If an adb command that we capture output from has to start the server
    itself, the server inherits that command's pipes and keeps them open
    forever, so the command never "finishes" (stuck in Stopping).
    """
    global _server_started
    if not _server_started:
        _server_started = start_detached([adb, "start-server"], timeout=30)


def parse_devices(text: str) -> dict[str, str]:
    """Parse `adb devices` output into {serial: state}.

    Example input:
        List of devices attached
        1a2b3c4d	device
        5e6f7a8b	unauthorized
    """
    devices = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("List of devices", "*")):
            continue  # header or daemon start messages
        parts = line.split()
        if len(parts) >= 2:
            devices[parts[0]] = parts[1]
    return devices


_ENV_RE = re.compile(rf"^{re.escape(ENV_PREFIX)}(\w+)$", re.IGNORECASE)


def roles_from_env(env: dict[str, str]) -> dict[str, str]:
    """Extract {ROLE: serial} from a dict of environment variables."""
    result = {}
    for name, value in env.items():
        m = _ENV_RE.match(name)
        if m and value.strip():
            result[m.group(1).upper()] = value.strip()
    return result


def _registry_env() -> dict[str, str]:
    """Read user + system environment variables from the registry.

    os.environ only holds what existed when the app started; reading the
    registry lets Refresh pick up variables the user set afterwards.
    """
    if sys.platform != "win32":
        return {}
    import winreg

    env = {}
    keys = [
        (winreg.HKEY_LOCAL_MACHINE,
         r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, r"Environment"),  # user overrides system
    ]
    for hive, path in keys:
        try:
            with winreg.OpenKey(hive, path) as key:
                i = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(key, i)
                    except OSError:
                        break
                    if isinstance(value, str):
                        env[name] = value
                    i += 1
        except OSError:
            continue
    return env


def load_env() -> dict[str, str]:
    """Process environment plus the current registry values (see above)."""
    env = dict(os.environ)
    env.update(_registry_env())
    return env


def load_role_map() -> dict[str, str]:
    """Return {ROLE: serial} from the process environment and the registry."""
    return roles_from_env(load_env())


_CPE_RE = re.compile(r"^CPE(\d+)$")


def is_cpe(role: str) -> bool:
    return bool(_CPE_RE.match(role))


def order_roles(roles) -> list[str]:
    """AP first, then CPEs by number (CPE2 before CPE10), then the rest."""
    def key(role):
        if role == AP_ROLE:
            return (0, 0, role)
        m = _CPE_RE.match(role)
        if m:
            return (1, int(m.group(1)), role)
        return (2, 0, role)
    return sorted(roles, key=key)


def build_device_rows(role_map: dict[str, str],
                      connected: dict[str, str]) -> list[tuple[str, str, str]]:
    """Combine role mapping and `adb devices` result into table rows.

    Returns a list of (role, serial, state) where state is the adb state
    ("device", "offline", "unauthorized"...) or "missing".
    Only configured roles appear (a setup with 2 CPEs shows CPE1 and CPE2);
    connected devices without a role are appended as "(unassigned)".
    """
    rows = []
    for role in order_roles(role_map):
        serial = role_map[role]
        rows.append((role, serial, connected.get(serial, "missing")))
    mapped = set(role_map.values())
    for serial, state in connected.items():
        if serial not in mapped:
            rows.append(("(unassigned)", serial, state))
    return rows
