"""
Parsers: turn command output into structured data.

Parsers are tolerant: unknown lines are ignored and every field is
optional, so a firmware change gives missing values, not a crash.
Sections are separated by '##<name> <arg>' marker lines that the monitor
presets echo (see presets/builtin.yaml).
"""

from __future__ import annotations

import re
from typing import Callable

_NUM = re.compile(r"-?\d+(?:\.\d+)?")
_KV = re.compile(r"^\s*([A-Za-z_][\w\[\]\. ]*?)\s*[:=]\s*(.*)$")


def sections(text: str) -> list[tuple[str, str, list[str]]]:
    """Split output on '##name arg' markers -> [(name, arg, lines)]."""
    out: list[tuple[str, str, list[str]]] = [("", "", [])]
    for line in text.splitlines():
        if line.startswith("##"):
            head = line[2:].split(None, 1)
            out.append((head[0] if head else "", head[1].strip() if len(head) > 1 else "", []))
        else:
            out[-1][2].append(line)
    return [s for s in out if s[0] or s[2]]


def numbers(value: str) -> list[float]:
    return [float(x) for x in _NUM.findall(value)]


def counters(value: str) -> list[int]:
    """'0:12, 1:0, 2:345' or '12 0 345' -> per-index counts."""
    pairs = re.findall(r"(\d+)\s*:\s*(\d+)", value)
    if pairs:
        result: dict[int, int] = {}
        for k, v in pairs:
            result[int(k)] = int(v)
        return [result.get(i, 0) for i in range(max(result) + 1)]
    return [int(float(x)) for x in _NUM.findall(value)]


def dominant_index(value: str) -> int | None:
    """Index with the highest counter (e.g. the MCS used most)."""
    c = counters(value)
    if not c or max(c) <= 0:
        return None
    return max(range(len(c)), key=lambda i: c[i])


def key_values(lines: list[str]) -> dict[str, str]:
    kv = {}
    for line in lines:
        m = _KV.match(line)
        if m:
            kv.setdefault(m.group(1).strip().lower(), m.group(2).strip())
    return kv


def _first_number(value: str | None) -> float | None:
    if not value:
        return None
    n = numbers(value)
    return n[0] if n else None


def _valid_rssi(values: list[float]) -> float | None:
    vals = [v for v in values if -120 < v < 0]
    return max(vals) if vals else None


BW_BY_INDEX = {0: 20, 1: 40, 2: 80, 3: 160, 4: 320, 5: 640}  # rx_bw counters are indexed
BEACON_RATE_INDEX = 3   # rx_legacy_ofdm_rate index the beacons are received at


def parse_radio(lines: list[str]) -> dict:
    """wifistats <radio> 9 | grep tx_mcs  ->  tx_mcs (most used) + tx_mcs_hist"""
    kv = key_values(lines)
    out: dict = {}
    for k, v in kv.items():
        if k.startswith("tx_mcs"):
            idx = dominant_index(v)
            if idx is not None:
                out.setdefault("tx_mcs", idx)
            hist = counters(v)
            if hist and "tx_mcs_hist" not in out:
                out["tx_mcs_hist"] = hist
    return out


def parse_legacy(lines: list[str]) -> dict:
    """wifistats <radio> 9 | grep 'Legacy OFDM Rates'.

    Beacons go out at a legacy OFDM rate, so the growth of these TX counters
    shows beacons (plus other management frames) being sent. Accepts
    '6 Mbps: 120, 9 Mbps: 0', '0:120, 1:0' or plain '120 0 0'.
    """
    counts: list[int] = []
    uptime = None
    for line in lines:
        um = re.match(r"^\s*(\d+\.\d+)\s+\d+(?:\.\d+)?\s*$", line)   # /proc/uptime
        if um and uptime is None:
            uptime = float(um.group(1))
            continue
        if "legacy" not in line.lower():
            continue
        parts = re.split(r"[:=]", line, maxsplit=1)
        val = parts[1] if len(parts) > 1 else ""
        pairs = re.findall(r"(\d+(?:\.\d+)?)\s*(?:mbps)?\s*[:=]\s*(\d+)", val, re.I)
        counts += [int(v) for _, v in pairs] if pairs else [int(float(x)) for x in _NUM.findall(val)]
    if not counts:
        return {}
    out = {"legacy_total": sum(counts), "legacy_counts": counts}
    if uptime is not None:
        out["uptime_s"] = uptime  # device clock, so time deltas do not include adb latency
    return out


def parse_rx(lines: list[str]) -> dict:
    """wifistats <radio> 10 | grep rx_bw / rx_mcs / rssi_in_dbm / pilot_evm ..."""
    out: dict = {}
    for line in lines:
        m = _KV.match(line)
        if not m:
            continue
        key, value = m.group(1).strip().lower(), m.group(2).strip()
        if key.startswith("rx_mcs") and "rx_mcs" not in out:
            idx = dominant_index(value)
            if idx is not None:
                out["rx_mcs"] = idx
            hist = counters(value)
            if hist:
                out["rx_mcs_hist"] = hist
        elif key.startswith("rx_legacy_ofdm_rate") and "rx_legacy_hist" not in out:
            # beacons come in at legacy OFDM index 3: a CPE's counter climbs (~150) until it associates
            hist = counters(value)
            if hist:
                out["rx_legacy_hist"] = hist
                out["beacons_rx"] = hist[BEACON_RATE_INDEX] if len(hist) > BEACON_RATE_INDEX else 0
        elif key.startswith("rx_bw") and "rx_bw_mhz" not in out:
            idx = dominant_index(value)
            if idx is not None:
                out["rx_bw_mhz"] = BW_BY_INDEX.get(idx, idx)
        elif key.startswith("rssi_in_dbm") and "rssi_dbm" not in out:
            r = _valid_rssi(numbers(value))
            if r is not None:
                out["rssi_dbm"] = r
        elif key.startswith("pilot_evm_db[0]") and "evm0_db" not in out:
            v = [x for x in numbers(value.split(":", 1)[-1] if "]" in key else value) if x != 0]
            if v:
                out["evm0_db"] = sum(v) / len(v)
        elif key.startswith("pilot_evm_db[1]") and "evm1_db" not in out:
            v = [x for x in numbers(value) if x != 0]
            if v:
                out["evm1_db"] = sum(v) / len(v)
    return out


def parse_iw_link(lines: list[str]) -> dict:
    """iw <dev> link"""
    text = "\n".join(lines)
    out: dict = {}
    if "Not connected" in text:
        out["associated"] = False
        return out
    m = re.search(r"Connected to ([0-9a-fA-F:]{17})", text)
    if m:
        out["associated"] = True
        out["bssid"] = m.group(1).lower()
    for key, pat in (("ssid", r"SSID:\s*(.+)"), ("freq_mhz", r"freq:\s*([\d.]+)"),
                     ("signal_dbm", r"signal:\s*(-?[\d.]+)"),
                     ("tx_bitrate", r"tx bitrate:\s*(.+)"), ("rx_bitrate", r"rx bitrate:\s*(.+)")):
        mm = re.search(pat, text)
        if mm:
            v = mm.group(1).strip()
            out[key] = float(v) if key in ("freq_mhz", "signal_dbm") else v
    for key in ("tx_bitrate", "rx_bitrate"):
        mm = re.match(r"([\d.]+)\s*([KMG]?)Bit/s", str(out.get(key, "")), re.I)
        if mm:
            out[key + "_mbps"] = float(mm.group(1)) * {"": 1e-6, "K": 1e-3, "M": 1.0, "G": 1e3}[mm.group(2).upper()]
    return out


def parse_ifconfig(lines: list[str]) -> dict:
    text = "\n".join(lines)
    out: dict = {}
    m = re.search(r"(?:HWaddr|ether)\s+([0-9a-fA-F:]{17})", text)
    if m:
        out["mac"] = m.group(1).lower()
    m = re.search(r"RX bytes:\s*(\d+).*?TX bytes:\s*(\d+)", text, re.S)
    if m:
        out["rx_bytes"], out["tx_bytes"] = int(m.group(1)), int(m.group(2))
    else:
        rx = re.search(r"RX packets.*?bytes\s+(\d+)", text)
        tx = re.search(r"TX packets.*?bytes\s+(\d+)", text)
        if rx:
            out["rx_bytes"] = int(rx.group(1))
        if tx:
            out["tx_bytes"] = int(tx.group(1))
    out["up"] = bool(re.search(r"\bUP\b", text))
    return out


def parse_wlanconfig_list(lines: list[str]) -> list[dict]:
    """wlanconfig <vap> list -> one dict per station (ADDR, RSSI, ...)."""
    stations = []
    header: list[str] | None = None
    for line in lines:
        cols = line.split()
        if not cols:
            continue
        if cols[0].upper() == "ADDR":
            header = [c.upper() for c in cols]
            continue
        if header and re.fullmatch(r"[0-9a-fA-F:]{17}", cols[0]):
            st = {"mac": cols[0].lower()}
            for i, name in enumerate(header[1:], 1):
                if i < len(cols):
                    st[name.lower()] = cols[i]
            stations.append(st)
    return stations


# --- monitor parsers --------------------------------------------------------

def parse_cpe(text: str) -> dict:
    """Output of the cpe-stats preset -> {metrics..., link: {...}}"""
    m: dict = {}
    for name, _arg, lines in sections(text):
        if name == "radio":
            m.update(parse_radio(lines))
        elif name == "rx":
            m.update(parse_rx(lines))
        elif name == "ifconfig":
            m["ifconfig"] = parse_ifconfig(lines)
        elif name == "link":
            m["link"] = parse_iw_link(lines)
    link = m.get("link") or {}
    if "rssi_dbm" not in m and "signal_dbm" in link:
        m["rssi_dbm"] = link["signal_dbm"]
    return m


def parse_ap(text: str) -> dict:
    """Output of the ap-stats preset -> {radios: {wifi0: {...}}, stations: {mld0: [...]}}"""
    m: dict = {"radios": {}, "stations": {}}
    for name, arg, lines in sections(text):
        if name == "radio":
            m["radios"].setdefault(arg, {}).update(parse_radio(lines))
        elif name == "legacy":
            m["radios"].setdefault(arg, {}).update(parse_legacy(lines))
        elif name == "rx":
            m["radios"].setdefault(arg, {}).update(parse_rx(lines))
        elif name == "mld":
            m["stations"][arg] = parse_wlanconfig_list(lines)
    rssis = [r["rssi_dbm"] for r in m["radios"].values() if "rssi_dbm" in r]
    if rssis:
        m["rssi_dbm"] = max(rssis)
    return m


# --- scan results -----------------------------------------------------------

def parse_iw_scan(text: str) -> list[dict]:
    """`iw dev <dev> scan dump` -> [{bssid, ssid, freq_mhz, rssi_dbm, associated}]"""
    results = []
    cur: dict | None = None
    for line in text.splitlines():
        m = re.match(r"^BSS ([0-9a-fA-F:]{17})", line)
        if m:
            cur = {"bssid": m.group(1).lower(), "associated": "associated" in line}
            results.append(cur)
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("freq:"):
            n = _first_number(s[5:])
            if n is not None:
                cur["freq_mhz"] = n
        elif s.startswith("signal:"):
            n = _first_number(s[7:])
            if n is not None:
                cur["rssi_dbm"] = n
        elif s.startswith("SSID:"):
            cur["ssid"] = s[5:].strip()
        elif s.startswith("DS Parameter set: channel"):
            n = _first_number(s.split("channel", 1)[1])
            if n is not None:
                cur["channel"] = int(n)
    return results


PARSERS: dict[str, Callable[[str], object]] = {
    "wifistats.cpe": parse_cpe,
    "wifistats.ap": parse_ap,
    "iw.scan": parse_iw_scan,
}


def register(name: str, fn: Callable[[str], object]) -> None:
    PARSERS[name] = fn


# --- iperf [SUM] lines (traffic run in parallel streams, in the BB shells) ---

_IPERF_RATE = re.compile(r"([\d.]+)\s*([KMGT]?)bits/sec", re.I)
_IPERF_INTERVAL = re.compile(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*sec")
_IPERF_LOSS = re.compile(r"(\d+)\s*/\s*(\d+)\s*\(\s*([\d.e+-]+)\s*%\s*\)")
_UNIT = {"": 1e-6, "K": 1e-3, "M": 1.0, "G": 1e3, "T": 1e6}


def parse_iperf_sum(line: str) -> dict | None:
    """One iperf (2 or 3) '[SUM]' line -> {mbps, start_s, end_s, per_pct?, lost?, total?}.

    iperf2:  [SUM]  0.0- 1.0 sec   120 MBytes  1.01 Gbits/sec   0.012 ms  10/90000 (0.011%)
    iperf3:  [SUM]   1.00-2.00   sec  25.0 MBytes   210 Mbits/sec  0.031 ms  3/18000 (0.017%)
    The final summary over the whole run (interval longer than ~1.5 s) is
    marked final=True so a graph can skip it.
    """
    if "[SUM" not in line.upper():
        return None
    rate = _IPERF_RATE.search(line)
    if not rate:
        return None
    out: dict = {"mbps": float(rate.group(1)) * _UNIT[rate.group(2).upper()]}
    iv = _IPERF_INTERVAL.search(line)
    if iv:
        out["start_s"], out["end_s"] = float(iv.group(1)), float(iv.group(2))
        out["final"] = out["end_s"] - out["start_s"] > 1.5
    loss = _IPERF_LOSS.search(line[rate.end():])
    if loss:
        out["lost"], out["total"] = int(loss.group(1)), int(loss.group(2))
        try:
            out["per_pct"] = float(loss.group(3))
        except ValueError:
            pass
    out["role"] = "receiver" if "receiver" in line.lower() else ("sender" if "sender" in line.lower() else "")
    return out
