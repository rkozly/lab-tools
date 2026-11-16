"""
Wifistats recording: runs the AP / CPE command list (see config) every N
seconds and appends the output, with a timestamp, to a text file until
stopped.

All commands of one round run in a single `adb shell` call, so a round costs
one adb connection, and each command's output gets a "$ <command>" header.
A round that fails (device rebooting, adb hiccup) is noted in the file and
recording goes on with the next round.
"""

import time

from ..config import wifistats_commands
from ..core.paths import check_output_file
from .base_task import BaseTask, State, TaskError, TaskStopped

ROUND_TIMEOUT = 60
# Consecutive failed rounds after which the recording gives up.
MAX_FAILED_ROUNDS = 30


def build_round_script(commands: list[str]) -> str:
    """One device shell script running every command under a header line.

    Header lines use printf with %s, so quotes, |, [ ] and \\ inside the
    commands are printed exactly as written. The { } group sends the errors
    of every command in a pipe (not only the last one) to the file.
    """
    parts = []
    for cmd in commands:
        quoted = "'" + cmd.replace("'", "'\\''") + "'"
        parts.append(f"printf '$ %s\\n' {quoted}; {{ {cmd}; }} 2>&1")
    return "; ".join(parts)


class WifistatsTask(BaseTask):
    def __init__(self, role: str, serial: str, output_path: str, interval: int,
                 parent=None):
        super().__init__(device=role, name="Wifistats recording", parent=parent)
        self.role = role
        self.serial = serial
        self.output_path = output_path
        self.interval = max(1, int(interval))
        self.script = build_round_script(wifistats_commands(role))

    def execute(self):
        self.status.emit(State.WAITING, "Checking output path...")
        check_output_file(self.output_path)

        rounds = failed = 0
        try:
            with open(self.output_path, "ab") as f:
                while True:
                    started = time.monotonic()
                    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                    rc, out, err = self.adb(self.serial, "shell", self.script,
                                            timeout=ROUND_TIMEOUT)
                    rounds += 1
                    text = f"\n===== {stamp} | {self.role} | round {rounds} =====\n"
                    if rc == 0:
                        failed = 0
                        text += out.replace("\r\n", "\n")
                    else:
                        failed += 1
                        reason = self.describe_failure(["adb", "shell"], rc, err or out)
                        text += f"[round failed: {reason.splitlines()[0]}]\n"
                    f.write(text.encode("utf-8"))
                    f.flush()

                    if failed >= MAX_FAILED_ROUNDS:
                        raise TaskError(f"{failed} rounds in a row failed - "
                                        f"device disconnected?\n{reason}")
                    state = State.COLLECTING if failed == 0 else State.WAITING
                    note = "" if failed == 0 else f" ({failed} failed, device lost?)"
                    self.status.emit(state, f"Recording... round {rounds}{note}")
                    # Interval counts from the start of the round, so rounds
                    # stay N seconds apart however long the commands take.
                    self.sleep_interruptible(
                        max(0.0, self.interval - (time.monotonic() - started)))
        except TaskStopped:
            return State.STOPPED, f"Stopped - {rounds} round{'s' if rounds != 1 else ''} saved"
        except PermissionError:
            raise TaskError(f"Permission denied writing to:\n{self.output_path}")
        except OSError as e:
            raise TaskError(f"Cannot write output file:\n{e}")
