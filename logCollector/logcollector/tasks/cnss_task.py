"""
CNSS log collection: streams `cnss_diag -c` output to a file until stopped.

CongctrlTask does the same for congestion control logs: it first enables
CONGCTRL logging with wifitool, then streams `cnss_diag -c | grep CONGCTRL`.
"""

import os

from ..config import CONGCTRL_COMMAND, congctrl_setup_commands
from ..core.paths import check_output_file
from .base_task import BaseTask, State, TaskError, TaskStopped

ROOT_TIMEOUT = 60
SHELL_TIMEOUT = 30


class CnssTask(BaseTask):
    NAME = "CNSS collection"
    # Arguments after `adb -s <serial> wait-for-device shell`.
    REMOTE = ["cnss_diag", "-c"]

    def __init__(self, role: str, serial: str, output_path: str, parent=None):
        super().__init__(device=role, name=self.NAME, parent=parent)
        self.role = role
        self.serial = serial
        self.output_path = output_path

    def prepare(self) -> None:
        """Device setup before collecting. Nothing to do for plain CNSS."""

    def execute(self):
        self.status.emit(State.WAITING, "Checking output path...")
        check_output_file(self.output_path)
        self.check_stop()
        self.prepare()
        self.check_stop()

        self.status.emit(State.COLLECTING, "Collecting...")
        try:
            # Equivalent of:
            #   adb -s <serial> wait-for-device shell cnss_diag -c > <file>
            with open(self.output_path, "wb") as f:
                rc, _, err = self.adb(self.serial, "wait-for-device",
                                      "shell", *self.REMOTE,
                                      stdout_file=f)
        except PermissionError:
            raise TaskError(f"Permission denied writing to:\n{self.output_path}")
        except TaskStopped:
            self._kill_remote()
            raise
        except OSError as e:
            raise TaskError(f"Cannot write output file:\n{e}")

        # cnss_diag only exits by itself if the device rebooted or disconnected.
        # Whatever was collected is already in the file: end like a normal
        # Stop so the row goes back to idle and can be started again.
        size = _size(self.output_path)
        if rc != 0 and not size:
            raise TaskError(self.describe_failure(self.REMOTE, rc, err))
        return State.STOPPED, (f"Stopped (device rebooted?)\n"
                               f"cnss_diag exited. Log saved: {self.output_path} "
                               f"({size / 1024:,.0f} KB)")

    def _kill_remote(self):
        """Killing the local adb client normally ends cnss_diag on the device,
        but make sure it does not keep running in the background.

        Skipped while another CNSS / CONGCTRL collection runs on the same
        device: pkill would end that one too."""
        if any(isinstance(t, CnssTask) and t is not self and t.serial == self.serial
               for t in BaseTask.active_tasks()):
            return
        try:
            self.adb(self.serial, "shell", "pkill cnss_diag || killall cnss_diag",
                     timeout=10, interruptible=False)
        except TaskError:
            pass


class CongctrlTask(CnssTask):
    NAME = "CONGCTRL collection"
    # One argument, so the pipe runs in the device shell, like
    #   adb shell "cnss_diag -c | grep CONGCTRL" > <file>
    REMOTE = [CONGCTRL_COMMAND]

    def prepare(self) -> None:
        self.status.emit(State.WAITING, "Waiting for device (root)...")
        self.adb(self.serial, "wait-for-device", "root",
                 timeout=ROOT_TIMEOUT, check=True)
        self.status.emit(State.WAITING, "Enabling CONGCTRL logging...")
        for cmd in congctrl_setup_commands(self.role):
            # wait-for-device: adbd restarts after `adb root`.
            self.adb(self.serial, "wait-for-device", "shell", cmd,
                     timeout=SHELL_TIMEOUT, check=True)


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0
