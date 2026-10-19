"""Read / write managed parameters on a device.

Write pipeline (per device):

1. read the current file byte-exact from the device;
2. patch *only* the requested values with :class:`UciDocument`;
3. save a local backup of the original file;
4. push the patched file (staged + ``cat >`` to keep permissions);
5. read the file back and verify it is byte-identical to what was pushed;
6. re-parse and confirm each value.
"""

from __future__ import annotations

import difflib
import hashlib
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Mapping

from devreader.config.parameters import ConfigFileDef, ParameterDef
from devreader.config.jsoncfg import JsonDocument
from devreader.config.uci import UciDocument
from devreader.config.wpa import WpaNetworkDocument

# Parser per ConfigFileDef.format. Writable formats must also provide set()/to_bytes().
DOCUMENT_TYPES = {"uci": UciDocument, "wpa_networks": WpaNetworkDocument, "json": JsonDocument}
from devreader.core.adb_service import AdbService
from devreader.core.errors import (
    AdbError,
    ConfigParseError,
    ConfigPushError,
    ConfigReadError,
    ConfigVerificationError,
    ConfigWriteError,
)
from devreader.core.logging_service import LogService
from devreader.core.preparation import check_ready


@dataclass
class ReadResult:
    serial: str
    config: ConfigFileDef
    values: dict[str, str | None]  # param id -> value (None = missing)
    missing: list[str] = field(default_factory=list)
    raw: bytes = b""

    @property
    def ok(self) -> bool:
        return not self.missing


@dataclass
class WriteResult:
    serial: str
    config: ConfigFileDef
    requested: dict[str, str]
    changed: list[str]
    verified: dict[str, str | None]
    backup: Path | None = None
    diff: str = ""

    @property
    def no_change(self) -> bool:
        return not self.changed


class ConfigService:
    def __init__(self, adb: AdbService, log: LogService, backup_dir: Path | None = None) -> None:
        self._adb = adb
        self._log = log
        self._backup_dir = backup_dir

    # ------------------------------------------------------------------
    def read_document(self, serial: str, config: ConfigFileDef, cancel=None):
        check_ready(self._adb, serial)
        try:
            raw = self._adb.read_file(serial, config.remote_path, cancel=cancel)
        except AdbError as exc:
            raise ConfigReadError(
                f"Failed to read {config.remote_path}: {exc}",
                command=exc.command, output=exc.output, serial=serial,
            ) from exc
        if not raw.strip():
            raise ConfigReadError(f"{config.remote_path} is empty or unreadable", serial=serial)
        try:
            doc = DOCUMENT_TYPES[config.format].from_bytes(raw)
        except Exception as exc:
            raise ConfigParseError(f"Failed to parse {config.remote_path}: {exc}", serial=serial) from exc
        if not doc.sections and getattr(doc, "require_sections", True):
            raise ConfigParseError(f"No 'config' sections found in {config.remote_path}", serial=serial)
        return doc, raw

    def read(self, serial: str, config: ConfigFileDef, cancel=None) -> ReadResult:
        self._log.info(f"Reading {config.remote_path}", device=serial)
        doc, raw = self.read_document(serial, config, cancel)
        values: dict[str, str | None] = {}
        missing: list[str] = []
        for p in config.parameters:
            section = doc.find_section(p.section, p.section_type)
            if section is None:
                values[p.id] = None
                if p.required:
                    missing.append(f"section '{p.section}' not found (for {p.key})")
                continue
            opt = section.options.get(p.key)
            values[p.id] = opt.value if opt else None
            if opt is None and p.required:
                missing.append(f"option {p.key} not found in {section.label}")

        required = [p for p in config.parameters if p.required]
        if missing and required and all(values[p.id] is None for p in required):
            raise ConfigParseError(
                f"None of the managed parameters were found in {config.remote_path}: " + "; ".join(missing),
                serial=serial,
            )
        for p in config.parameters:
            self._log.info(f"{p.key} = {values[p.id]!r}", device=serial)
        for m in missing:
            self._log.warning(m, device=serial)
        self._log.success(f"{config.title} retrieved", device=serial)
        return ReadResult(serial, config, values, missing, raw)

    # ------------------------------------------------------------------
    def write(
        self,
        serial: str,
        config: ConfigFileDef,
        values: Mapping[str, str],
        *,
        cancel: threading.Event | None = None,
        create_missing: bool = False,
    ) -> WriteResult:
        """Write ``values`` (param id -> validated string) to the device."""
        if config.read_only:
            raise ConfigWriteError(f"{config.remote_path} is read-only in DevReader", serial=serial)
        params: list[tuple[ParameterDef, str]] = [(config.parameter(pid), v) for pid, v in values.items()]
        self._log.info(f"Writing {len(params)} parameter(s) to {config.remote_path}", device=serial)

        doc, original = self.read_document(serial, config, cancel)
        changed: list[str] = []
        for p, value in params:
            section = doc.find_section(p.section, p.section_type)
            if section is None:
                raise ConfigWriteError(f"Section '{p.section}' not found in {config.remote_path}", serial=serial)
            if p.key not in section.options and not create_missing:
                raise ConfigWriteError(
                    f"Option {p.key} not found in {section.label}; refusing to add it", serial=serial
                )
            old = section.options[p.key].value if p.key in section.options else None
            if doc.set(p.section, p.key, value, section_type=p.section_type, create=create_missing):
                changed.append(p.id)
                self._log.info(f"{p.key}: {old!r} -> {value!r}", device=serial)

        if not changed:
            self._log.info("Values already match the device - nothing to write", device=serial)
            verified = {p.id: doc.get(p.section, p.key, p.section_type) for p, _ in params}
            return WriteResult(serial, config, dict(values), [], verified)

        new_bytes = doc.to_bytes()
        diff = unified_diff(original, new_bytes, config.remote_path)
        _assert_only_expected_lines_changed(original, new_bytes, doc.changed_lines, create_missing)
        backup = self._backup(serial, config, original)

        try:
            self._adb.write_file(serial, config.remote_path, new_bytes, cancel=cancel)
        except AdbError as exc:
            raise ConfigPushError(
                f"Failed to push {config.remote_path}: {exc}",
                command=exc.command, output=exc.output, serial=serial,
            ) from exc
        self._log.info("File pushed - verifying", device=serial)

        # Verification: byte compare + value check.
        try:
            readback = self._adb.read_file(serial, config.remote_path, cancel=cancel)
        except AdbError as exc:
            raise ConfigVerificationError(f"Verification read failed: {exc}", serial=serial) from exc
        if readback != new_bytes:
            raise ConfigVerificationError(
                f"Verification failed: file on device differs from the pushed content "
                f"(sha1 device={_sha1(readback)} expected={_sha1(new_bytes)}). Backup: {backup}",
                serial=serial,
            )
        check_doc = DOCUMENT_TYPES[config.format].from_bytes(readback)
        verified: dict[str, str | None] = {}
        mismatches = []
        for p, value in params:
            actual = check_doc.get(p.section, p.key, p.section_type)
            verified[p.id] = actual
            if actual != value:
                mismatches.append(f"{p.key}: expected {value!r}, got {actual!r}")
        if mismatches:
            raise ConfigVerificationError("Verification failed: " + "; ".join(mismatches), serial=serial)

        for pid in changed:
            p = config.parameter(pid)
            self._log.success(f"{p.key} updated to {verified[pid]!r}", device=serial)

        for cmd in config.post_write_commands:
            self._adb.shell(serial, cmd, cancel=cancel, check=False)

        self._log.success(f"{config.title} written and verified", device=serial)
        return WriteResult(serial, config, dict(values), changed, verified, backup, diff)

    # ------------------------------------------------------------------
    def _backup(self, serial: str, config: ConfigFileDef, data: bytes) -> Path | None:
        if self._backup_dir is None:
            return None
        try:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_serial = "".join(c if c.isalnum() or c in "-_." else "_" for c in serial)
            path = self._backup_dir / safe_serial / f"{config.id}_{stamp}.bak"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            self._log.info(f"Backup saved to {path}", device=serial)
            return path
        except OSError as exc:
            self._log.warning(f"Could not save local backup: {exc}", device=serial)
            return None


def unified_diff(old: bytes, new: bytes, name: str) -> str:
    a = old.decode("utf-8", "replace").splitlines(keepends=True)
    b = new.decode("utf-8", "replace").splitlines(keepends=True)
    return "".join(difflib.unified_diff(a, b, f"a{name}", f"b{name}", n=2))


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()[:10]


def _assert_only_expected_lines_changed(old: bytes, new: bytes, changed: set[int], inserted: bool) -> None:
    """Safety net: guarantee no line other than the targeted ones was touched."""
    a = old.splitlines(keepends=True)
    b = new.splitlines(keepends=True)
    if not inserted:
        if len(a) != len(b):
            raise ConfigWriteError("Internal safety check failed: line count changed")
        for i, (x, y) in enumerate(zip(a, b)):
            if x != y and i not in changed:
                raise ConfigWriteError(f"Internal safety check failed: unexpected change on line {i + 1}")
