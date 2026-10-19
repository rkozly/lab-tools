"""
wpa_supplicant (CPE) and hostapd (AP) debug logs.

DaemonCheckTask  checks that the start-up files contain the debug changes
                 AND that the running daemon was started with them; with
                 fix=True it applies the changes, restarts (or reboots) and
                 checks again.
DaemonLogTask    streams the daemon's log file to the PC while it is being
                 written (`tail -f` over adb), so a crash or reboot loses
                 nothing that was already logged. After a reboot it waits for
                 the device and continues in the same file.
"""

import os
import shutil
import tempfile
import time

from PySide6.QtCore import Signal

from ..config import DaemonLogProfile
from ..core.daemon_config import (DONE, MISSING, NEEDED, apply_edits, edit_status,
                                  has_flags, pick_global_process,
                                  process_list_command)
from ..core.paths import check_output_file
from .base_task import BaseTask, State, TaskError, TaskStopped

SHELL_TIMEOUT = 30
RESTART_TIMEOUT = 180     # `wifi` reload can take a while
VERIFY_TIMEOUT = 300      # waiting for the daemon after restart / reboot
VERIFY_INTERVAL = 5


def _check_device(task: BaseTask, profile: DaemonLogProfile, serial: str) -> dict:
    """Read the device state. Returns a report dict (see DaemonCheckTask)."""
    files = {}
    for path in profile.files():
        rc, out, err = task.adb(serial, "wait-for-device", "shell", f"cat {path}",
                                timeout=SHELL_TIMEOUT)
        if rc != 0 or "no such file" in (out + err).lower():
            files[path] = MISSING
            continue
        states = [edit_status(out, e) for e in profile.edits if e.path == path]
        files[path] = (MISSING if MISSING in states
                       else NEEDED if NEEDED in states else DONE)

    rc, out, _ = task.adb(serial, "wait-for-device", "shell",
                          process_list_command(profile.binary), timeout=SHELL_TIMEOUT)
    cmdline = pick_global_process(out) if rc == 0 else None
    process_ok = bool(cmdline) and has_flags(cmdline, profile.process_flags)

    files_ok = all(s == DONE for s in files.values())
    missing = [p for p, s in files.items() if s == MISSING]
    if missing:
        message = ("Expected line not found in " + ", ".join(missing)
                   + " - this firmware differs, change it by hand")
    elif not files_ok:
        message = "Debug logging not enabled in " + ", ".join(
            p for p, s in files.items() if s == NEEDED)
    elif not cmdline:
        message = f"Files OK, but {profile.binary} is not running"
    elif not process_ok:
        message = f"Files OK, but {profile.binary} was not restarted yet"
    else:
        message = "Verified - debug logging enabled"
    return {"files": files, "cmdline": cmdline or "", "files_ok": files_ok,
            "process_ok": process_ok, "ok": files_ok and process_ok,
            "fixable": not missing, "message": message}


class DaemonCheckTask(BaseTask):
    # report: files {path: done/needed/missing}, cmdline, files_ok,
    #         process_ok, ok, fixable, message
    result = Signal(dict)

    def __init__(self, profile: DaemonLogProfile, role: str, serial: str,
                 fix: bool = False, reboot: bool = False, parent=None):
        super().__init__(device=role, name=f"{profile.label} check", parent=parent)
        self.profile = profile
        self.serial = serial
        self.fix = fix
        self.reboot = reboot

    def execute(self):
        self.status.emit(State.WAITING, "Checking device...")
        # Needed for writing /etc; harmless if adbd already runs as root.
        self.adb(self.serial, "wait-for-device", "root", timeout=60)
        report = _check_device(self, self.profile, self.serial)

        if self.fix and not report["ok"]:
            if not report["fixable"]:
                self.result.emit(report)
                raise TaskError(report["message"])
            if not report["files_ok"]:
                self._apply_edits()
            self._restart()
            report = self._wait_until_verified()

        self.result.emit(report)
        return (State.COMPLETED if report["ok"] else State.ERROR), report["message"]

    def _apply_edits(self):
        tmp = tempfile.mkdtemp(prefix="logcollector_")
        try:
            for path in self.profile.files():
                edits = [e for e in self.profile.edits if e.path == path]
                self.status.emit(State.WAITING, f"Changing {path}...")
                local = os.path.join(tmp, os.path.basename(path))
                self.adb(self.serial, "pull", path, local, timeout=60, check=True)
                with open(local, "rb") as f:
                    # latin-1 maps every byte to one character: lossless.
                    text = f.read().decode("latin-1")
                new = apply_edits(text, edits)
                if new == text:
                    continue
                with open(local, "wb") as f:
                    f.write(new.encode("latin-1"))
                staged = f"/tmp/{os.path.basename(path)}.logcollector"
                self.adb(self.serial, "push", local, staged, timeout=60, check=True)
                # Keep the first original as a backup. `cat >` instead of
                # moving the file keeps its permissions (init scripts must
                # stay executable).
                self.adb(self.serial, "shell",
                         f"[ -f {path}.orig ] || cp -p {path} {path}.orig; "
                         f"cat {staged} > {path} && rm {staged} && sync",
                         timeout=SHELL_TIMEOUT, check=True)
                self.log.info("Changed %s (backup %s.orig)", path, path,
                              extra={"device": self.device})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _restart(self):
        if self.reboot:
            self.status.emit(State.WAITING, "Rebooting device...")
            # The connection drops during reboot, so the result is ignored.
            self.adb(self.serial, "shell", "reboot", timeout=20)
            self.sleep_interruptible(15)  # do not see the old system again
            return
        for cmd in self.profile.restart_commands:
            self.status.emit(State.WAITING, f"Running '{cmd}'...")
            rc, out, err = self.adb(self.serial, "wait-for-device", "shell", cmd,
                                    timeout=RESTART_TIMEOUT)
            if rc != 0:
                self.log.warning("'%s' returned %s: %s", cmd, rc, (err or out).strip(),
                                 extra={"device": self.device})

    def _wait_until_verified(self) -> dict:
        end = time.monotonic() + VERIFY_TIMEOUT
        while True:
            self.status.emit(State.WAITING, f"Waiting for {self.profile.binary}...")
            report = _check_device(self, self.profile, self.serial)
            if report["ok"] or time.monotonic() > end:
                if not report["ok"]:
                    report["message"] += " (after restart; try Reboot)"
                return report
            self.sleep_interruptible(VERIFY_INTERVAL)


class DaemonLogTask(BaseTask):
    # Restarts of the stream that end this quickly count as failures.
    QUICK_EXIT = 3
    MAX_QUICK_EXITS = 5

    def __init__(self, profile: DaemonLogProfile, role: str, serial: str,
                 output_path: str, parent=None):
        super().__init__(device=role, name=f"{profile.label} collection", parent=parent)
        self.profile = profile
        self.serial = serial
        self.output_path = output_path

    def execute(self):
        p = self.profile
        self.status.emit(State.WAITING, "Checking output path...")
        check_output_file(self.output_path)

        self.status.emit(State.WAITING, f"Checking {p.binary}...")
        rc, out, _ = self.adb(self.serial, "wait-for-device", "shell",
                              process_list_command(p.binary), timeout=SHELL_TIMEOUT)
        cmdline = pick_global_process(out) if rc == 0 else None
        if not cmdline or not has_flags(cmdline, p.process_flags):
            raise TaskError(f"{p.binary} is not running with debug logging.\n"
                            "Untick and tick the device to check and fix it.")

        received = 0          # bytes of the device log already saved
        saved = 0             # bytes written to the output file
        boot = self._boot_id()
        quick_exits = 0
        try:
            with open(self.output_path, "ab") as f:
                self._mark(f, f"collection started, device log {p.remote_log}")
                first = True
                while True:
                    if not first:
                        self.status.emit(State.WAITING, "Device lost - waiting for it...")
                        self.adb(self.serial, "wait-for-device")
                        new_boot = self._boot_id()
                        size = self._remote_size()
                        if new_boot != boot:
                            self._mark(f, "device rebooted - new log starts here")
                            boot, received = new_boot, 0
                        elif size is not None and size < received:
                            self._mark(f, "device log was truncated - restarting from its start")
                            received = 0
                    first = False

                    self.status.emit(State.COLLECTING, "Collecting (live)...")
                    f.flush()
                    before = os.fstat(f.fileno()).st_size
                    started = time.monotonic()
                    rc, _, err = self.adb(self.serial, "shell", self._stream_cmd(received),
                                          stdout_file=f)
                    grown = os.fstat(f.fileno()).st_size - before
                    received += grown
                    saved += grown

                    if time.monotonic() - started < self.QUICK_EXIT and grown == 0:
                        quick_exits += 1
                        if quick_exits >= self.MAX_QUICK_EXITS:
                            raise TaskError(self.describe_failure(
                                ["adb", "-s", self.serial, "tail"], rc,
                                err or "log stream keeps stopping"))
                        self.sleep_interruptible(2)
                    else:
                        quick_exits = 0
        except TaskStopped:
            return State.STOPPED, f"Stopped - {_size(saved)} saved"
        except PermissionError:
            raise TaskError(f"Permission denied writing to:\n{self.output_path}")
        except OSError as e:
            raise TaskError(f"Cannot write output file:\n{e}")

    def _stream_cmd(self, offset: int) -> str:
        log = self.profile.remote_log
        # Wait for the file (it is created when the daemon starts after a
        # reboot), then follow it from the first byte not saved yet.
        return (f"while [ ! -f {log} ]; do sleep 1; done; "
                f"exec tail -c +{offset + 1} -f {log}")

    def _boot_id(self) -> str:
        rc, out, _ = self.adb(self.serial, "wait-for-device", "shell",
                              "cat /proc/sys/kernel/random/boot_id",
                              timeout=SHELL_TIMEOUT)
        return out.strip() if rc == 0 else ""

    def _remote_size(self) -> int | None:
        rc, out, _ = self.adb(self.serial, "shell",
                              f"wc -c < {self.profile.remote_log}", timeout=SHELL_TIMEOUT)
        try:
            return int(out.split()[0]) if rc == 0 else None
        except (ValueError, IndexError):
            return None

    @staticmethod
    def _mark(f, text: str) -> None:
        f.write(f"\n===== [logCollector {time.strftime('%Y-%m-%d %H:%M:%S')}] "
                f"{text} =====\n".encode())
        f.flush()


def _size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"
