"""Device preparation workflow (root, disable-verity, reboot, remount, ...).

The sequence is declared as data (:data:`PREPARE_STEPS`) so new steps or
alternative workflows can be added without touching the runner.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Sequence

from devreader.core.adb_service import WAIT_FOR_DEVICE_TIMEOUT, AdbResult, AdbService, DeviceState
from devreader.core.errors import (
    AdbError,
    CancelledError,
    DeviceNotFoundError,
    DeviceOfflineError,
    DeviceUnauthorizedError,
    PreparationError,
    RemountError,
    RootError,
)
from devreader.core.logging_service import LogService


@dataclass(frozen=True)
class PrepStep:
    title: str
    args: tuple[str, ...]
    timeout: float = WAIT_FOR_DEVICE_TIMEOUT
    fatal: bool = True
    error_cls: type[PreparationError] = PreparationError
    # Substrings (lowercase) in the output that indicate failure even with exit code 0.
    failure_markers: tuple[str, ...] = ()
    # After running, wait for the device to drop off the bus (used after reboot).
    expect_disconnect: bool = False


PREPARE_STEPS: Sequence[PrepStep] = (
    PrepStep(
        "Root", ("wait-for-device", "root"),
        error_cls=RootError,
        failure_markers=("cannot run as root", "production builds", "error:"),
    ),
    PrepStep(
        "Disable verity", ("wait-for-device", "disable-verity"),
        fatal=False,
        failure_markers=("error:", "failed"),
    ),
    PrepStep(
        "Reboot", ("wait-for-device", "reboot"),
        expect_disconnect=True,
    ),
    PrepStep(
        "Root (after reboot)", ("wait-for-device", "root"),
        timeout=300,
        error_cls=RootError,
        failure_markers=("cannot run as root", "production builds", "error:"),
    ),
    PrepStep(
        "Remount", ("wait-for-device", "remount"),
        error_cls=RemountError,
        failure_markers=("remount failed", "permission denied", "not running as root", "error:"),
    ),
    PrepStep(
        "SELinux permissive", ("wait-for-device", "shell", "setenforce 0"),
        fatal=False,
        timeout=60,
        failure_markers=("permission denied", "not found", "error"),
    ),
    PrepStep(
        "Remount / read-write", ("wait-for-device", "shell", "mount -o rw,remount /"),
        timeout=60,
        error_cls=RemountError,
        failure_markers=("permission denied", "denied", "not permitted", "error"),
    ),
)


# Subset of PREPARE_STEPS that makes / writable again without a reboot. Enough
# when verity is already disabled (Prepare ran once since the last flash).
REMOUNT_STEPS: Sequence[PrepStep] = tuple(
    s for s in PREPARE_STEPS
    if s.title in ("Root (after reboot)", "Remount", "SELinux permissive", "Remount / read-write")
)

READ_ONLY_MARKERS = ("read-only file system", "read only file system")


def is_read_only_error(exc: BaseException) -> bool:
    text = f"{exc} {getattr(exc, 'output', '') or ''}".lower()
    return any(m in text for m in READ_ONLY_MARKERS)


def make_writable(adb: AdbService, log: LogService, serial: str,
                  cancel: threading.Event | None = None) -> bool:
    """root + remount + setenforce 0 + mount -o rw,remount / (no reboot). True if all fatal steps passed."""
    log.warning("File system is read-only - trying root + remount (no reboot)", device=serial)
    report = PreparationService(adb, log, REMOUNT_STEPS).prepare(serial, cancel=cancel)
    return report.ok


def check_ready(adb: AdbService, serial: str) -> None:
    """Raise a specific :class:`AdbError` unless ``serial`` is connected and authorized."""
    state = adb.devices().get(serial, DeviceState.DISCONNECTED)
    if state == DeviceState.CONNECTED:
        return
    if state in (DeviceState.UNAUTHORIZED, DeviceState.AUTHORIZING, DeviceState.NO_PERMISSIONS):
        raise DeviceUnauthorizedError(
            "Device is unauthorized - accept the USB debugging prompt on the device", serial=serial
        )
    if state == DeviceState.OFFLINE:
        raise DeviceOfflineError("Device is offline - reconnect it or restart the adb server", serial=serial)
    if state == DeviceState.DISCONNECTED:
        raise DeviceNotFoundError("Device is not connected", serial=serial)
    raise AdbError(f"Device is in state '{state.value}', expected 'device'", serial=serial)


@dataclass
class StepReport:
    step: PrepStep
    ok: bool
    output: str
    duration: float
    error: str = ""


@dataclass
class PrepareReport:
    serial: str
    steps: list[StepReport]
    ok: bool
    warnings: int = 0
    error: str = ""

    @property
    def summary(self) -> str:
        if not self.ok:
            return f"Prepare failed: {self.error}"
        if self.warnings:
            return f"Prepared ({self.warnings} warning{'s' if self.warnings > 1 else ''})"
        return "Prepared"


# (serial, step_index, total_steps, step_title) -> None
ProgressFn = Callable[[str, int, int, str], None]
# (serial, text) -> None
OutputFn = Callable[[str, str], None]


class PreparationService:
    def __init__(self, adb: AdbService, log: LogService, steps: Sequence[PrepStep] = PREPARE_STEPS) -> None:
        self._adb = adb
        self._log = log
        self.steps = tuple(steps)

    def prepare(
        self,
        serial: str,
        *,
        cancel: threading.Event | None = None,
        on_progress: ProgressFn | None = None,
        on_output: OutputFn | None = None,
    ) -> PrepareReport:
        reports: list[StepReport] = []
        warnings = 0
        total = len(self.steps)
        self._log.info("Preparation started", device=serial)

        # ``wait-for-device`` blocks forever on unauthorized/offline/missing devices,
        # so fail fast with a clear message instead of waiting for the timeout.
        try:
            check_ready(self._adb, serial)
        except AdbError as exc:
            self._log.error(f"Cannot prepare: {exc}", device=serial)
            return PrepareReport(serial, reports, False, 0, str(exc))

        for idx, step in enumerate(self.steps):
            if on_progress:
                on_progress(serial, idx, total, step.title)
            self._log.info(f"Step {idx + 1}/{total}: {step.title}", device=serial)
            start = time.monotonic()
            try:
                result = self._adb.run(step.args, serial=serial, timeout=step.timeout, cancel=cancel)
                output = result.output
                if on_output and output:
                    on_output(serial, output)
                failure = self._failure_reason(step, result)
                if failure:
                    raise step.error_cls(
                        f"{step.title} failed: {failure}",
                        command=result.command_line, output=output, serial=serial,
                    )
                if step.expect_disconnect:
                    self._wait_for_disconnect(serial, cancel)
                reports.append(StepReport(step, True, output, time.monotonic() - start))
                self._log.success(f"{step.title} OK", device=serial)
            except CancelledError:
                reports.append(StepReport(step, False, "", time.monotonic() - start, "Cancelled"))
                self._log.warning("Preparation cancelled", device=serial)
                return PrepareReport(serial, reports, False, warnings, "Cancelled")
            except (AdbError, PreparationError) as exc:
                elapsed = time.monotonic() - start
                reports.append(StepReport(step, False, exc.output, elapsed, str(exc)))
                if step.fatal:
                    self._log.error(f"{step.title} failed - aborting preparation", device=serial,
                                    command=exc.command, error=exc.output or str(exc))
                    return PrepareReport(serial, reports, False, warnings, str(exc))
                warnings += 1
                self._log.warning(f"{step.title} failed (non-fatal) - continuing", device=serial,
                                  command=exc.command, error=exc.output or str(exc))

        if on_progress:
            on_progress(serial, total, total, "Done")
        report = PrepareReport(serial, reports, True, warnings)
        self._log.success(f"Preparation complete: {report.summary}", device=serial)
        return report

    @staticmethod
    def _failure_reason(step: PrepStep, result: AdbResult) -> str:
        text = result.output.lower()
        # Benign messages that contain failure-ish words.
        benign = ("already running as root", "verity is already disabled", "already disabled")
        if not result.ok:
            return result.output.splitlines()[0] if result.output else f"exit code {result.returncode}"
        if any(b in text for b in benign):
            return ""
        for marker in step.failure_markers:
            if marker in text:
                return next((ln for ln in result.output.splitlines() if marker in ln.lower()), marker)
        return ""

    def _wait_for_disconnect(self, serial: str, cancel: threading.Event | None, timeout: float = 45.0) -> None:
        """After ``reboot`` wait until the device leaves the bus.

        Otherwise the following ``wait-for-device`` can return immediately while
        the device is still shutting down, and the next command fails.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                raise CancelledError("Operation cancelled", serial=serial)
            try:
                if serial not in self._adb.devices():
                    self._log.info("Device went down for reboot", device=serial)
                    return
            except AdbError:
                pass
            time.sleep(1.0)
        self._log.warning("Device did not visibly disconnect after reboot; continuing", device=serial)
