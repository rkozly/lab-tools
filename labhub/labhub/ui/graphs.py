"""
Graphs tab: moving charts of the wifistats values, one card per AP / CPE
that is open in the Console (same devices as the Stats tab).

The values come from a poller per device (the ap-stats / cpe-stats snapshot
commands, once a second) - the same wifistats calls the Stats tab's watch runs,
parsed instead of shown as text. Capture (per card, or Capture all) saves the
card's charts as a PNG with a summary table under them: min / avg / max / last
of every line, and per MCS bar chart the packets and the MCS used most. "Whole run" (default): the charts span from the
first sample to now and get denser as time passes, the MCS bars count since the
start; the 30 s .. 15 min choices scroll to the left instead.

  AP   TX / RX MCS (bars: packets per index in the last 60 s), per radio
       RX RSSI, pilot EVM 0/1, time between beacons, beacons heard;
       connected stations (a number)
  CPE  TX / RX MCS (bars), signal (RSSI + iw link signal), TX / RX bitrate
       (iw link), pilot EVM, RX bandwidth, beacons heard (rx_legacy_ofdm_rate[3]:
       climbs to ~150 before the CPE associates)
  Both iperf throughput and PER: the [SUM] lines (parallel streams added up)
       that iperf prints in the device's backbone shell; PER = UDP lost / total.
"""

from __future__ import annotations

import time
from collections import deque

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget)

from .common import c as tc

COLORS = ["#4aa3ff", "#3ccf7a", "#f0b43c", "#c678dd", "#ff6b7a", "#56d4dd"]
WINDOWS = ((None, "whole run"), (30, "last 30 s"), (60, "last 1 min"), (120, "last 2 min"), (300, "last 5 min"),
           (900, "last 15 min"))
MCS_WINDOW_S = 60
MAX_POINTS = 200_000          # per line: hours at one sample a second


def _elapsed(sec: float) -> str:
    sec = int(sec)
    if sec < 120:
        return f"{sec}s"
    h, rem = divmod(sec, 3600)
    m, s_ = divmod(rem, 60)
    return f"{h}:{m:02d}:{s_:02d}" if h else f"{m}:{s_:02d}"


def _font(p: QPainter, size: float = 8) -> None:
    f = p.font()
    f.setPointSizeF(size)
    p.setFont(f)


class Chart(QWidget):
    """Lines over time, x = seconds before now, redrawn continuously so it moves."""

    def __init__(self, title: str, unit: str = "", lo: float | None = None, hi: float | None = None) -> None:
        super().__init__()
        self.title, self.unit, self.lo, self.hi = title, unit, lo, hi
        self.series: dict[str, list[tuple[float, float]]] = {}
        self.window: float | None = None      # None = whole run
        self.hint = "waiting for data…"
        self.setMinimumHeight(150)

    def add(self, name: str, t: float, v) -> None:
        if not isinstance(v, (int, float)):
            return
        pts = self.series.setdefault(name, [])
        pts.append((t, float(v)))
        if len(pts) > MAX_POINTS:
            del pts[:len(pts) - MAX_POINTS]

    def clear(self) -> None:
        self.series.clear()
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        _font(p)
        now = time.monotonic()
        r = QRectF(self.rect()).adjusted(50, 20, -10, -20)
        p.setPen(QColor(tc("#d8dce4")))
        all_v = [v for s in self.series.values() for _t, v in s]
        giga = self.unit == "Mbit/s" and all_v and max(all_v) >= 1000     # show Gbit/s from 1000 Mbit/s on
        unit = "Gbit/s" if giga else self.unit
        div = 1000.0 if giga else 1.0
        p.drawText(QRectF(8, 2, self.width() - 16, 16), Qt.AlignLeft | Qt.AlignVCenter,
                   self.title + (f"  ({unit})" if unit else ""))
        whole = self.window is None
        t_start = min((s[0][0] for s in self.series.values() if s), default=now)
        span = max(now - t_start, 10.0) if whole else self.window
        pts = self.series if whole else {k: [(t, v) for t, v in s if now - t <= span + 2]
                                         for k, s in self.series.items()}
        vals = [v for s in pts.values() for _, v in s]
        x = self.width() - 10
        for i, name in reversed(list(enumerate(sorted(pts)))):
            if not pts[name]:
                continue
            last = pts[name][-1][1]
            shown = f"{last / div:.2f}" if giga else f"{last:.1f}"
            txt = (f"● {name} {shown}" if len(pts) > 1 else shown)
            w = p.fontMetrics().horizontalAdvance(txt) + 12
            p.setPen(QColor(COLORS[i % len(COLORS)]))
            p.drawText(QRectF(x - w, 2, w, 16), Qt.AlignRight | Qt.AlignVCenter, txt)
            x -= w
        p.setPen(QPen(QColor(tc("#2c3039"))))
        p.drawRect(r)
        if not vals:
            p.setPen(QColor(tc("#5b6170")))
            p.drawText(r, Qt.AlignCenter | Qt.TextWordWrap, self.hint)
            return
        lo = self.lo if self.lo is not None else min(vals)
        hi = self.hi if self.hi is not None else max(vals)
        lo, hi = min(lo, min(vals)), max(hi, max(vals))
        if hi - lo < 1e-9:
            lo, hi = lo - 1, hi + 1
        pad = (hi - lo) * 0.08
        if self.lo is None:
            lo -= pad
        if self.hi is None:
            hi += pad

        def X(t: float) -> float:
            return r.right() - (now - t) / span * r.width()

        def Y(v: float) -> float:
            return r.bottom() - (v - lo) / (hi - lo) * r.height()

        for i in range(5):
            v = lo + (hi - lo) * i / 4
            y = Y(v)
            p.setPen(QPen(QColor(tc("#23262e"))))
            p.drawLine(int(r.left()), int(y), int(r.right()), int(y))
            p.setPen(QColor(tc("#8a90a0")))
            p.drawText(QRectF(0, y - 8, 46, 16), Qt.AlignRight | Qt.AlignVCenter,
                       f"{v / div:.2f}" if giga else
                       (f"{v:,.0f}" if abs(hi - lo) >= 20 else (f"{v:.1f}" if abs(hi - lo) >= 1 else f"{v:.3f}")))
        step = next((s for s in (5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200) if s >= span / 6), 14400)
        sec = 0
        while sec <= span + 0.01:
            p.setPen(QColor(tc("#8a90a0")))
            if whole:                          # time since the first sample, left to right
                x = r.left() + sec / span * r.width()
                p.drawText(QRectF(x - 30, r.bottom() + 3, 60, 14), Qt.AlignCenter, _elapsed(sec))
            else:
                x = r.right() - sec / span * r.width()
                p.drawText(QRectF(x - 30, r.bottom() + 3, 60, 14), Qt.AlignCenter, "now" if sec == 0 else f"-{sec}s")
            sec += step
        p.setClipRect(r)
        for i, name in enumerate(sorted(pts)):
            s = pts[name]
            if not s:
                continue
            color = QColor(COLORS[i % len(COLORS)])
            cols: dict[int, list[float]] = {}  # one point (the mean) per pixel column: long runs stay fast
            for t, v in s:
                cols.setdefault(int(X(t)), []).append(v)
            xs = sorted(cols)
            path = QPainterPath(QPointF(xs[0], Y(sum(cols[xs[0]]) / len(cols[xs[0]]))))
            for cx in xs[1:]:
                path.lineTo(QPointF(cx, Y(sum(cols[cx]) / len(cols[cx]))))
            p.setPen(QPen(color, 1.8))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
            p.setBrush(color)
            t, v = s[-1]
            p.drawEllipse(QPointF(X(t), Y(v)), 3, 3)


class McsBars(QWidget):
    """Bars per MCS index: packets in the last MCS_WINDOW_S seconds.

    wifistats tx_mcs / rx_mcs are counters since boot, so the bar of an index is
    its counter now minus its counter MCS_WINDOW_S ago. One group of bars per
    radio (wifi0, wifi1) side by side.
    """

    def __init__(self, title: str) -> None:
        super().__init__()
        self.title = title
        self.hist: dict[str, deque] = {}      # radio -> deque[(t, [counters])]
        self.first: dict[str, tuple[float, list[int]]] = {}   # radio -> first sample (whole run)
        self.window: float | None = None      # None = since the start, else the last MCS_WINDOW_S
        self.setMinimumHeight(170)

    def add(self, name: str, t: float, counters) -> None:
        if not counters:
            return
        d = self.hist.setdefault(name, deque())
        d.append((t, list(counters)))
        self.first.setdefault(name, (t, list(counters)))
        while d and t - d[0][0] > MCS_WINDOW_S + 5:
            d.popleft()

    def clear(self) -> None:
        self.hist.clear()
        self.first.clear()
        self.update()

    def bars(self) -> dict[str, list[int]]:
        """radio -> packets per index over the last window (counter resets handled)."""
        out = {}
        now = time.monotonic()
        for name, d in self.hist.items():
            if not d:
                continue
            base = self.first.get(name, d[0]) if self.window is None else \
                next(((t, c) for t, c in d if now - t <= MCS_WINDOW_S), d[0])
            cur = d[-1][1]
            n = max(len(cur), len(base[1]))
            b = base[1] + [0] * (n - len(base[1]))
            cur = cur + [0] * (n - len(cur))
            delta = [x - y for x, y in zip(cur, b)]
            if any(v < 0 for v in delta):      # counters were reset: count from the reset
                delta = cur
                if self.window is None:
                    self.first[name] = (d[-1][0], [0] * n)
            if len(d) == 1 and base[0] == d[-1][0]:   # one sample only: show nothing yet
                delta = [0] * n
            out[name] = delta
        return out

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        _font(p)
        p.setPen(QColor(tc("#d8dce4")))
        p.drawText(QRectF(8, 2, self.width() - 16, 16), Qt.AlignLeft | Qt.AlignVCenter,
                   f"{self.title}  (packets per index, "
                   + ("since the start)" if self.window is None else f"last {MCS_WINDOW_S} s)"))
        r = QRectF(self.rect()).adjusted(56, 22, -10, -22)
        p.setPen(QPen(QColor(tc("#2c3039"))))
        p.drawRect(r)
        data = self.bars()
        top = max((v for b in data.values() for v in b), default=0)
        if not data or top <= 0:
            p.setPen(QColor(tc("#5b6170")))
            p.drawText(r, Qt.AlignCenter, "waiting for data…" if not any(len(d) > 1 for d in self.hist.values())
                       else ("no packets yet" if self.window is None else "no packets in the last 60 s"))
            return
        n = max(len(b) for b in data.values())
        names = sorted(data)
        slot = r.width() / n
        bw = max(1.5, (slot - 3) / len(names))
        for i in range(5):
            v = top * i / 4
            y = r.bottom() - v / top * r.height()
            p.setPen(QPen(QColor(tc("#23262e"))))
            p.drawLine(int(r.left()), int(y), int(r.right()), int(y))
            p.setPen(QColor(tc("#8a90a0")))
            p.drawText(QRectF(0, y - 8, 52, 16), Qt.AlignRight | Qt.AlignVCenter, _short(v))
        legend_x = self.width() - 10
        for gi, name in reversed(list(enumerate(names))):
            b = data[name]
            total = sum(b)
            best = max(range(len(b)), key=lambda k: b[k]) if total else None
            txt = f"● {name}: {_short(total)} pkts" + (f", mostly MCS {best}" if best is not None else "")
            w = p.fontMetrics().horizontalAdvance(txt) + 14
            p.setPen(QColor(COLORS[gi % len(COLORS)]))
            p.drawText(QRectF(legend_x - w, 2, w, 16), Qt.AlignRight | Qt.AlignVCenter, txt)
            legend_x -= w
        for gi, name in enumerate(names):
            color = QColor(COLORS[gi % len(COLORS)])
            for k, v in enumerate(data[name]):
                if v <= 0:
                    continue
                h = max(1.0, v / top * r.height())
                x = r.left() + k * slot + 1.5 + gi * bw
                p.setPen(Qt.NoPen)
                p.setBrush(color)
                p.drawRect(QRectF(x, r.bottom() - h, bw - 0.5, h))
        p.setPen(QColor(tc("#8a90a0")))
        for k in range(n):
            if n <= 20 or k % 2 == 0:
                p.drawText(QRectF(r.left() + k * slot, r.bottom() + 3, slot, 14), Qt.AlignCenter, str(k))


def _fmt_rate(mbps: float) -> str:
    return f"{mbps / 1000:.2f} Gbit/s" if mbps >= 1000 else f"{mbps:.0f} Mbit/s"


def _short(v: float) -> str:
    v = float(v)
    for div, suf in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
        if abs(v) >= div:
            return f"{v / div:.1f}{suf}"
    return f"{v:.0f}"


class BigNumber(QFrame):
    """A labelled number (connected stations)."""

    def __init__(self, title: str) -> None:
        super().__init__()
        self.setObjectName("Card")
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 6, 12, 8)
        t = QLabel(title)
        t.setObjectName("MetricLabel")
        v.addWidget(t)
        self.value = QLabel("–")
        self.value.setObjectName("Metric")
        self.value.setStyleSheet("font-size: 22pt;")
        v.addWidget(self.value)
        self.detail = QLabel("")
        self.detail.setObjectName("Muted")
        self.detail.setStyleSheet("background: transparent;")
        v.addWidget(self.detail)
        v.addStretch(1)


BCN_RX_TITLE = "Beacons heard – rx_legacy_ofdm_rate[3]"
IPERF_HINT = ("start iperf in this device's BB shell (Console):\nthe [SUM] lines are drawn here")


class DeviceGraphs(QFrame):
    """All charts of one device."""

    def __init__(self, title: str, role: str) -> None:
        super().__init__()
        self.name = title
        self.setObjectName("Card")
        self.role = role
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 8, 12, 10)
        head = QHBoxLayout()
        t = QLabel(title)
        t.setStyleSheet("font-weight: 600; font-size: 11pt; background: transparent;")
        head.addWidget(t)
        self.status = QLabel("starting…")
        self.status.setObjectName("Muted")
        self.status.setStyleSheet("background: transparent;")
        head.addWidget(self.status, 1)
        cap = QPushButton("Capture")
        cap.setToolTip("Save this card's charts as a PNG, with a summary table (min / avg / max / last) under them")
        cap.clicked.connect(self.capture_to_file)
        head.addWidget(cap)
        b = QPushButton("⇩ CSV")
        b.setToolTip("Export the captured values of this device")
        b.clicked.connect(self.export)
        head.addWidget(b)
        v.addLayout(head)
        grid = QGridLayout()
        grid.setSpacing(6)
        self.charts: dict[str, Chart] = {}
        self.mcs: dict[str, McsBars] = {"tx": McsBars("TX MCS"), "rx": McsBars("RX MCS")}
        if role == "ap":
            self.stations = BigNumber("CONNECTED STATIONS")
            specs = [("rssi", "RX RSSI", "dBm", None, None), ("evm0", "Pilot EVM 0", "dB", None, None),
                     ("evm1", "Pilot EVM 1", "dB", None, None), ("bcn", "Time between beacons", "ms", 0, None),
                     ("tput", "Throughput – iperf [SUM]", "Mbit/s", 0, None), ("per", "PER – iperf UDP loss", "%", 0, None),
                     ("bcn_rx", BCN_RX_TITLE, "count", 0, None)]
        else:
            self.stations = None
            specs = [("signal", "Signal", "dBm", None, None), ("bitrate", "Bitrate (iw link)", "Mbit/s", 0, None),
                     ("evm", "Pilot EVM", "dB", None, None), ("bw", "RX bandwidth", "MHz", 0, None),
                     ("bcn_rx", BCN_RX_TITLE, "count", 0, None),
                     ("tput", "Throughput – iperf [SUM]", "Mbit/s", 0, None), ("per", "PER – iperf UDP loss", "%", 0, None)]
        grid.addWidget(self.mcs["tx"], 0, 0)
        grid.addWidget(self.mcs["rx"], 0, 1)
        for i, (key, title_, unit, lo, hi) in enumerate(specs):
            ch = Chart(title_, unit, lo, hi)
            if key in ("tput", "per"):
                ch.hint = IPERF_HINT
            self.charts[key] = ch
            grid.addWidget(ch, 1 + i // 2, i % 2)
        if self.stations is not None:
            grid.addWidget(self.stations, 1 + len(specs) // 2, len(specs) % 2)
        v.addLayout(grid)
        self.rows: list[tuple[float, dict]] = []      # (monotonic time, flat values) for CSV
        self._last_beacon: dict[str, tuple[float, int, str]] = {}

    def widgets(self) -> list[QWidget]:
        return list(self.charts.values()) + list(self.mcs.values())

    def set_window(self, sec: float | None) -> None:
        """None = whole run (charts from the first sample, MCS bars since the start)."""
        for ch in self.charts.values():
            ch.window = sec
        for bars in self.mcs.values():
            bars.window = None if sec is None else MCS_WINDOW_S

    def set_status(self, text: str, color: str = "#8a90a0") -> None:
        self.status.setText("   " + text)
        self.status.setStyleSheet(f"color: {tc(color)}; background: transparent;")

    def _row(self, t: float, flat: dict) -> None:
        if flat:
            self.rows.append((t, flat))
            del self.rows[:-20000]

    def add_iperf(self, s: dict, source: str) -> None:
        """One iperf [SUM] line (parsed) from the BB shell of this device."""
        t = time.monotonic()
        name = source + (f" {s['role']}" if s.get("role") else "")
        self.charts["tput"].add(name, t, s["mbps"])
        flat = {f"iperf.{name}.mbps": s["mbps"]}
        if s.get("per_pct") is not None:
            self.charts["per"].add(name, t, s["per_pct"])
            flat[f"iperf.{name}.per_pct"] = s["per_pct"]
        self._row(t, flat)

    def add_sample(self, metrics: dict) -> None:
        t = time.monotonic()
        flat: dict = {}
        if metrics.get("parse_error"):
            self.set_status(f"parser error: {metrics['parse_error']}", "#ff6b7a")
            return
        if self.role == "ap":
            for phy, r in sorted((metrics.get("radios") or {}).items()):
                self.mcs["tx"].add(phy, t, r.get("tx_mcs_hist"))
                self.mcs["rx"].add(phy, t, r.get("rx_mcs_hist"))
                for key, src in (("rssi", "rssi_dbm"), ("evm0", "evm0_db"), ("evm1", "evm1_db"),
                                 ("bcn_rx", "beacons_rx")):
                    if r.get(src) is not None:
                        self.charts[key].add(phy, t, r[src])
                        flat[f"{phy}.{src}"] = r[src]
                for src in ("tx_mcs", "rx_mcs"):
                    if r.get(src) is not None:
                        flat[f"{phy}.{src}"] = r[src]
                ms = self._beacon_ms(phy, r, t)
                if ms is not None:
                    self.charts["bcn"].add(phy, t, ms)
                    flat[f"{phy}.beacon_interval_ms"] = round(ms, 2)
            per = {mld: sts for mld, sts in (metrics.get("stations") or {}).items()}
            total = sum(len(s) for s in per.values())
            self.stations.value.setText(str(total))
            self.stations.detail.setText("   ".join(f"{m}: {len(s)}" for m, s in sorted(per.items())))
            names = [st.get("name") or st["mac"] for sts in per.values() for st in sts]
            self.stations.setToolTip("\n".join(names) or "no stations")
            flat["stations"] = total
            self.set_status("live" if flat else "no values in the output yet", "#3ccf7a" if flat else "#f0b43c")
        else:
            link = metrics.get("link") or {}
            self.mcs["tx"].add("wifi0", t, metrics.get("tx_mcs_hist"))
            self.mcs["rx"].add("wifi0", t, metrics.get("rx_mcs_hist"))
            for key, name, val in (("signal", "rx rssi", metrics.get("rssi_dbm")),
                                   ("signal", "link", link.get("signal_dbm")),
                                   ("bitrate", "tx", link.get("tx_bitrate_mbps")),
                                   ("bitrate", "rx", link.get("rx_bitrate_mbps")),
                                   ("evm", "pilot 0", metrics.get("evm0_db")),
                                   ("evm", "pilot 1", metrics.get("evm1_db")),
                                   ("bw", "rx", metrics.get("rx_bw_mhz")),
                                   ("bcn_rx", "beacons", metrics.get("beacons_rx"))):
                if val is not None:
                    self.charts[key].add(name, t, val)
                    flat[f"{key}.{name}"] = val
            for src in ("tx_mcs", "rx_mcs"):
                if metrics.get(src) is not None:
                    flat[src] = metrics[src]
            if link:
                state = "associated" if link.get("associated") else "NOT associated"
                peer = link.get("peer_name") or link.get("bssid") or ""
                self.set_status(f"live · {state}" + (f" → {peer}" if peer and link.get("associated") else ""),
                                "#3ccf7a" if link.get("associated") else "#f0b43c")
            else:
                self.set_status("live" if flat else "no values in the output yet", "#3ccf7a" if flat else "#f0b43c")
        self._row(t, flat)

    def _beacon_ms(self, phy: str, r: dict, now: float) -> float | None:
        total = r.get("legacy_total")
        if total is None:
            return None
        t, clock = (r["uptime_s"], "device") if r.get("uptime_s") is not None else (now, "pc")
        prev = self._last_beacon.get(phy)
        self._last_beacon[phy] = (t, total, clock)
        if not prev or prev[2] != clock or t <= prev[0]:
            return None
        dc = total - prev[1]
        return (t - prev[0]) / dc * 1000 if dc > 0 else None

    def clear(self) -> None:
        self.rows.clear()
        for w in self.widgets():
            w.clear()

    # --- capture: the charts + a summary table ----------------------------------------

    def summary(self) -> list[tuple[str, str, str, str, str, str]]:
        """(what, min, avg, max, last, samples) per line; MCS bars: (what, packets, most used MCS, ...)."""
        rows = []
        for ch in self.charts.values():
            for name, pts in sorted(ch.series.items()):
                vals = [v for _t, v in pts]
                if not vals:
                    continue
                what = ch.title + (f" – {name}" if name else "")
                f = (lambda v: _fmt_rate(v)) if ch.unit == "Mbit/s" else (
                    lambda v, u=ch.unit: (f"{v:.0f} {u}" if u == "count" else f"{v:.1f} {u}").strip())
                rows.append((what, f(min(vals)), f(sum(vals) / len(vals)), f(max(vals)), f(vals[-1]), str(len(vals))))
        for bars in self.mcs.values():
            for radio, b in sorted(bars.bars().items()):
                total = sum(b)
                if total <= 0:
                    continue
                best = max(range(len(b)), key=lambda k: b[k])
                scope = "since the start" if bars.window is None else f"last {MCS_WINDOW_S} s"
                rows.append((f"{bars.title} – {radio} ({scope})", f"{_short(total)} packets", f"mostly MCS {best}",
                             f"{100 * b[best] / total:.0f} % at MCS {best}", "", ""))
        if self.stations is not None and self.stations.value.text() not in ("", "–"):
            rows.append(("Connected stations", self.stations.value.text(), "", "", "", self.stations.detail.text()))
        return rows

    def capture(self) -> QImage:
        """The card as it is on screen, with the summary table drawn under it."""
        shot = self.grab()
        rows = self.summary()
        row_h, pad = 24, 14
        heads = ("", "min", "avg", "max", "last", "samples")
        w = shot.width()
        h = shot.height() + pad * 3 + row_h * (len(rows) + 1) + 20
        img = QImage(w, h, QImage.Format_ARGB32)
        img.fill(QColor(tc("#16181d")))
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        p.drawPixmap(0, 0, shot)
        top = shot.height() + pad * 2
        f = QFont(p.font())
        f.setPointSizeF(9)
        bold = QFont(f)
        bold.setBold(True)
        p.setFont(bold)
        p.setPen(QColor(tc("#d8dce4")))
        p.drawText(QRectF(pad, top - pad - 4, w - 2 * pad, 18), Qt.AlignLeft | Qt.AlignVCenter,
                   f"{self.name} – summary")
        first = max(260.0, (w - 2 * pad) * 0.34)
        rest = (w - 2 * pad - first) / (len(heads) - 1)
        xs = [pad, pad + first] + [pad + first + rest * i for i in range(1, len(heads))]
        p.setPen(QPen(QColor(tc("#2c3039"))))
        for r in range(len(rows) + 2):
            y = top + r * row_h
            p.drawLine(QPointF(pad, y), QPointF(w - pad, y))
        for x in xs + [w - pad]:
            p.drawLine(QPointF(x, top), QPointF(x, top + row_h * (len(rows) + 1)))
        p.setPen(QColor(tc("#d8dce4")))
        for i, hd in enumerate(heads):
            p.drawText(QRectF(xs[i] + 8, top, (first if i == 0 else rest) - 10, row_h),
                       (Qt.AlignLeft if i == 0 else Qt.AlignCenter) | Qt.AlignVCenter, hd)
        p.setFont(f)
        for r, row in enumerate(rows, 1):
            y = top + r * row_h
            for i, cell in enumerate(row):
                p.setPen(QColor(tc("#d8dce4" if i == 0 else "#8ab4ff")))
                width = (first if i == 0 else rest) - 10
                p.drawText(QRectF(xs[i] + 8, y, width, row_h), (Qt.AlignLeft if i == 0 else Qt.AlignCenter) | Qt.AlignVCenter,
                           p.fontMetrics().elidedText(cell, Qt.ElideRight, int(width)))
        p.end()
        return img

    def capture_to_file(self) -> None:
        from datetime import datetime
        name = f"{self.name}_{datetime.now():%Y%m%d_%H%M%S}.png".replace(" ", "_")
        path, _ = QFileDialog.getSaveFileName(self, "Save capture", name, "PNG image (*.png)")
        if path:
            self.capture().save(path)

    def export(self) -> None:
        if not self.rows:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export graph data", "graphs.csv", "CSV (*.csv)")
        if not path:
            return
        keys = sorted({k for _, f in self.rows for k in f})
        t0 = self.rows[0][0]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(",".join(["t_s"] + keys) + "\n")
            for t, f in self.rows:
                fh.write(",".join([f"{t - t0:.2f}"] + [str(f.get(k, "")) for k in keys]) + "\n")


class GraphsView(QWidget):
    restart_all = Signal()
    stop_all = Signal()

    def __init__(self) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        bar = QHBoxLayout()
        bar.addStretch(1)
        self.window = QComboBox()
        for sec, label in WINDOWS:
            self.window.addItem(label, sec)
        self.window.setCurrentIndex(0)
        self.window.setToolTip("Whole run: everything since the graphs started (MCS bars since the start too).\n"
                               "Last …: a window that scrolls (MCS bars: the last 60 s).")
        self.window.currentIndexChanged.connect(self._window)
        bar.addWidget(self.window)
        b = QPushButton("Capture all")
        b.setToolTip("Save every card (charts + summary table) as a PNG into a folder")
        b.clicked.connect(self.capture_all)
        bar.addWidget(b)
        b = QPushButton("Clear")
        b.clicked.connect(self.clear)
        bar.addWidget(b)
        for text, sig in (("⟳ Restart all", self.restart_all), ("■ Stop all", self.stop_all)):
            b = QPushButton(text)
            b.clicked.connect(sig.emit)
            bar.addWidget(b)
        lay.addLayout(bar)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        host = QWidget()
        self.box = QVBoxLayout(host)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(10)
        self.box.addStretch(1)
        scroll.setWidget(host)
        lay.addWidget(scroll, 1)
        self.empty = QLabel("Open AP / CPE shells in the Console: their graphs show up here.")
        self.empty.setObjectName("Muted")
        self.empty.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.empty)
        self.cards: dict[str, DeviceGraphs] = {}
        # repaint continuously so the lines move left even between samples
        self._timer = QTimer(self, interval=200, timeout=self._tick)
        self._timer.start()

    def _tick(self) -> None:
        if self.isVisible():
            for card in self.cards.values():
                for w in card.widgets():
                    w.update()

    def _window(self) -> None:
        for card in self.cards.values():
            card.set_window(self.window.currentData())

    def card(self, device_id: str, title: str, role: str) -> DeviceGraphs:
        c = self.cards.get(device_id)
        if c is None:
            c = DeviceGraphs(title, role)
            c.set_window(self.window.currentData())
            self.cards[device_id] = c
        return c

    def capture_all(self) -> None:
        from datetime import datetime
        from pathlib import Path
        if not self.cards:
            return
        folder = QFileDialog.getExistingDirectory(self, "Save the captures into")
        if not folder:
            return
        stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
        for card in self.cards.values():
            card.capture().save(str(Path(folder) / f"{card.name}_{stamp}.png".replace(" ", "_")))

    def remove(self, device_id: str) -> None:
        c = self.cards.pop(device_id, None)
        if c:
            c.deleteLater()

    def relayout(self, order: list[str]) -> None:
        while self.box.count():
            self.box.takeAt(0)
        ids = [i for i in order if i in self.cards] + [i for i in self.cards if i not in order]
        for i in ids:
            self.box.addWidget(self.cards[i])
        self.box.addStretch(1)
        self.empty.setVisible(not ids)

    def clear(self) -> None:
        for card in self.cards.values():
            card.clear()
