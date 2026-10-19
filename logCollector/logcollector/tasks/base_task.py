"""
Base class for every background job (device scan, collections, translation).

A task is a QThread. Subclasses implement `execute()` and use the helpers
here to run commands, so they automatically get:
  * hidden, tracked subprocesses
  * responsive Stop (commands are polled and killed when stop is requested)
  * logging of every command and its result
  * a single `done` signal with the final state

To add a new collection type, subclass BaseTask, implement execute(),
and create a panel that starts it. Nothing else needs to change.
"""

import subprocess
import threading
import time

from PySide6.QtCore import QThread, Signal

from ..app_logging import get_logger, log_command
from ..core.adb import ensure_server, find_adb
from ..core.paths import PathError
from ..core.process import ManagedProcess

# Seconds to wait for output pipes to close after killing a command.
KILL_GRACE = 3


class State:
    """Status values shown in the UI (also used to pick colors)."""
    IDLE = "idle"
    WAITING = "waiting"
    COLLECTING = "collecting"
    STOPPING = "stopping"
    STOPPED = "stopped"
    TRANSLATING = "translating"
    COMPLETED = "completed"
    ERROR = "error"


class TaskError(Exception):
    """Expected failure with a message suitable for the user."""


class TaskStopped(Exception):
    """Raised internally when the user pressed Stop."""


class BaseTask(QThread):
    # (state, message) - progress updates
    status = Signal(str, str)
    # (state, message) - final result: completed / stopped / error
    done = Signal(str, str)

    # Strong references to running tasks. Keeps the QThread objects alive
    # and lets the main window stop everything on exit.
    _active: set["BaseTask"] = set()

    def __init__(self, device: str = "-", name: str = "task", parent=None):
        super().__init__(parent)
        self.device = device   # role/serial used in log lines
        self.name = name       # human readable task name (e.g. "CNSS")
        self._stop = threading.Event()
        self.log = get_logger(self.__class__.__name__)
        self.finished.connect(self._on_finished)

    # ----- lifecycle --------------------------------------------------------

    def start(self, *args, **kwargs):
        BaseTask._active.add(self)
        super().start(*args, **kwargs)

    def _on_finished(self):
        BaseTask._active.discard(self)

    @classmethod
    def active_tasks(cls) -> list["BaseTask"]:
        return [t for t in cls._active if t.isRunning()]

    @classmethod
    def stop_all(cls, wait_ms: int = 3000) -> None:
        tasks = list(cls._active)
        for t in tasks:
            t.stop()
        for t in tasks:
            t.wait(wait_ms)

    def stop(self) -> None:
        """Request the task to stop. Safe to call from the GUI thread."""
        if not self._stop.is_set() and self.isRunning():
            self._stop.set()
            self.status.emit(State.STOPPING, "Stopping...")

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    def run(self):
        """QThread entry point. Do not override; implement execute()."""
        self.log.info("%s started", self.name, extra={"device": self.device})
        try:
            state, message = self.execute()
        except TaskStopped:
            state, message = State.STOPPED, "Stopped"
        except (TaskError, PathError) as e:
            state, message = State.ERROR, str(e)
        except Exception as e:  # subprocess crash, unexpected bug, ...
            self.log.exception("Unexpected error", extra={"device": self.device})
            state, message = State.ERROR, f"Unexpected error: {e}"

        level = self.log.error if state == State.ERROR else self.log.info
        level("%s finished: %s - %s", self.name, state,
              message.replace("\n", " "), extra={"device": self.device})
        self.done.emit(state, message)

    def execute(self) -> tuple[str, str]:
        """Do the work. Return (final_state, message). Raise TaskError on failure."""
        raise NotImplementedError

    # ----- helpers for subclasses ------------------------------------------

    def check_stop(self) -> None:
        if self._stop.is_set():
            raise TaskStopped()

    def sleep_interruptible(self, seconds: float, on_tick=None) -> None:
        """Sleep on the PC side, waking up immediately when Stop is pressed.

        on_tick(remaining_seconds) is called about once per second.
        """
        end = time.monotonic() + seconds
        last_tick = None
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            if on_tick and int(remaining) != last_tick:
                last_tick = int(remaining)
                on_tick(last_tick + 1)
            if self._stop.wait(min(remaining, 0.25)):
                raise TaskStopped()

    def run_cmd(self, args: list[str], stdout_file=None, cwd: str | None = None,
                timeout: float | None = None, interruptible: bool = True,
                check: bool = False) -> tuple[int, str, str]:
        """Run a hidden command and wait for it.

        stdout_file    - open binary file object to receive stdout (replaces `>`)
        timeout        - seconds before the process is killed (None = no limit)
        interruptible  - kill the process as soon as Stop is pressed
        check          - raise TaskError when the exit code is not 0

        Returns (returncode, stdout_text, stderr_text).
        """
        try:
            proc = ManagedProcess(args, stdout=stdout_file, cwd=cwd)
        except FileNotFoundError:
            log_command(self.device, args, False, None, "executable not found")
            raise TaskError(f"Program not found: {args[0]}")
        except OSError as e:
            log_command(self.device, args, False, None, str(e))
            raise TaskError(f"Could not start {args[0]}: {e}")

        start = time.monotonic()
        killed_reason = ""
        killed_at = 0.0
        out = err = b""
        while True:
            try:
                out, err = proc.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                pass
            if killed_reason:
                # Killed, but a leftover grandchild may still hold the output
                # pipes open. Do not wait for it forever.
                if (proc.popen.poll() is not None
                        and time.monotonic() - killed_at > KILL_GRACE):
                    proc.abandon()
                    break
                continue
            if interruptible and self._stop.is_set():
                killed_reason = "stopped by user"
            elif timeout and time.monotonic() - start > timeout:
                killed_reason = f"timed out after {timeout:.0f}s"
            else:
                continue
            killed_at = time.monotonic()
            proc.terminate()

        rc = proc.popen.returncode
        out_text = _decode(out)
        err_text = _decode(err)
        if killed_reason:
            err_text = (err_text + f"\n[{killed_reason}]").strip()
        log_command(self.device, args, rc == 0 and not killed_reason, rc, err_text)

        if killed_reason == "stopped by user":
            raise TaskStopped()
        if check and (rc != 0 or killed_reason):
            raise TaskError(self.describe_failure(args, rc, err_text or out_text))
        return rc, out_text, err_text

    # ----- adb helpers ------------------------------------------------------

    def adb_path(self) -> str:
        path = find_adb()
        if not path:
            raise TaskError("ADB is not installed or not in PATH.\n"
                            "Install Android platform-tools and add adb to PATH.")
        ensure_server(path)
        return path

    def adb(self, serial: str, *args, **kwargs) -> tuple[int, str, str]:
        return self.run_cmd([self.adb_path(), "-s", serial, *args], **kwargs)

    @staticmethod
    def describe_failure(args, rc, text: str) -> str:
        """Turn adb/subprocess errors into a short, friendly message."""
        low = text.lower()
        if "not found" in low and "device" in low:
            return "Device disconnected or not found."
        if "offline" in low:
            return "Device is offline."
        if "unauthorized" in low:
            return "Device is unauthorized. Accept the USB debugging prompt."
        if "permission denied" in low:
            return "Permission denied."
        if "timed out" in low:
            return "Command timed out."
        last = text.strip().splitlines()[-1] if text.strip() else ""
        cmd = " ".join(args[3:] if len(args) > 3 else args)
        return f"Command failed (exit code {rc}): {cmd}" + (f"\n{last}" if last else "")


def _decode(data) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    return data.decode("utf-8", errors="replace")
