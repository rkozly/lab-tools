"""Centralized ADB execution layer.

*All* interaction with ``adb`` goes through :class:`AdbService`. It is responsible
for locating the executable, running commands with timeouts and cancellation,
classifying errors, and logging every invocation.

The service is thread-safe and holds no per-device state, so it can be shared
by any number of concurrent worker threads.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Sequence

from devreader.core.errors import (
    AdbCommandError,
    AdbError,
    AdbNotFoundError,
    CancelledError,
    DeviceNotFoundError,
    DeviceOfflineError,
    DeviceUnauthorizedError,
    TimeoutError_,
)
from devreader.core.logging_service import LogService


class DeviceState(str, Enum):
    """Connection state of a device as reported by ``adb devices``."""

    CONNECTED = "device"
    OFFLINE = "offline"
    UNAUTHORIZED = "unauthorized"
    NO_PERMISSIONS = "no permissions"
    RECOVERY = "recovery"
    SIDELOAD = "sideload"
    BOOTLOADER = "bootloader"
    AUTHORIZING = "authorizing"
    CONNECTING = "connecting"
    DISCONNECTED = "disconnected"  # not listed by adb at all
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, text: str) -> "DeviceState":
        text = text.strip().lower()
        if text.startswith("no permissions"):
            return cls.NO_PERMISSIONS
        for state in cls:
            if state.value == text:
                return state
        return cls.UNKNOWN


@dataclass(frozen=True)
class AdbResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration: float
    stdout_bytes: bytes = b""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        """Combined stdout + stderr, stripped."""
        return "\n".join(p for p in (self.stdout.strip(), self.stderr.strip()) if p)

    @property
    def command_line(self) -> str:
        return format_command(self.args)


def format_command(args: Sequence[str]) -> str:
    return " ".join(f'"{a}"' if (" " in a or not a) else a for a in args)


# Default timeouts in seconds; individual calls may override.
DEFAULT_TIMEOUT = 30.0
WAIT_FOR_DEVICE_TIMEOUT = 180.0

# Remote directories tried (in order) for staging pushed files.
STAGING_DIRS = ("/tmp", "/data/local/tmp")

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _default_cwd() -> str | None:
    """Working directory for adb child processes.

    When DevReader is launched from a network share, children inherit either a
    UNC directory or the temporary drive letter created by ``pushd`` in the
    launcher. Wrappers such as the Chocolatey adb shim then fail to start the
    real adb.exe ("The directory name is invalid"), so use the user's home
    folder instead, as a normal shell would.
    """
    if os.name != "nt":
        return None
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = ""
    if not cwd or cwd.startswith(("\\\\", "//")) or not os.path.isdir(cwd):
        return os.path.expanduser("~")
    return None


class AdbService:
    """Runs adb commands. Construct once and share."""

    def __init__(self, log: LogService, adb_path: str | None = None) -> None:
        self._log = log
        self._explicit_path = adb_path
        self._resolved: str | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Executable discovery
    # ------------------------------------------------------------------
    @property
    def adb_path(self) -> str:
        """Absolute path to adb. Raises :class:`AdbNotFoundError` if missing."""
        with self._lock:
            if self._resolved and Path(self._resolved).exists():
                return self._resolved
            self._resolved = self._locate()
            return self._resolved

    def set_adb_path(self, path: str | None) -> None:
        with self._lock:
            self._explicit_path = path or None
            self._resolved = None

    def is_available(self) -> bool:
        try:
            _ = self.adb_path
            return True
        except AdbNotFoundError:
            return False

    def _locate(self) -> str:
        candidates: list[str | None] = [self._explicit_path, os.environ.get("ADB_PATH")]
        candidates.append(shutil.which("adb"))
        exe = "adb.exe" if os.name == "nt" else "adb"
        for var in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
            root = os.environ.get(var)
            if root:
                candidates.append(str(Path(root) / "platform-tools" / exe))
        if os.name == "nt":
            local = os.environ.get("LOCALAPPDATA")
            if local:
                candidates.append(str(Path(local) / "Android" / "Sdk" / "platform-tools" / exe))
            candidates.append(r"C:\platform-tools\adb.exe")
        for cand in candidates:
            if cand and Path(cand).is_file():
                return str(Path(cand).resolve())
        raise AdbNotFoundError(
            "adb executable not found. Install Android platform-tools and add it to PATH, "
            "set the ADB_PATH environment variable, or configure it in Settings."
        )

    # ------------------------------------------------------------------
    # Generic execution
    # ------------------------------------------------------------------
    def run(
        self,
        args: Sequence[str],
        *,
        serial: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        cancel: threading.Event | None = None,
        check: bool = False,
        quiet: bool = False,
        binary: bool = False,
    ) -> AdbResult:
        """Run ``adb [-s serial] <args>``.

        :param check: raise a classified :class:`AdbError` on non-zero exit.
        :param quiet: log at debug level only (for polling commands).
        :param binary: keep raw stdout bytes (``result.stdout_bytes``).
        """
        full = [self.adb_path]
        if serial:
            full += ["-s", serial]
        full += list(args)
        display = format_command(["adb", *full[1:]])

        if cancel is not None and cancel.is_set():
            raise CancelledError("Operation cancelled", command=display)

        start = time.monotonic()
        returncode, out_b, err_b = self._execute(full, timeout, cancel, display, serial)
        duration = time.monotonic() - start
        result = AdbResult(
            args=tuple(["adb", *full[1:]]),
            returncode=returncode,
            stdout=_decode(out_b),
            stderr=_decode(err_b),
            duration=duration,
            stdout_bytes=out_b if binary else b"",
        )

        log_fn = self._log.debug if quiet else (self._log.info if result.ok else self._log.warning)
        log_fn(
            f"adb exited with code {result.returncode} ({duration:.1f}s)",
            device=serial,
            command=display,
            result="" if binary else result.stdout,
            error=result.stderr,
        )

        if check and not result.ok:
            raise classify_error(result, serial)
        return result

    def _execute(self, full, timeout, cancel, display, serial) -> tuple[int, bytes, bytes]:
        """Run the process and return ``(returncode, stdout, stderr)``.

        Overridden by the simulator; everything else in this class is shared.
        """
        try:
            proc = subprocess.Popen(
                full,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=_default_cwd(),
                creationflags=_CREATE_NO_WINDOW,
            )
        except FileNotFoundError as exc:
            with self._lock:
                self._resolved = None
            raise AdbNotFoundError(f"Failed to start adb: {exc}") from exc
        except OSError as exc:
            raise AdbCommandError(f"Failed to start adb: {exc}", command=display) from exc
        out_b, err_b = self._communicate(proc, timeout, cancel, display, serial)
        return proc.returncode, out_b, err_b

    def _communicate(self, proc, timeout, cancel, display, serial) -> tuple[bytes, bytes]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill(proc)
                raise TimeoutError_(
                    f"Command timed out after {timeout:.0f}s", command=display, serial=serial
                )
            try:
                return proc.communicate(timeout=min(0.25, remaining))
            except subprocess.TimeoutExpired:
                if cancel is not None and cancel.is_set():
                    _kill(proc)
                    raise CancelledError("Operation cancelled", command=display, serial=serial)

    # ------------------------------------------------------------------
    # High level helpers
    # ------------------------------------------------------------------
    def devices(self) -> dict[str, DeviceState]:
        """Return ``{serial: state}`` for every device known to the adb server.

        If the first attempt fails (server not running, client/server version
        mismatch, stale server), the server is (re)started and the call retried once.
        """
        result = self.run(["devices"], timeout=20, quiet=True)
        if not result.ok:
            self._log.warning(
                f"'adb devices' failed (exit code {result.returncode}) - restarting adb server and retrying",
                command=result.command_line, error=result.output,
            )
            self.start_server()
            result = self.run(["devices"], timeout=20, quiet=True)
        if not result.ok:
            detail = result.output or f"exit code {result.returncode}, no output"
            raise AdbCommandError(
                f"'adb devices' failed using {self.adb_path}: {detail}",
                command=result.command_line, output=result.output,
            )
        return parse_devices_output(result.stdout)

    def start_server(self, timeout: float = 30.0) -> None:
        """Start (or restart after a version mismatch) the adb server.

        Output goes to DEVNULL on purpose: the server daemon inherits the
        child's handles, and with pipes ``communicate()`` would block until
        the daemon exits.
        """
        try:
            subprocess.run(
                [self.adb_path, "start-server"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=timeout, cwd=_default_cwd(), creationflags=_CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._log.warning(f"adb start-server failed: {exc}")

    def version(self) -> str:
        result = self.run(["version"], timeout=10, quiet=True)
        return result.stdout.splitlines()[0] if result.stdout else "unknown"

    def shell(
        self,
        serial: str,
        command: str,
        *,
        wait: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
        cancel: threading.Event | None = None,
        check: bool = True,
    ) -> AdbResult:
        """``adb -s <serial> [wait-for-device] shell "<command>"``."""
        args = (["wait-for-device"] if wait else []) + ["shell", command]
        return self.run(args, serial=serial, timeout=timeout, cancel=cancel, check=check)

    def read_file(self, serial: str, remote_path: str, *, cancel=None, timeout=DEFAULT_TIMEOUT) -> bytes:
        """Read a remote file byte-exact.

        Uses ``adb exec-out cat`` (no CR/LF translation); falls back to ``adb pull``
        for devices/adbd versions that do not support exec-out.
        """
        result = self.run(
            ["exec-out", "cat", remote_path],
            serial=serial, timeout=timeout, cancel=cancel, binary=True,
        )
        if result.ok and not _looks_like_cat_error(result, remote_path):
            return result.stdout_bytes
        if _looks_like_cat_error(result, remote_path):
            raise classify_error(result, serial, default_message=f"Cannot read {remote_path}")

        self._log.debug("exec-out failed; falling back to adb pull", device=serial)
        with tempfile.TemporaryDirectory(prefix="devreader_") as tmp:
            local = Path(tmp) / Path(remote_path).name
            self.pull(serial, remote_path, local, cancel=cancel, timeout=timeout)
            return local.read_bytes()

    def write_file(self, serial: str, remote_path: str, data: bytes, *, cancel=None, timeout=60.0) -> None:
        """Replace the contents of ``remote_path`` with ``data``.

        The data is pushed to a staging file and then copied over the target with
        ``cat staged > target``. Unlike pushing directly onto the target this keeps
        the file's inode, owner, permissions and SELinux label intact.
        """
        name = remote_path.rstrip("/").rsplit("/", 1)[-1]
        staged: str | None = None
        last_exc: AdbError | None = None
        with tempfile.TemporaryDirectory(prefix="devreader_") as tmp:
            local = Path(tmp) / name
            local.write_bytes(data)
            for staging_dir in STAGING_DIRS:
                remote_tmp = f"{staging_dir}/.devreader_{name}_{os.getpid()}"
                try:
                    self.push(serial, local, remote_tmp, cancel=cancel, timeout=timeout)
                    staged = remote_tmp
                    break
                except CancelledError:
                    raise
                except AdbError as exc:
                    last_exc = exc
        if staged is None:
            assert last_exc is not None
            raise last_exc

        marker = "__DEVREADER_RC="
        script = (
            f"cat '{staged}' > '{remote_path}'; rc=$?; sync; rm -f '{staged}'; echo {marker}$rc"
        )
        result = self.shell(serial, script, timeout=timeout, cancel=cancel, check=False)
        match = re.search(re.escape(marker) + r"(\d+)", result.stdout)
        if not match or match.group(1) != "0":
            detail = re.sub(re.escape(marker) + r"\d+", "", result.output).strip()
            raise AdbCommandError(
                f"Failed to write {remote_path}: {detail or 'unknown error'}",
                command=result.command_line, output=result.output, serial=serial,
            )

    def pull(self, serial: str, remote: str, local: Path, *, cancel=None, timeout=60.0) -> AdbResult:
        return self.run(["pull", remote, str(local)], serial=serial, timeout=timeout, cancel=cancel, check=True)

    def push(self, serial: str, local: Path, remote: str, *, cancel=None, timeout=60.0) -> AdbResult:
        result = self.run(["push", str(local), remote], serial=serial, timeout=timeout, cancel=cancel)
        # Some adb versions return 0 even when the remote side rejects the write.
        if not result.ok or "error" in result.output.lower() or "failed" in result.output.lower():
            raise classify_error(result, serial, default_message=f"Push to {remote} failed")
        return result


# ----------------------------------------------------------------------
# Parsing / classification helpers (pure functions, unit tested)
# ----------------------------------------------------------------------
def parse_devices_output(text: str) -> dict[str, DeviceState]:
    devices: dict[str, DeviceState] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("*") or line.lower().startswith("list of devices"):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            # "no permissions (...)" contains spaces; keep the full remainder.
            state_text = parts[1].split("  ")[0]
            devices[parts[0]] = DeviceState.parse(state_text)
    return devices


def classify_error(result: AdbResult, serial: str | None, default_message: str = "adb command failed"):
    text = result.output.lower()
    kwargs = dict(command=result.command_line, output=result.output, serial=serial)
    if "unauthorized" in text:
        return DeviceUnauthorizedError(
            "Device is unauthorized - accept the USB debugging prompt on the device", **kwargs
        )
    if "offline" in text:
        return DeviceOfflineError("Device is offline", **kwargs)
    if "not found" in text and ("device" in text or "no devices" in text):
        return DeviceNotFoundError("Device not found / disconnected", **kwargs)
    first = result.output.splitlines()[0] if result.output else f"exit code {result.returncode}"
    return AdbCommandError(f"{default_message}: {first}", **kwargs)


def _looks_like_cat_error(result: AdbResult, remote_path: str) -> bool:
    err = (result.stderr + _decode(result.stdout_bytes[:300])).lower()
    return remote_path.lower() in err and (
        "no such file" in err or "permission denied" in err or "is a directory" in err
    ) and len(result.stdout_bytes) < 400


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n")


def _kill(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
        proc.communicate(timeout=5)
    except Exception:
        pass
