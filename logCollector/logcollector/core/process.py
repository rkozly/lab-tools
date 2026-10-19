"""
Subprocess handling.

Rules enforced here for every external command:
  * no console window ever appears (CREATE_NO_WINDOW + SW_HIDE)
  * no shell is used; output redirection is done in Python
  * every process is tracked so it can be killed on shutdown
  * the whole app runs inside a Windows Job Object with
    KILL_ON_JOB_CLOSE, so if the GUI crashes or is killed from Task
    Manager, Windows terminates every child (adb, python ...) for us.
"""

import os
import subprocess
import sys
import threading

from ..app_logging import get_logger

log = get_logger("process")

IS_WINDOWS = sys.platform == "win32"


# ---------------------------------------------------------------------------
# Windows Job Object (ctypes only, no pywin32 dependency)
# ---------------------------------------------------------------------------

_job_handle = None


def init_job_object() -> bool:
    """Put the current process into a kill-on-close job.

    Child processes inherit the job automatically, so nothing we start
    can outlive us. Returns True on success.
    """
    global _job_handle
    if not IS_WINDOWS or _job_handle is not None:
        return _job_handle is not None

    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMIT),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JobObjectExtendedLimitInformation = 9

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.GetCurrentProcess.restype = wintypes.HANDLE

    job = k32.CreateJobObjectW(None, None)
    if not job:
        log.warning("CreateJobObject failed (%s)", ctypes.get_last_error())
        return False

    info = EXTENDED_LIMIT()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                       ctypes.byref(info), ctypes.sizeof(info)):
        log.warning("SetInformationJobObject failed (%s)", ctypes.get_last_error())
        return False

    if not k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
        # Can fail on very old Windows if we are already inside a job.
        log.warning("AssignProcessToJobObject failed (%s)", ctypes.get_last_error())
        return False

    _job_handle = job  # keep the handle open for the lifetime of the app
    log.info("Job object active: child processes are killed when the app exits")
    return True


# ---------------------------------------------------------------------------
# Process registry
# ---------------------------------------------------------------------------

_live: set["ManagedProcess"] = set()
_live_lock = threading.Lock()


def kill_all(timeout: float = 2.0) -> None:
    """Terminate every process that is still running (used on shutdown)."""
    with _live_lock:
        procs = list(_live)
    for p in procs:
        p.terminate()
    for p in procs:
        p.wait_or_kill(timeout)


def running_count() -> int:
    with _live_lock:
        return len(_live)


# ---------------------------------------------------------------------------
# ManagedProcess
# ---------------------------------------------------------------------------

def _hidden_popen_kwargs() -> dict:
    if not IS_WINDOWS:
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return {"startupinfo": si, "creationflags": subprocess.CREATE_NO_WINDOW}


def _default_cwd() -> str | None:
    """Working directory for commands started without an explicit cwd.

    When the app is launched from a network share, children would inherit
    either a UNC directory or the temporary drive letter created by
    `pushd` in the launcher, which is removed again by `popd`. Some
    launchers, like the Chocolatey adb shim, then fail with "The directory
    name is invalid", so use the user's home folder, as a normal shell would.
    """
    if not IS_WINDOWS:
        return None
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = ""
    if not cwd or cwd.startswith(("\\\\", "//")) or not os.path.isdir(cwd):
        return os.path.expanduser("~")
    return None


def start_detached(args: list[str], timeout: float = 30) -> bool:
    """Run a hidden command without capturing its output; True if rc == 0.

    Used for `adb start-server`: the server keeps running in the
    background, and it must not inherit any pipe we read from.
    """
    try:
        rc = subprocess.run(args, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=timeout, cwd=_default_cwd(),
                            **_hidden_popen_kwargs()).returncode
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("%s failed: %s", " ".join(args), e)
        return False
    return rc == 0


class ManagedProcess:
    """Thin wrapper around Popen that is hidden and tracked."""

    def __init__(self, args: list[str], stdout=None, cwd: str | None = None):
        self.args = args
        self.popen = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=stdout if stdout is not None else subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd if cwd is not None else _default_cwd(),
            **_hidden_popen_kwargs(),
        )
        with _live_lock:
            _live.add(self)

    @property
    def pid(self) -> int:
        return self.popen.pid

    def poll(self):
        rc = self.popen.poll()
        if rc is not None:
            self._forget()
        return rc

    def communicate(self, timeout: float | None = None):
        """Same as Popen.communicate; raises TimeoutExpired, safe to retry."""
        out, err = self.popen.communicate(timeout=timeout)
        self._forget()
        return out, err

    def terminate(self) -> None:
        """Kill the process and everything it started.

        Killing only the direct child is not enough: wrappers such as the
        Chocolatey adb shim run the real adb.exe as a child, which keeps our
        output pipes open, so the command never ends ("Stopping" forever).
        """
        try:
            if self.popen.poll() is not None:
                return
            if IS_WINDOWS:
                subprocess.run(["taskkill", "/PID", str(self.popen.pid), "/T", "/F"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=10,
                               cwd=_default_cwd(), **_hidden_popen_kwargs())
            if self.popen.poll() is None:
                self.popen.terminate()  # TerminateProcess on Windows
        except (OSError, subprocess.SubprocessError):
            pass

    def abandon(self) -> None:
        """Stop tracking a process whose pipes never close (already killed)."""
        self._forget()

    def wait_or_kill(self, timeout: float = 2.0) -> None:
        try:
            self.popen.wait(timeout)
        except subprocess.TimeoutExpired:
            try:
                self.popen.kill()
                self.popen.wait(1)
            except (OSError, subprocess.TimeoutExpired):
                pass
        finally:
            self._forget()

    def _forget(self) -> None:
        with _live_lock:
            _live.discard(self)
