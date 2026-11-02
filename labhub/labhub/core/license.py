"""
AP license (QWES): push the .pfm license file with adb, and read how many
stations the AP allows (max_sta) from hostapd's QWES log lines.

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
WATCH_CMD = 'tail -f -n +1 /tmp/hostapd.txt | grep -E "QWES"'
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
class LicensePushed(Event):
    node_id: str
    name: str
    ok: bool
    text: str


class LicenseService:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus

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
