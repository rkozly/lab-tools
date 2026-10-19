"""Flash WLAN (wlan_wkk) firmware from a build folder onto a device.

Equivalent of, run from the build folder (``pushd \\\\pint\\QI_LBR_Builds\\WLAN...``)::

    adb -s <serial> root
    adb -s <serial> shell setenforce 0
    adb -s <serial> shell mount -o remount,rw /firmware
    adb -s <serial> push <file> /firmware/image/qcn9224     (for each file)
    (ver_info.txt: Image_Build_IDs.wlan_wkk = <build folder name>)
    adb -s <serial> shell mount -o remount,ro /firmware
    adb -s <serial> reboot

The AP and the CPEs use different images (:data:`WLAN_FILES`). Every file
is checked before anything is touched on the device.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from devreader.config.jsoncfg import set_string_value
from devreader.core.adb_service import WAIT_FOR_DEVICE_TIMEOUT, AdbService
from devreader.core.errors import AdbError, DevReaderError
from devreader.core.logging_service import LogService
from devreader.core.preparation import check_ready

WLAN_TARGET_DIR = "/firmware/image/qcn9224"
VER_INFO_PATH = "/firmware/verinfo/ver_info.txt"

# Files pushed per device role, in this order.
WLAN_FILES: dict[str, tuple[str, ...]] = {
    "ap": ("Data_dualmac.msc", "amss_dualmac.bin", "m3.bin", "bdwlan.b101c"),
    "cpe": ("Data.msc", "amss.bin", "m3.bin", "bdwlan.b0004"),
}

PUSH_TIMEOUT = 300.0


class FirmwareError(DevReaderError):
    """Flashing firmware failed."""


def role_of(is_ap: bool) -> str:
    return "ap" if is_ap else "cpe"


def missing_files(folder: Path, role: str) -> list[str]:
    return [f for f in WLAN_FILES[role] if not (folder / f).is_file()]


@dataclass
class FlashResult:
    serial: str
    folder: Path
    files: tuple[str, ...]


# (serial, step_index, total_steps, step_title) -> None
ProgressFn = Callable[[str, int, int, str], None]


class FirmwareService:
    def __init__(self, adb: AdbService, log: LogService, backup_dir: Path | None = None) -> None:
        self._adb = adb
        self._log = log
        self._backup_dir = backup_dir

    def flash_wlan(self, serial: str, folder: Path, *, is_ap: bool,
                   update_ver_info: bool = True,
                   cancel: threading.Event | None = None,
                   on_progress: ProgressFn | None = None) -> FlashResult:
        role = role_of(is_ap)
        files = WLAN_FILES[role]
        missing = missing_files(folder, role)
        if missing:
            raise FirmwareError(f"Missing in {folder}: {', '.join(missing)}", serial=serial)
        check_ready(self._adb, serial)

        steps: list[tuple[str, Callable[[], None]]] = [
            ("Root", lambda: self._run(serial, ["wait-for-device", "root"], cancel, WAIT_FOR_DEVICE_TIMEOUT)),
            ("setenforce 0", lambda: self._shell(serial, "setenforce 0", cancel, check=False)),
            ("Remount /firmware rw", lambda: self._shell(serial, "mount -o remount,rw /firmware", cancel)),
        ]
        for name in files:
            steps.append((f"Push {name}", lambda n=name: self._adb.push(
                serial, folder / n, WLAN_TARGET_DIR, cancel=cancel, timeout=PUSH_TIMEOUT)))
        if update_ver_info:
            steps.append(("Update ver_info.txt", lambda: self._update_ver_info(serial, folder.name, cancel)))
        steps.append(("Remount /firmware ro", lambda: self._shell(serial, "mount -o remount,ro /firmware",
                                                                    cancel, check=False)))
        steps.append(("Reboot", lambda: self._run(serial, ["reboot"], cancel, 60)))

        self._log.info(f"Flashing wlan_wkk from {folder} ({role.upper()} images)", device=serial)
        for i, (title, action) in enumerate(steps):
            if on_progress:
                on_progress(serial, i, len(steps), title)
            self._log.info(f"Step {i + 1}/{len(steps)}: {title}", device=serial)
            try:
                action()
            except AdbError as exc:
                raise FirmwareError(f"{title} failed: {exc}", command=exc.command,
                                    output=exc.output, serial=serial) from exc
        if on_progress:
            on_progress(serial, len(steps), len(steps), "Rebooting")
        self._log.success(f"wlan_wkk flashed from {folder.name} - device is rebooting", device=serial)
        return FlashResult(serial, folder, files)

    def _update_ver_info(self, serial: str, build_id: str, cancel) -> None:
        """Set Image_Build_IDs.wlan_wkk in ver_info.txt to the flashed build (the "vi" step)."""
        original = self._adb.read_file(serial, VER_INFO_PATH, cancel=cancel)
        try:
            new = set_string_value(original, "Image_Build_IDs", "wlan_wkk", build_id)
        except (KeyError, ValueError) as exc:
            raise AdbError(f"Cannot edit {VER_INFO_PATH}: {exc}", serial=serial) from exc
        if new == original:
            self._log.info(f"wlan_wkk in {VER_INFO_PATH} is already {build_id}", device=serial)
            return
        self._backup(serial, original)
        self._adb.write_file(serial, VER_INFO_PATH, new, cancel=cancel)
        if self._adb.read_file(serial, VER_INFO_PATH, cancel=cancel) != new:
            raise AdbError(f"Verification of {VER_INFO_PATH} failed", serial=serial)
        self._log.success(f"wlan_wkk in {VER_INFO_PATH} set to {build_id}", device=serial)

    def _backup(self, serial: str, data: bytes) -> None:
        if self._backup_dir is None:
            return
        try:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in serial)
            path = self._backup_dir / safe / f"ver_info_{stamp}.bak"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            self._log.info(f"Backup saved to {path}", device=serial)
        except OSError as exc:
            self._log.warning(f"Could not save local backup: {exc}", device=serial)

    def _run(self, serial: str, args: list[str], cancel, timeout: float) -> None:
        self._adb.run(args, serial=serial, timeout=timeout, cancel=cancel, check=True)

    def _shell(self, serial: str, command: str, cancel, check: bool = True) -> None:
        result = self._adb.shell(serial, command, wait=True, timeout=60, cancel=cancel, check=check)
        if check and any(m in result.output.lower() for m in ("permission denied", "not permitted", "invalid")):
            raise AdbError(result.output.strip(), command=result.command_line, output=result.output, serial=serial)
