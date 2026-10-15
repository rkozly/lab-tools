"""Device discovery from ``adb_device_id_*`` environment variables.

On Windows, variables set via *System Properties* are only visible to processes
started after the change. To pick up changes without restarting DevReader we
also read the persisted user and machine environment from the registry.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum

from devreader.core.adb_service import AdbService, DeviceState
from devreader.core.logging_service import LogService

ENV_PREFIX = "adb_device_id_"
_ENV_RE = re.compile(rf"^{ENV_PREFIX}(.+)$", re.IGNORECASE)


class ConnectionStatus(str, Enum):
    CONNECTED = "Connected"
    DISCONNECTED = "Disconnected"
    UNAUTHORIZED = "Unauthorized"
    OFFLINE = "Offline"
    OTHER = "Other"

    @classmethod
    def from_state(cls, state: DeviceState) -> "ConnectionStatus":
        return {
            DeviceState.CONNECTED: cls.CONNECTED,
            DeviceState.DISCONNECTED: cls.DISCONNECTED,
            DeviceState.UNAUTHORIZED: cls.UNAUTHORIZED,
            DeviceState.AUTHORIZING: cls.UNAUTHORIZED,
            DeviceState.NO_PERMISSIONS: cls.UNAUTHORIZED,
            DeviceState.OFFLINE: cls.OFFLINE,
        }.get(state, cls.OTHER)


@dataclass
class Device:
    name: str
    serial: str
    env_var: str = ""
    state: DeviceState = DeviceState.DISCONNECTED
    last_action: str = ""
    last_action_ok: bool | None = None
    configured: bool = True  # False => connected but not mapped by any env var

    @property
    def status(self) -> ConnectionStatus:
        return ConnectionStatus.from_state(self.state)

    @property
    def is_ready(self) -> bool:
        return self.state == DeviceState.CONNECTED


@dataclass
class DiscoveryResult:
    devices: list[Device] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    adb_available: bool = True


def friendly_name(suffix: str) -> str:
    """``ap`` -> ``AP``, ``cpe1`` -> ``CPE1``."""
    return suffix.strip().replace("_", " ").upper()


def read_environment() -> dict[str, str]:
    """Merge machine, user and process environment (process wins)."""
    merged: dict[str, str] = {}
    if os.name == "nt":
        merged.update(_read_registry_env())
    merged.update(os.environ)
    return merged


def _read_registry_env() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        import winreg
    except ImportError:
        return values
    keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, r"Environment"),  # user overrides machine
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
                    i += 1
                    if _ENV_RE.match(name) and isinstance(value, str):
                        values[name] = value
        except OSError:
            continue
    return values


def find_env_devices(env: dict[str, str] | None = None) -> tuple[list[Device], list[str]]:
    """Parse ``adb_device_id_*`` variables into :class:`Device` objects."""
    env = read_environment() if env is None else env
    devices: dict[str, Device] = {}
    warnings: list[str] = []
    for var, value in env.items():
        match = _ENV_RE.match(var)
        if not match:
            continue
        serial = (value or "").strip().strip('"').strip("'")
        name = friendly_name(match.group(1))
        if not serial:
            warnings.append(f"Environment variable {var} is empty - skipped")
            continue
        if name in devices:  # case-variant duplicates of the same variable
            continue
        devices[name] = Device(name=name, serial=serial, env_var=var)
    ordered = sorted(devices.values(), key=lambda d: _natural_key(d.name))
    return ordered, warnings


def _natural_key(text: str):
    # AP first (by convention), then CPE1, CPE2, ..., CPE10 in numeric order.
    parts = re.split(r"(\d+)", text)
    return [0 if text == "AP" else 1] + [int(p) if p.isdigit() else p for p in parts]


class DiscoveryService:
    def __init__(self, adb: AdbService, log: LogService, include_unmapped: bool = True) -> None:
        self._adb = adb
        self._log = log
        self.include_unmapped = include_unmapped

    def discover(self) -> DiscoveryResult:
        result = DiscoveryResult()
        devices, warnings = find_env_devices()
        result.warnings.extend(warnings)
        if not devices:
            result.warnings.append(
                f"No {ENV_PREFIX}* environment variables found. "
                f"Define e.g. {ENV_PREFIX}ap=<serial> and click Refresh."
            )
        for dev in devices:
            self._log.set_alias(dev.serial, dev.name)

        try:
            connected = self._adb.devices()
        except Exception as exc:  # AdbNotFoundError, timeouts, ...
            result.adb_available = False
            for w in result.warnings:
                self._log.warning(w)
            result.warnings.append(str(exc))
            self._log.error(f"ADB unavailable: {exc}")
            result.devices = devices
            return result

        known = {d.serial for d in devices}
        for dev in devices:
            dev.state = connected.get(dev.serial, DeviceState.DISCONNECTED)

        if self.include_unmapped:
            for serial, state in connected.items():
                if serial not in known:
                    devices.append(Device(name="(unmapped)", serial=serial, state=state, configured=False))

        result.devices = devices
        for w in result.warnings:
            self._log.warning(w)
        summary = ", ".join(f"{d.name}={d.status.value}" for d in devices if d.configured) or "none"
        self._log.info(f"Discovery complete: {summary}")
        return result
