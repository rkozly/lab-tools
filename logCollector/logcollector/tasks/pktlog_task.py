"""
PKTLOG capture: configure pktlog on a radio, wait, disable, copy the
buffer to /tmp on the device and pull it to the PC.
"""

import os

from ..config import PktlogProfile
from ..core.paths import check_output_file
from .base_task import BaseTask, State, TaskError

# Timeouts (seconds) for individual device commands.
ROOT_TIMEOUT = 60
SHELL_TIMEOUT = 30
COPY_TIMEOUT = 120   # copying the 30 MB buffer out of /proc can be slow
PULL_TIMEOUT = 600


class PktlogTask(BaseTask):
    def __init__(self, label: str, serial: str, profile: PktlogProfile,
                 duration: int, destination: str, copy_only: bool = False,
                 parent=None):
        super().__init__(device=label, name=f"PKTLOG {profile.radio}", parent=parent)
        self.serial = serial
        self.profile = profile
        self.duration = duration
        self.destination = destination
        # copy_only: skip the capture and only redo "cp" + "adb pull".
        # Used by Retry Copy; pktlog is already disabled, so the buffer on
        # the device still holds the last capture.
        self.copy_only = copy_only
        # True once the capture window is over: a failure after this point
        # can be retried with copy_only.
        self.captured = copy_only

    def shell(self, command: str, timeout=SHELL_TIMEOUT, **kwargs):
        # wait-for-device on every call: adbd restarts after `adb root`.
        return self.adb(self.serial, "wait-for-device", "shell", command,
                        timeout=timeout, **kwargs)

    def execute(self):
        self.status.emit(State.WAITING, "Checking output path...")
        check_output_file(self.destination)

        self.status.emit(State.WAITING, "Waiting for device (root)...")
        self.adb(self.serial, "wait-for-device", "root",
                 timeout=ROOT_TIMEOUT, check=True)
        if not self.copy_only:
            self._capture()
        return self._copy_and_pull()

    def _capture(self):
        p = self.profile
        enabled = False
        try:
            self.status.emit(State.COLLECTING, "Configuring pktlog...")
            for cmd in p.setup_commands():
                rc, out, err = self.shell(cmd)
                if rc != 0 and cmd.startswith("pktlogconf"):
                    raise TaskError(self.describe_failure(["adb", "-s", self.serial, cmd],
                                                          rc, err or out))
                # wifitool failures are logged but not fatal.
                enabled = True

            self.sleep_interruptible(
                self.duration,
                on_tick=lambda s: self.status.emit(State.COLLECTING,
                                                   f"Collecting... {s}s left"))
        finally:
            # Always turn pktlog off again, also after Stop or an error.
            if enabled:
                self.shell(p.disable_command(), interruptible=False)

        self.check_stop()
        self.captured = True

    def _copy_and_pull(self):
        p = self.profile
        self.status.emit(State.COLLECTING, "Copying log on device...")
        self.shell(p.copy_command(), timeout=COPY_TIMEOUT, check=True)

        self.status.emit(State.COLLECTING, "Pulling log...")
        self.adb(self.serial, "pull", p.remote_file, self.destination,
                 timeout=PULL_TIMEOUT, check=True)

        if not os.path.isfile(self.destination) or os.path.getsize(self.destination) == 0:
            raise TaskError(f"Pulled file is missing or empty:\n{self.destination}")
        size_kb = os.path.getsize(self.destination) / 1024
        return State.COMPLETED, f"Completed ({size_kb:,.0f} KB)"
