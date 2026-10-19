"""In-process fake adb used by ``--simulate`` and the test-suite.

Only :meth:`AdbService._execute` is overridden, so all argument building,
logging, error classification and timeouts go through the real code path.
"""

from __future__ import annotations

import random
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from devreader.core.adb_service import AdbService, DeviceState
from devreader.core.errors import CancelledError, TimeoutError_
from devreader.core.logging_service import LogService

SAMPLE_WIRELESS = """\
# /etc/config/wireless - simulated device
config qcawifi 'qcawifi'
\toption rf_freq1_KHz '26000000'
\toption rf_freq2_KHz '26000000'
\toption enable_rfsim '1'	# keep me

config wifi-device 'wifi0'
\toption type 'qcawifi'
\toption channel '36'
\toption htmode 'HE80'
\toption country 'US'
\t# option disabled '1'

config wifi-iface
\toption device 'wifi0'
\toption mode 'ap'
\toption ssid "Rozaline Lab"
\tlist maclist '00:11:22:33:44:55'
"""


@dataclass
class SimDevice:
    serial: str
    state: DeviceState = DeviceState.CONNECTED
    rooted: bool = False
    files: dict[str, bytes] = field(default_factory=dict)
    booting_until: float = 0.0
    # Paths under these prefixes fail with "Read-only file system" until
    # "mount -o rw,remount /" runs (verity_disabled=False: remount never helps).
    read_only: tuple[str, ...] = ()
    verity_disabled: bool = True


class SimulatedAdbService(AdbService):
    def __init__(self, log: LogService, serials: dict[str, DeviceState] | None = None,
                 latency: float = 0.15) -> None:
        super().__init__(log)
        self.latency = latency
        self._sim_lock = threading.Lock()
        self.devices_sim: dict[str, SimDevice] = {}
        for serial, state in (serials or {}).items():
            self.add_device(serial, state)

    def add_device(self, serial: str, state: DeviceState = DeviceState.CONNECTED) -> SimDevice:
        dev = SimDevice(serial, state, files={"/etc/config/wireless": SAMPLE_WIRELESS.encode()})
        self.devices_sim[serial] = dev
        return dev

    @property
    def adb_path(self) -> str:  # type: ignore[override]
        return "adb"

    def start_server(self, timeout: float = 30.0) -> None:
        pass

    # ------------------------------------------------------------------
    def _execute(self, full, timeout, cancel, display, serial):
        args = list(full[1:])
        if args[:1] == ["-s"]:
            serial, args = args[1], args[2:]
        self._sleep(self.latency, cancel, display)

        if args == ["devices"]:
            return 0, self._devices_text().encode(), b""
        if args == ["version"]:
            return 0, b"Android Debug Bridge version 1.0.41 (simulated)\n", b""

        dev = self.devices_sim.get(serial or "")
        if args[:1] == ["wait-for-device"]:
            args = args[1:]
            dev = self._wait_for(serial, timeout, cancel, display)
        if dev is None:
            return 1, b"", f"error: device '{serial}' not found\n".encode()
        if dev.state == DeviceState.UNAUTHORIZED:
            return 1, b"", b"error: device unauthorized.\n"
        if dev.state == DeviceState.OFFLINE:
            return 1, b"", b"error: device offline\n"
        return self._dispatch(dev, args, cancel, display)

    def _dispatch(self, dev: SimDevice, args: list[str], cancel, display):
        cmd = args[0] if args else ""
        if cmd == "root":
            dev.rooted = True
            return 0, b"restarting adbd as root\n", b""
        if cmd == "disable-verity":
            return 0, b"Verity disabled on /\nNow reboot your device for settings to take effect\n", b""
        if cmd == "reboot":
            dev.rooted = False
            dev.booting_until = time.monotonic() + 3.0
            return 0, b"", b""
        if cmd == "remount":
            if not dev.rooted:
                return 1, b"", b"Not running as root. Try \"adb root\" first.\n"
            return 0, b"remount succeeded\n", b""
        if cmd in ("shell", "exec-out"):
            return self._shell(dev, " ".join(args[1:]))
        if cmd == "push":
            local, remote = args[1], args[2]
            dev.files[remote] = Path(local).read_bytes()
            return 0, f"{local}: 1 file pushed, 0 skipped.\n".encode(), b""
        if cmd == "pull":
            remote, local = args[1], args[2]
            if remote not in dev.files:
                return 1, b"", f"adb: error: failed to stat remote object '{remote}': No such file or directory\n".encode()
            Path(local).write_bytes(dev.files[remote])
            return 0, b"1 file pulled.\n", b""
        return 1, b"", f"adb: unknown command {cmd}\n".encode()

    def _shell(self, dev: SimDevice, command: str):
        if command == "mount -o rw,remount /":
            if not dev.rooted:
                return 1, b"", b"mount: '/' not in /proc/mounts\n"
            if dev.verity_disabled:
                dev.read_only = ()
            return 0, b"", b""
        if command in ("setenforce 0", "sync"):
            return 0, b"", b""
        m = re.fullmatch(r"cat (\S+)", command)
        if m:
            path = m.group(1)
            if path not in dev.files:
                return 1, f"cat: {path}: No such file or directory\n".encode(), b""
            return 0, dev.files[path], b""
        m = re.fullmatch(r"cat '([^']+)' > '([^']+)'; rc=\$\?; sync; rm -f '[^']+'; echo (\S+)\$rc", command)
        if m:
            src, dst, marker = m.groups()
            if any(dst.startswith(ro) for ro in dev.read_only):
                dev.files.pop(src, None)
                return 0, f"/bin/sh: can't create {dst}: Read-only file system\n{marker}1\n".encode(), b""
            dev.files[dst] = dev.files.pop(src)
            return 0, f"{marker}0\n".encode(), b""
        return 0, b"", b""

    # ------------------------------------------------------------------
    def _devices_text(self) -> str:
        lines = ["List of devices attached"]
        now = time.monotonic()
        for dev in self.devices_sim.values():
            if dev.state == DeviceState.DISCONNECTED or dev.booting_until > now:
                continue
            lines.append(f"{dev.serial}\t{dev.state.value}")
        return "\n".join(lines) + "\n\n"

    def _wait_for(self, serial, timeout, cancel, display) -> SimDevice | None:
        deadline = time.monotonic() + timeout
        while True:
            dev = self.devices_sim.get(serial or "")
            if dev and dev.state != DeviceState.DISCONNECTED and dev.booting_until <= time.monotonic():
                return dev
            if time.monotonic() >= deadline:
                raise TimeoutError_(f"Command timed out after {timeout:.0f}s", command=display, serial=serial)
            self._sleep(0.2, cancel, display)

    @staticmethod
    def _sleep(seconds: float, cancel, display) -> None:
        end = time.monotonic() + seconds * random.uniform(0.6, 1.4)
        while time.monotonic() < end:
            if cancel is not None and cancel.is_set():
                raise CancelledError("Operation cancelled", command=display)
            time.sleep(0.05)
