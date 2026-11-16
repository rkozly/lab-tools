"""
AP license (QWES): push the .pfm license file with adb, and read how many
stations the AP allows (max_sta) from hostapd's QWES log lines
(the newest `max_sta=` in /tmp/hostapd.txt, read every few seconds).

The file and the folder on the AP are set in Mappings (lab file:
features.license.file / .dest); the defaults below are the lab's 64-station license.
"""

from __future__ import annotations

import ntpath
import os
import re
import subprocess
import threading
from dataclasses import dataclass

from ..transports import base as tr
from .events import Event, EventBus

DEFAULT_FILE = (r"\\bulk\si\SystemIntegration\LBR\license\af555f86-e36d-420e-92c7-c54bb84cb13d_2026-07-08_"
                r"platform_RSA_license_123948124704882474564238635192177888094 2.pfm")
DEFAULT_DEST = "/persist/data/pfm/licenses"
LOG_FILE = "/tmp/hostapd.txt"
# the newest QWES line with max_sta; polled (a one-shot command: no pty / grep buffering to go wrong)
READ_CMD = (f"if [ -f {LOG_FILE} ]; then grep -E 'QWES' {LOG_FILE} | grep 'max_sta=' | tail -n 1; "
            f"else echo 'no {LOG_FILE} on the AP'; fi")
_MAX_STA = re.compile(r"\bmax_sta=(\d+)")
_PUSHED = re.compile(r"\b1 file pushed")


def settings(features: dict | None) -> tuple[str, str]:
    """(local license file, folder on the AP) from the lab's features."""
    lic = (features or {}).get("license") or {}
    if not isinstance(lic, dict):
        lic = {}
    return str(lic.get("file") or DEFAULT_FILE), str(lic.get("dest") or DEFAULT_DEST).rstrip("/")


def last_max_sta(text: str) -> int | None:
    """The newest max_sta=<n> in QWES output, or None."""
    found = _MAX_STA.findall(text)
    return int(found[-1]) if found else None


@dataclass
class MaxSta(Event):
    node_id: str
    value: int | None
    detail: str          # the QWES line, or why there is no value


@dataclass
class LicensePushed(Event):
    node_id: str
    name: str
    ok: bool
    text: str


class LicenseService:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._reading: set[str] = set()

    def read_max_sta(self, node_id: str, serial: str) -> None:
        if node_id in self._reading:
            return
        self._reading.add(node_id)
        threading.Thread(target=self._read, args=(node_id, serial), daemon=True).start()

    def _read(self, node_id: str, serial: str) -> None:
        try:
            tr.ensure_root(serial)   # /tmp/hostapd.txt is root's
            argv = tr.exec_argv(tr.ConnectSpec("adb", {"serial": serial}), READ_CMD)
            res = subprocess.run(argv, capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL,
                                 creationflags=tr.CREATE_NO_WINDOW, cwd=os.path.expanduser("~"),
                                 encoding="utf-8", errors="replace")
            out = (res.stdout + res.stderr).strip()
            value = last_max_sta(out)
            if value is not None:
                detail = out.splitlines()[-1].strip()
            elif res.returncode != 0:
                detail = out or f"adb exit {res.returncode}"
            else:
                detail = out or f"no QWES max_sta line in {LOG_FILE} yet"
            self.bus.publish(MaxSta(node_id, value, detail))
        except (OSError, subprocess.TimeoutExpired, tr.TransportError) as e:
            self.bus.publish(MaxSta(node_id, None, f"cannot read: {e}"))
        finally:
            self._reading.discard(node_id)

    def push(self, node_id: str, name: str, serial: str, local: str, dest_dir: str) -> None:
        threading.Thread(target=self._push, args=(node_id, name, serial, local, dest_dir), daemon=True).start()

    def _push(self, node_id: str, name: str, serial: str, local: str, dest_dir: str) -> None:
        if not os.path.isfile(local):
            self.bus.publish(LicensePushed(node_id, name, False, f"license file not found: {local}"))
            return
        remote = f"{dest_dir}/{ntpath.basename(local)}"
        try:
            argv = tr.exec_argv(tr.ConnectSpec("adb", {"serial": serial}), "true")[:3] + ["push", local, remote]
            res = subprocess.run(argv, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                                 creationflags=tr.CREATE_NO_WINDOW, cwd=os.path.expanduser("~"),
                                 encoding="utf-8", errors="replace")
        except (OSError, subprocess.TimeoutExpired, tr.TransportError) as e:
            self.bus.publish(LicensePushed(node_id, name, False, str(e)))
            return
        out = (res.stdout + res.stderr).strip()
        ok = res.returncode == 0 and bool(_PUSHED.search(out))
        self.bus.publish(LicensePushed(node_id, name, ok, out or f"adb push exit {res.returncode}"))
