"""
Association helpers (Traffic tab → Associate): what the AP / CPEs report over
a second adb connection, so it works while their Console shells are busy.

  beacons   rx_legacy_ofdm_rate[3] of `wifistats wifi0 10` on a CPE: beacons
            heard (climbs while the CPE scans, ~150 when it associates), and
            `iw ath0 link` (associated = "Connected to", as in Graphs)
  mlds      the AP's MLD interfaces (mld0, mld1) are UP after its QCMAP bridge
  ap beacons the AP's beacons sent, per radio: the TX "Legacy OFDM Rates" counters of
            `wifistats wifi0 9` (phy0) and `wifistats wifi1 9` (phy1); rising = beaconing
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from dataclasses import dataclass

from .. import parsers
from ..transports import base as tr
from .events import Event, EventBus

AP_MLDS = ("mld0", "mld1")
BEACONS_CMD = "wifistats wifi0 10 | grep -E '^rx_legacy_ofdm_rate'; echo '##link'; iw ath0 link"
MLDS_CMD = "; ".join(f"echo '##{m}'; ifconfig {m} 2>&1" for m in AP_MLDS)
AP_BCN_CMD = ("echo '##phy0'; wifistats wifi0 9 | grep -E 'Legacy OFDM Rates'; "
              "echo '##phy1'; wifistats wifi1 9 | grep -E 'Legacy OFDM Rates'")
BEACONS_NEEDED = 10          # beacons a CPE must hear before the next CPE starts

# channel -> the CPE quick command that scans it (A / B / C: Console buttons, their text
# can be edited there; the airtel ones: quick.TRAFFIC_SCANS, Traffic tab only)
CHANNELS = {"A": "cpe.scan_a", "B": "cpe.scan_b", "C": "cpe.scan_c",
            "26000_airtel": "cpe.scan_26000_airtel", "26330_airtel": "cpe.scan_26330_airtel"}
LONG_CHANNELS = {"A": "cpe.long_a", "B": "cpe.long_b", "C": "cpe.long_c",
                 "26000_airtel": "cpe.long_26000_airtel", "26330_airtel": "cpe.long_26330_airtel"}
AP_SCAN = {"short": "ap.scan", "long": "ap.scan_long"}


def cpe_scan_key(channel: str, scan: str) -> str:
    return (LONG_CHANNELS if scan == "long" else CHANNELS)[channel]


@dataclass
class BeaconCount(Event):
    node_id: str
    value: int | None
    error: str = ""
    associated: bool | None = None   # iw ath0 link; None = could not tell


@dataclass
class ApBeacons(Event):
    node_id: str
    phy0: int | None             # beacons (legacy OFDM frames) sent on wifi0, since boot
    phy1: int | None
    error: str = ""


@dataclass
class MldState(Event):
    node_id: str
    up: bool
    detail: str                  # "mld0 UP, mld1 down" or why it could not be read


def parse_mlds(out: str) -> tuple[bool, str]:
    """Output of MLDS_CMD -> (all UP, 'mld0 UP · mld1 missing')."""
    parts = re.split(r"^##(\S+)\s*$", out, flags=re.M)
    state = {}
    for name, body in zip(parts[1::2], parts[2::2]):
        if re.search(r"not found|no such device|error fetching|can't", body, re.I) or not body.strip():
            state[name] = "missing"
        else:
            state[name] = "UP" if re.search(r"\bUP\b", body) else "down"
    detail = " · ".join(f"{m} {state.get(m, 'missing')}" for m in AP_MLDS)
    return all(state.get(m) == "UP" for m in AP_MLDS), detail


class DeviceReader:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._busy: set[str] = set()

    def _start(self, key: str, fn, *args) -> None:
        if key in self._busy:
            return
        self._busy.add(key)

        def work():
            try:
                fn(*args)
            finally:
                self._busy.discard(key)
        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _adb(serial: str, command: str) -> tuple[int, str]:
        tr.ensure_root(serial)   # wifistats needs root
        argv = tr.exec_argv(tr.ConnectSpec("adb", {"serial": serial}), command)
        res = subprocess.run(argv, capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL,
                             creationflags=tr.CREATE_NO_WINDOW, cwd=os.path.expanduser("~"),
                             encoding="utf-8", errors="replace")
        return res.returncode, (res.stdout + res.stderr).strip()

    def read_beacons(self, node_id: str, serial: str) -> None:
        self._start(f"bcn:{node_id}", self._beacons, node_id, serial)

    def _beacons(self, node_id: str, serial: str) -> None:
        try:
            code, out = self._adb(serial, BEACONS_CMD)
        except (OSError, subprocess.TimeoutExpired, tr.TransportError) as e:
            self.bus.publish(BeaconCount(node_id, None, str(e)))
            return
        stats, _, link = out.partition("##link")
        value = parsers.parse_rx(stats.splitlines()).get("beacons_rx")
        assoc = parsers.parse_iw_link(link.splitlines()).get("associated") if link.strip() else None
        err = "" if value is not None else (stats.strip().splitlines()[-1] if stats.strip()
                                            else f"no rx_legacy_ofdm_rate (exit {code})")
        self.bus.publish(BeaconCount(node_id, value, err, assoc))

    def read_ap_beacons(self, node_id: str, serial: str) -> None:
        self._start(f"apbcn:{node_id}", self._ap_beacons, node_id, serial)

    def _ap_beacons(self, node_id: str, serial: str) -> None:
        try:
            code, out = self._adb(serial, AP_BCN_CMD)
        except (OSError, subprocess.TimeoutExpired, tr.TransportError) as e:
            self.bus.publish(ApBeacons(node_id, None, None, str(e)))
            return
        counts = {}
        for name, _arg, lines in parsers.sections(out):
            counts[name] = parsers.parse_legacy(lines).get("legacy_total")
        err = "" if any(v is not None for v in counts.values()) else (out.splitlines()[-1] if out else f"exit {code}")
        self.bus.publish(ApBeacons(node_id, counts.get("phy0"), counts.get("phy1"), err))

    def check_mlds(self, node_id: str, serial: str) -> None:
        self._start(f"mld:{node_id}", self._mlds, node_id, serial)

    def _mlds(self, node_id: str, serial: str) -> None:
        try:
            _code, out = self._adb(serial, MLDS_CMD)
        except (OSError, subprocess.TimeoutExpired, tr.TransportError) as e:
            self.bus.publish(MldState(node_id, False, f"cannot read: {e}"))
            return
        up, detail = parse_mlds(out)
        self.bus.publish(MldState(node_id, up, detail))
