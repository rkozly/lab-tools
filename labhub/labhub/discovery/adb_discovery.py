"""
ADB discovery: polls `adb devices -l` and feeds the registry.
Also probes backbones (TCP port for ssh, always-up for local).
"""

from __future__ import annotations

import os
import socket
import subprocess
import threading

from ..core.models import AdbDevice, Status
from ..transports import base as tr


def parse_devices(text: str) -> list[AdbDevice]:
    """Parse `adb devices -l`:
        1a2b3c4d   device usb:1-4 product:ipq model:RDP433 device:x transport_id:3
    """
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("List of devices", "*", "adb server", "daemon")):
            continue
        parts = line.split()
        if len(parts) < 2 or parts[0].endswith(":"):
            continue  # messages like "adb: warning: ..."
        info = {}
        for p in parts[2:]:
            if ":" in p:
                k, v = p.split(":", 1)
                info[k] = v
        out.append(AdbDevice(parts[0], parts[1], info))
    return out


class Discovery:
    def __init__(self, registry, bus, interval_s: float = 2.0) -> None:
        self.registry = registry
        self.bus = bus
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_ok: bool | None = None
        self._last_msg = ""
        self.adb_path: str | None = None
        self.last_output = ""

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="discovery", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def refresh(self) -> None:
        self._wake.set()

    def _status(self, ok: bool, msg: str) -> None:
        from ..core.events import AdbStatus
        if (ok, msg) != (self._last_ok, self._last_msg):
            self._last_ok, self._last_msg = ok, msg
            self.bus.publish(AdbStatus(ok, msg))

    def poll_once(self) -> None:
        adb = tr.find_adb()
        self.adb_path = adb
        if not adb:
            self.last_output = "adb.exe was not found (PATH, registry PATH, usual folders)."
            self._status(False, "adb not found – click to set its path")
            self.registry.set_adb_devices([])
        else:
            try:
                res = subprocess.run([adb, "devices", "-l"], capture_output=True, text=True, timeout=20,
                                     stdin=subprocess.DEVNULL, creationflags=tr.CREATE_NO_WINDOW,
                                     cwd=os.path.expanduser("~"))
                self.last_output = f"{adb} devices -l  (exit {res.returncode})\n{res.stdout}{res.stderr}"
                devices = parse_devices(res.stdout)
                self.registry.set_adb_devices(devices)
                if res.returncode != 0 and not devices:
                    err = (res.stderr or res.stdout).strip().splitlines()
                    self._status(False, f"adb error: {err[-1] if err else res.returncode}")
                else:
                    self._status(True, f"adb OK · {len(devices)} device(s)")
            except subprocess.TimeoutExpired:
                self.last_output = f"{adb} devices -l  timed out after 20 s"
                self._status(False, "adb timed out (another adb server version running?)")
            except OSError as e:
                self.last_output = f"{adb}: {e}"
                self._status(False, f"adb error: {e}")
        self._probe_backbones()

    def _probe_backbones(self) -> None:
        for bb in list(self.registry.lab.backbones.values()):
            c = bb.connect
            if c.type == "local":
                status = Status.CONNECTED
            elif c.type == "ssh" and c.params.get("host"):
                try:
                    with socket.create_connection((str(c.params["host"]), int(c.params.get("port", 22))), timeout=1.5):
                        status = Status.CONNECTED
                except OSError:
                    status = Status.DISCONNECTED
            elif c.type == "adb" and c.params.get("serial"):
                state = next((d.state for d in self.registry._adb.values() if d.serial == c.params["serial"]), None)
                status = Status.from_adb(state) if state else Status.DISCONNECTED
            else:
                continue
            self.registry.set_backbone_status(bb.id, status)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as e:  # never let discovery die
                self._status(False, f"discovery error: {e}")
            self._wake.wait(self.interval_s)
            self._wake.clear()
