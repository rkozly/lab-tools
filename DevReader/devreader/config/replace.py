"""Replace a whole file on a device with a local file.

Pipeline (per device):

1. read the current file byte-exact (it must exist) and save a local backup;
2. push the local file (staged + ``cat >`` to keep permissions);
3. read the file back and verify it is byte-identical to the local file;
4. summarize the key options of the new file for the comparison table.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from devreader.config.uci import UciDocument
from devreader.core.adb_service import AdbService
from devreader.core.errors import (
    AdbError,
    ConfigPushError,
    ConfigReadError,
    ConfigValidationError,
    ConfigVerificationError,
)
from devreader.core.logging_service import LogService
from devreader.core.preparation import check_ready

# Remote files offered in the path picker; the user may also type any path.
REPLACEABLE_PATHS = ("/etc/config/wireless",)

# Options shown in the comparison table, in column order.
SUMMARY_KEYS = ("rf_freq1_KHz", "rf_freq2_KHz", "mld_ssid", "macaddr")


def summarize(data: bytes, keys: tuple[str, ...] = SUMMARY_KEYS) -> dict[str, list[str]]:
    """Return ``{key: [values]}`` for ``option key value`` lines in any section.

    Options such as ``macaddr`` appear once per radio/interface, so every
    distinct value is kept, in file order. A key that is absent maps to ``[]``.
    """
    doc = UciDocument.from_bytes(data)
    out: dict[str, list[str]] = {k: [] for k in keys}
    for section in doc.sections:
        for key in keys:
            opt = section.options.get(key)
            if opt is not None and opt.value not in out[key]:
                out[key].append(opt.value)
    return out


def validate_remote_path(path: str) -> str:
    path = (path or "").strip()
    if not path.startswith("/") or path.endswith("/"):
        raise ConfigValidationError(f"'{path}' is not an absolute file path on the device")
    if any(c in path for c in "'\"\n\r;&|`$"):
        raise ConfigValidationError(f"'{path}': quotes and shell characters are not allowed")
    return path


@dataclass
class FileSummary:
    serial: str
    remote_path: str
    values: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class ReplaceResult(FileSummary):
    backup: Path | None = None
    no_change: bool = False


class FileReplaceService:
    def __init__(self, adb: AdbService, log: LogService, backup_dir: Path | None = None) -> None:
        self._adb = adb
        self._log = log
        self._backup_dir = backup_dir

    def read_summary(self, serial: str, remote_path: str, cancel: threading.Event | None = None) -> FileSummary:
        raw = self._read(serial, remote_path, cancel)
        values = summarize(raw)
        for key, vals in values.items():
            self._log.info(f"{key} = {', '.join(vals) if vals else '(not present)'}", device=serial)
        return FileSummary(serial, remote_path, values)

    def replace(self, serial: str, remote_path: str, data: bytes,
                cancel: threading.Event | None = None) -> ReplaceResult:
        remote_path = validate_remote_path(remote_path)
        if not data.strip():
            raise ConfigValidationError("The local file is empty - refusing to replace the device file")
        self._log.info(f"Replacing {remote_path} ({len(data)} bytes)", device=serial)

        original = self._read(serial, remote_path, cancel)
        if original == data:
            self._log.info("Device file is already identical - nothing to write", device=serial)
            return ReplaceResult(serial, remote_path, summarize(data), no_change=True)
        backup = self._backup(serial, remote_path, original)

        try:
            self._adb.write_file(serial, remote_path, data, cancel=cancel)
        except AdbError as exc:
            raise ConfigPushError(
                f"Failed to push {remote_path}: {exc}",
                command=exc.command, output=exc.output, serial=serial,
            ) from exc
        self._log.info("File pushed - verifying", device=serial)

        try:
            readback = self._adb.read_file(serial, remote_path, cancel=cancel)
        except AdbError as exc:
            raise ConfigVerificationError(f"Verification read failed: {exc}", serial=serial) from exc
        if readback != data:
            raise ConfigVerificationError(
                f"Verification failed: {remote_path} on the device differs from the local file. Backup: {backup}",
                serial=serial,
            )
        self._log.success(f"{remote_path} replaced and verified", device=serial)
        return ReplaceResult(serial, remote_path, summarize(readback), backup=backup)

    # ------------------------------------------------------------------
    def _read(self, serial: str, remote_path: str, cancel) -> bytes:
        remote_path = validate_remote_path(remote_path)
        check_ready(self._adb, serial)
        self._log.info(f"Reading {remote_path}", device=serial)
        try:
            return self._adb.read_file(serial, remote_path, cancel=cancel)
        except AdbError as exc:
            raise ConfigReadError(
                f"Failed to read {remote_path}: {exc}",
                command=exc.command, output=exc.output, serial=serial,
            ) from exc

    def _backup(self, serial: str, remote_path: str, data: bytes) -> Path | None:
        if self._backup_dir is None:
            return None
        try:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_serial = "".join(c if c.isalnum() or c in "-_." else "_" for c in serial)
            name = remote_path.rstrip("/").rsplit("/", 1)[-1] or "file"
            path = self._backup_dir / safe_serial / f"replace_{name}_{stamp}.bak"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            self._log.info(f"Backup saved to {path}", device=serial)
            return path
        except OSError as exc:
            self._log.warning(f"Could not save local backup: {exc}", device=serial)
            return None
