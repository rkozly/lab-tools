"""
Traffic recording: every N seconds writes one line with the attenuation of
both attenuators and the throughput of the device's interfaces to a text
file, until stopped.

iperf itself is started by hand on the backbone PCs. The devices bridge its
traffic, so the throughput comes from the byte counters of ath0 (CPE) or
mld0 / mld1 (AP): (bytes now - bytes before) * 8 / elapsed seconds.

A round where the device cannot be read (rebooting, adb hiccup) is noted in
the file and recording goes on with the next round. While an attenuation
sweep runs, every line also names the current step ("step 3/9 2 dB").
"""

import time

from ..config import ATTENUATORS, traffic_ifaces
from ..core import attenuator
from ..core.paths import check_output_file
from .base_task import BaseTask, State, TaskError, TaskStopped

ROUND_TIMEOUT = 30
# Consecutive failed device reads after which the recording gives up.
MAX_FAILED_ROUNDS = 30


def counters_script(ifaces: list[str]) -> str:
    """Device shell script printing "<iface> <rx_bytes> <tx_bytes>" per interface
    ("-" for an interface that does not exist)."""
    parts = []
    for i in ifaces:
        stat = f"/sys/class/net/{i}/statistics"
        parts.append(f"echo {i} $(cat {stat}/rx_bytes 2>/dev/null || echo -) "
                     f"$(cat {stat}/tx_bytes 2>/dev/null || echo -)")
    return "; ".join(parts)


def parse_counters(text: str) -> dict[str, tuple[int | None, int | None]]:
    """Output of counters_script -> {iface: (rx_bytes, tx_bytes)}."""
    result = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        values = [int(v) if v.isdigit() else None for v in parts[1:]]
        result[parts[0]] = (values[0], values[1])
    return result


def mbps(before: int | None, now: int | None, seconds: float) -> float | None:
    """Throughput between two byte counters. None if unknown or the counter
    went back (interface recreated / device rebooted)."""
    if before is None or now is None or seconds <= 0 or now < before:
        return None
    return (now - before) * 8 / seconds / 1e6


def _fmt(value: float | None) -> str:
    return "?" if value is None else f"{value:.1f}"


def format_rates(ifaces: list[str], prev: dict, cur: dict, seconds: float) -> str:
    """'ath0 rx 182.4 tx 3.1 Mbit/s', plus a total when there are several ifaces."""
    parts = []
    total_rx = total_tx = 0.0
    complete = True
    for i in ifaces:
        if i not in cur or cur[i] == (None, None):
            parts.append(f"{i} missing")
            complete = False
            continue
        rx = mbps(prev.get(i, (None, None))[0], cur[i][0], seconds)
        tx = mbps(prev.get(i, (None, None))[1], cur[i][1], seconds)
        if rx is None or tx is None:
            complete = False
        else:
            total_rx += rx
            total_tx += tx
        parts.append(f"{i} rx {_fmt(rx)} tx {_fmt(tx)} Mbit/s")
    if len(ifaces) > 1 and complete:
        parts.append(f"total rx {total_rx:.1f} tx {total_tx:.1f} Mbit/s")
    return " | ".join(parts)


class TrafficTask(BaseTask):
    def __init__(self, role: str, serial: str, output_path: str, interval: int,
                 attenuators=ATTENUATORS, parent=None):
        super().__init__(device=role, name="Traffic recording", parent=parent)
        self.role = role
        self.serial = serial
        self.output_path = output_path
        self.interval = max(1, int(interval))
        self.attenuators = list(attenuators)
        self.ifaces = traffic_ifaces(role)
        self.script = counters_script(self.ifaces)

    def execute(self):
        self.status.emit(State.WAITING, "Checking output path...")
        check_output_file(self.output_path)

        lines = failed = 0
        prev, prev_time = {}, 0.0
        last_att = None
        reason = ""
        try:
            with open(self.output_path, "ab") as f:
                f.write((f"\n# Traffic recording {self.role} started "
                         f"{time.strftime('%Y-%m-%d %H:%M:%S')} | ifaces "
                         f"{' '.join(self.ifaces)} | interval {self.interval} s\n"
                         ).encode("utf-8"))
                f.flush()
                while True:
                    started = time.monotonic()
                    readings = attenuator.read_all(self.attenuators)
                    att = attenuator.format_readings(readings)
                    step = attenuator.sweep_note()
                    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                    rc, out, err = self.adb(self.serial, "shell", self.script,
                                            timeout=ROUND_TIMEOUT)
                    now = time.monotonic()

                    mark = "  <-- changed" if last_att is not None and att != last_att else ""
                    last_att = att
                    if rc == 0:
                        failed = 0
                        cur = parse_counters(out)
                        if prev:
                            traffic = format_rates(self.ifaces, prev, cur, now - prev_time)
                        else:
                            traffic = "baseline (rates start with the next line)"
                        prev, prev_time = cur, now
                    else:
                        failed += 1
                        reason = self.describe_failure(["adb", "shell"], rc, err or out)
                        traffic = f"[device read failed: {reason.splitlines()[0]}]"
                        prev = {}   # rates restart from a fresh baseline
                    sweep = f" | {step}" if step else ""
                    f.write(f"{stamp} | att {att}{mark}{sweep} | {traffic}\n".encode("utf-8"))
                    f.flush()
                    lines += 1

                    if failed >= MAX_FAILED_ROUNDS:
                        raise TaskError(f"{failed} reads in a row failed - "
                                        f"device disconnected?\n{reason}")
                    state = State.COLLECTING if failed == 0 else State.WAITING
                    note = "" if failed == 0 else f" ({failed} failed, device lost?)"
                    self.status.emit(state, f"Recording... att {att}{note}\n{traffic}")
                    # Interval counts from the start of the round, so lines
                    # stay N seconds apart however long the reads take.
                    self.sleep_interruptible(
                        max(0.0, self.interval - (time.monotonic() - started)))
        except TaskStopped:
            return State.STOPPED, f"Stopped - {lines} line{'s' if lines != 1 else ''} saved"
        except PermissionError:
            raise TaskError(f"Permission denied writing to:\n{self.output_path}")
        except OSError as e:
            raise TaskError(f"Cannot write output file:\n{e}")
