"""
AP tab: the AP workflow as three steps, plus crash dumps.

  1. Scan       short scan (hw_ver_override <value>) or long scan (nothing)
  2. QCMAP      bridge / routing mode, typed into the AP shell with checks
  3. Stats      live view of beacons (legacy OFDM TX), MCS, RX, MLD stations
  Crash dumps   /data/vendor/ramdump: list, watch, pull, clear
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QGridLayout, QListWidget, QListWidgetItem,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QPushButton, QScrollArea, QSizePolicy,
                               QSpinBox, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from PySide6.QtWidgets import QApplication, QFileDialog, QPlainTextEdit

from .monitor import fmt


def section(title: str) -> tuple[QFrame, QVBoxLayout]:
    box = QFrame()
    box.setObjectName("Card")
    v = QVBoxLayout(box)
    v.setContentsMargins(14, 10, 14, 12)
    v.setSpacing(8)
    t = QLabel(title)
    t.setStyleSheet("font-weight: 600; font-size: 10.5pt; background: transparent;")
    v.addWidget(t)
    return box, v


def muted(text: str) -> QLabel:
    l = QLabel(text)
    l.setObjectName("Muted")
    l.setWordWrap(True)
    l.setStyleSheet("background: transparent;")
    return l


class Bars(QWidget):
    """Small bar chart (MCS histogram): highlights the most used index."""

    def __init__(self) -> None:
        super().__init__()
        self.values: list[float] = []
        self.setFixedHeight(54)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_values(self, values: list[float]) -> None:
        self.values = list(values)
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.values or max(self.values) <= 0:
            p.setPen(QColor("#5b6170"))
            p.drawText(self.rect(), Qt.AlignCenter, "no tx_mcs counters in the output")
            return
        n = len(self.values)
        w = self.width() / n
        top = max(self.values)
        best = self.values.index(top)
        h = self.height() - 14
        for i, v in enumerate(self.values):
            bh = max(1.0, v / top * h) if v > 0 else 0
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#2f6fed" if i == best else "#3a4152"))
            p.drawRoundedRect(QRectF(i * w + 1, h - bh, max(2.0, w - 2), bh), 2, 2)
        p.setPen(QColor("#8a90a0"))
        f = p.font()
        f.setPointSizeF(7)
        p.setFont(f)
        for i in range(n):
            if n <= 16 or i % 2 == 0:
                p.drawText(QRectF(i * w, h + 1, w, 12), Qt.AlignCenter, str(i))


PHY_COLORS = ["#3ccf7a", "#4aa3ff", "#f0b43c", "#c678dd"]


def nice_step(x: float) -> float:
    for st in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600):
        if st >= x:
            return float(st)
    return 600.0


class TimeChart(QWidget):
    """Lines over time: x = seconds before the newest sample, y = value."""

    def __init__(self) -> None:
        super().__init__()
        self.series: dict[str, list[tuple[float, float]]] = {}
        self.now, self.window, self.unit, self.zero_based = 0.0, 60.0, "", True
        self.setMinimumHeight(230)

    def set_data(self, series, now: float, window: float, unit: str, zero_based: bool) -> None:
        self.series, self.now, self.window, self.unit, self.zero_based = series, now, window, unit, zero_based
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        f = p.font()
        f.setPointSizeF(8)
        p.setFont(f)
        r = QRectF(self.rect()).adjusted(62, 18, -14, -26)
        pts = {k: [(t, v) for t, v in s if v is not None and 0 <= self.now - t <= self.window]
               for k, s in self.series.items()}
        vals = [v for s in pts.values() for _, v in s]
        p.setPen(QPen(QColor("#2c3039")))
        p.drawRect(r)
        p.setPen(QColor("#8a90a0"))
        p.drawText(QRectF(r.left(), 0, 300, 16), Qt.AlignLeft | Qt.AlignVCenter, self.unit)
        if not vals:
            p.setPen(QColor("#5b6170"))
            p.drawText(r, Qt.AlignCenter, "waiting for two samples…")
            return
        lo, hi = (0.0 if self.zero_based else min(vals)), max(vals)
        if hi - lo < 1e-9:
            hi = lo + (abs(lo) * 0.1 or 1.0)
        pad = (hi - lo) * 0.1
        hi += pad
        if not self.zero_based:
            lo -= pad

        def X(t: float) -> float:
            return r.right() - (self.now - t) / self.window * r.width()

        def Y(v: float) -> float:
            return r.bottom() - (v - lo) / (hi - lo) * r.height()

        for i in range(5):
            v = lo + (hi - lo) * i / 4
            y = Y(v)
            p.setPen(QPen(QColor("#23262e")))
            p.drawLine(int(r.left()), int(y), int(r.right()), int(y))
            p.setPen(QColor("#8a90a0"))
            txt = f"{v:,.0f}" if abs(hi) >= 1000 else f"{v:.1f}"
            p.drawText(QRectF(0, y - 8, 58, 16), Qt.AlignRight | Qt.AlignVCenter, txt)
        step = nice_step(self.window / 6)
        sec = 0.0
        while sec <= self.window + 1e-6:
            x = r.right() - sec / self.window * r.width()
            p.setPen(QPen(QColor("#23262e")))
            p.drawLine(int(x), int(r.top()), int(x), int(r.bottom()))
            p.setPen(QColor("#8a90a0"))
            p.drawText(QRectF(x - 32, r.bottom() + 5, 64, 16), Qt.AlignCenter, "now" if sec == 0 else f"-{sec:g} s")
            sec += step
        for i, (name, s) in enumerate(sorted(pts.items())):
            color = QColor(PHY_COLORS[i % len(PHY_COLORS)])
            p.setPen(QPen(color, 1.8))
            prev = None
            for t, v in s:
                pt = QPointF(X(t), Y(v))
                if prev is not None:
                    p.drawLine(prev, pt)
                prev = pt
            p.setBrush(color)
            for t, v in s:
                p.drawEllipse(QPointF(X(t), Y(v)), 2.4, 2.4)
            p.drawText(QRectF(r.right() - 90 * (len(pts) - i), 0, 86, 16), Qt.AlignRight | Qt.AlignVCenter,
                       f"● {name}")


class BeaconPanel(QFrame):
    """Beacon counter per phy, with time between beacons, chart and history."""

    MODES = (("Time between beacons", "bcn_interval_ms", "milliseconds between beacons", True),
             ("Beacons per second", "bcn_rate", "beacons per second", True),
             ("Beacon counter", "legacy_total", "beacon counter", False))
    WINDOWS = ((30, "last 30 s"), (60, "last 60 s"), (120, "last 2 min"), (300, "last 5 min"))
    COLS = ("phy", "beacon counter", "Δ counter", "Δ time", "beacons / s", "time between beacons")

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("Card")
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 10, 14, 12)
        v.setSpacing(8)
        head = QHBoxLayout()
        t = QLabel("Beacons per phy")
        t.setStyleSheet("font-weight: 600; font-size: 11pt; background: transparent;")
        head.addWidget(t)
        head.addWidget(muted("   counter = 'Legacy OFDM Rates' TX of wifistats <phy> 9"))
        head.addStretch(1)
        self.mode = QComboBox()
        for label, *_ in self.MODES:
            self.mode.addItem(label)
        self.mode.currentIndexChanged.connect(self._chart)
        head.addWidget(self.mode)
        self.window = QComboBox()
        for sec, label in self.WINDOWS:
            self.window.addItem(label, sec)
        self.window.setCurrentIndex(1)
        self.window.currentIndexChanged.connect(self._chart)
        head.addWidget(self.window)
        b = QPushButton("⇩ CSV")
        b.setToolTip("Export every sample (time, counter, Δ, ms between beacons) as CSV")
        b.clicked.connect(self.export)
        head.addWidget(b)
        b = QPushButton("Clear")
        b.clicked.connect(self.clear)
        head.addWidget(b)
        v.addLayout(head)

        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(26)
        self.grid.setVerticalSpacing(4)
        for c, h in enumerate(self.COLS):
            lbl = QLabel(h)
            lbl.setObjectName("MetricLabel")
            self.grid.addWidget(lbl, 0, c)
        self.grid.setColumnStretch(len(self.COLS), 1)
        v.addLayout(self.grid)
        self.rows: dict[str, list[QLabel]] = {}

        self.chart = TimeChart()
        v.addWidget(self.chart)
        self.note = muted("")
        v.addWidget(self.note)

        self.table = QTableWidget(0, 1)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setMinimumHeight(150)
        self.table.setMaximumHeight(230)
        v.addWidget(self.table)
        self.hist: dict[str, list[tuple[float, dict]]] = {}
        self.data: list[tuple[float, dict]] = []
        self.now = 0.0

    # --- data ---------------------------------------------------------------

    def clear(self) -> None:
        self.hist.clear()
        self.data.clear()
        for row in self.rows.values():
            for lbl in row:
                lbl.deleteLater()
        self.rows.clear()
        self.table.setRowCount(0)
        self.note.setText("")
        self._chart()

    def add_sample(self, radios: dict) -> None:
        entry, now, clock = {}, None, "device"
        for name, r in sorted(radios.items()):
            if r.get("legacy_total") is None or r.get("bcn_t") is None:
                continue
            t = r["bcn_t"]
            clock = r.get("bcn_clock", clock)
            now = t if now is None else max(now, t)
            self.hist.setdefault(name, []).append((t, r))
            del self.hist[name][:-900]
            self._row(name, r)
            entry[name] = r
        if not entry:
            self.note.setText("No 'Legacy OFDM Rates' lines in the output – use 'Copy raw output' below and send it.")
            self.note.setStyleSheet("color: #f0b43c; background: transparent;")
            return
        self.now = now
        self.data.append((now, entry))
        del self.data[:-900]
        src = "device clock (/proc/uptime)" if clock == "device" else "PC clock (less exact: includes adb delay)"
        self.note.setText(f"x axis = seconds, {src}.  Time between beacons = Δ time / Δ counter, averaged over "
                          f"each sample (~1 s).  ≈102 ms = one beacon every 100 TU.")
        self.note.setStyleSheet("color: #8a90a0; background: transparent;")
        self._chart()
        self._table()

    def _row(self, name: str, r: dict) -> None:
        row = self.rows.get(name)
        big = "background: transparent; font-size: 12pt; font-weight: 600;"
        if row is None:
            i = len(self.rows) + 1
            color = PHY_COLORS[(i - 1) % len(PHY_COLORS)]
            row = [QLabel() for _ in self.COLS]
            row[0].setText(f"<span style='color:{color}'>●</span> <b>{name}</b>")
            for c, lbl in enumerate(row):
                lbl.setStyleSheet(big if c in (1, 5) else "background: transparent;")
                self.grid.addWidget(lbl, i, c)
            self.rows[name] = row
        total, d, dt, rate, ms = (r.get(k) for k in ("legacy_total", "bcn_delta", "bcn_dt", "bcn_rate",
                                                       "bcn_interval_ms"))
        row[1].setText(f"{total:,}")
        if d is None:
            for lbl in row[2:5]:
                lbl.setText("…")
            row[5].setText("waiting for next sample")
            row[5].setStyleSheet("background: transparent; color: #8a90a0;")
            return
        row[2].setText(f"+{d:,}" + ("  (counter reset)" if r.get("bcn_reset") else ""))
        row[3].setText(f"{dt:.2f} s")
        row[4].setText(f"{rate:.1f}")
        if ms is None:
            row[5].setText(f"NO new beacons in {dt:.1f} s")
            row[5].setStyleSheet(big + " color: #ff6b7a;")
        else:
            row[5].setText(f"{ms:.1f} ms")
            row[5].setStyleSheet(big + " color: #3ccf7a;")

    def _chart(self, *_args) -> None:
        _label, key, unit, zero = self.MODES[self.mode.currentIndex()]
        series = {n: [(t, r.get(key)) for t, r in h] for n, h in self.hist.items()}
        self.chart.set_data(series, self.now, float(self.window.currentData() or 60), unit, zero)

    def _columns(self) -> tuple[list[str], list[str]]:
        names = sorted(self.hist)
        cols = ["device time (s)", "Δ time (s)"] + [f"{n} {c}" for n in names
                                                   for c in ("counter", "Δ", "ms between beacons")]
        return names, cols

    def _values(self, names: list[str], t: float, entry: dict) -> list[str]:
        first = next(iter(entry.values()))
        vals = [f"{t:.2f}", f"{first['bcn_dt']:.2f}" if first.get("bcn_dt") is not None else ""]
        for n in names:
            r = entry.get(n, {})
            ms = r.get("bcn_interval_ms")
            vals += [str(r.get("legacy_total", "")), str(r.get("bcn_delta", "")),
                     f"{ms:.1f}" if ms is not None else ("0 beacons" if r.get("bcn_delta") == 0 else "")]
        return vals

    def _table(self) -> None:
        names, cols = self._columns()
        self.table.setColumnCount(len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        rows = self.data[-150:][::-1]
        self.table.setRowCount(len(rows))
        for i, (t, entry) in enumerate(rows):
            for c, val in enumerate(self._values(names, t, entry)):
                it = QTableWidgetItem(val)
                if val == "0 beacons":
                    it.setForeground(QColor("#ff6b7a"))
                self.table.setItem(i, c, it)

    def export(self) -> None:
        if not self.data:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export beacons", "beacons.csv", "CSV (*.csv)")
        if not path:
            return
        names, cols = self._columns()
        with open(path, "w", encoding="utf-8") as f:
            f.write(",".join(cols) + "\n")
            for t, entry in self.data:
                f.write(",".join(self._values(names, t, entry)) + "\n")


class RadioCard(QFrame):
    """One radio (wifi0 / wifi1): TX MCS histogram and RX quality."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.setObjectName("Card")
        self.setMinimumWidth(300)
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 10, 14, 12)
        v.setSpacing(6)
        head = QHBoxLayout()
        t = QLabel(f"{name}  ·  TX MCS")
        t.setStyleSheet("font-weight: 600; font-size: 10.5pt; background: transparent;")
        head.addWidget(t)
        self.mcs_main = QLabel("–")
        self.mcs_main.setStyleSheet("background: transparent;")
        head.addWidget(self.mcs_main)
        head.addStretch(1)
        v.addLayout(head)
        self.mcs = Bars()
        v.addWidget(self.mcs)
        self.rx = QLabel("RX: –")
        self.rx.setObjectName("TileSub")
        self.rx.setWordWrap(True)
        v.addWidget(self.rx)

    def update_radio(self, r: dict) -> None:
        self.mcs.set_values(r.get("tx_mcs_hist") or [])
        self.mcs_main.setText(f"mostly MCS {r['tx_mcs']}" if r.get("tx_mcs") is not None else "–")
        self.rx.setText(f"RX  MCS {fmt(r.get('rx_mcs'))}  ·  BW {fmt(r.get('rx_bw_mhz'), ' MHz')}  ·  "
                        f"RSSI {fmt(r.get('rssi_dbm'), ' dBm')}  ·  EVM {fmt(r.get('evm0_db'), ' dB', 1)} / "
                        f"{fmt(r.get('evm1_db'), ' dB', 1)}")


class ApStatsView(QWidget):
    """Visual AP stats: beacons per phy, MCS per radio, MLD stations, raw text."""

    def __init__(self) -> None:
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        self.status = muted("Not running.")
        v.addWidget(self.status)
        self.beacons = BeaconPanel()
        v.addWidget(self.beacons)
        self.radio_row = QHBoxLayout()
        self.radio_row.setSpacing(10)
        v.addLayout(self.radio_row)
        self.radios: dict[str, RadioCard] = {}
        st, sv = section("Connected stations (wlanconfig mld0 / mld1 list)")
        self.sta = QTableWidget(0, 2)
        self.sta.setHorizontalHeaderLabels(["MLD", "Station"])
        self.sta.verticalHeader().hide()
        self.sta.setEditTriggers(QTableWidget.NoEditTriggers)
        self.sta.setMinimumHeight(120)
        self.sta.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.sta.horizontalHeader().setStretchLastSection(True)
        sv.addWidget(self.sta)
        v.addWidget(st)
        r = QHBoxLayout()
        self.raw_toggle = QCheckBox("Show raw output")
        r.addWidget(self.raw_toggle)
        cp = QPushButton("Copy raw output")
        cp.setToolTip("Copies the last raw stats output, to send for tuning the parser")
        cp.clicked.connect(self._copy)
        r.addWidget(cp)
        r.addStretch(1)
        v.addLayout(r)
        self.raw = QPlainTextEdit()
        self.raw.setObjectName("PaneText")
        self.raw.setReadOnly(True)
        self.raw.setMinimumHeight(220)
        self.raw.hide()
        self.raw_toggle.toggled.connect(self.raw.setVisible)
        v.addWidget(self.raw)
        self.last_raw = ""

    def _copy(self) -> None:
        QApplication.clipboard().setText(self.last_raw)
        self.set_status("Raw output copied to the clipboard." if self.last_raw else "No output yet.", "#4aa3ff")

    def set_status(self, text: str, color: str = "#8a90a0") -> None:
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color}; background: transparent;")

    def update_sample(self, sample, prev=None) -> None:
        m = sample.metrics
        self.last_raw = sample.raw
        radios = m.get("radios") or {}
        self.beacons.add_sample(radios)
        for name, r in sorted(radios.items()):
            card = self.radios.get(name)
            if card is None:
                card = RadioCard(name)
                self.radios[name] = card
                self.radio_row.addWidget(card)
            card.update_radio(r)
        rows = [(vap, st) for vap, sts in (m.get("stations") or {}).items() for st in sts]
        cols = ["MLD", "Station"] + sorted({k for _, st in rows for k in st if k not in ("mac", "name")})
        self.sta.setColumnCount(len(cols))
        self.sta.setHorizontalHeaderLabels(cols)
        self.sta.setRowCount(len(rows))
        for i, (vap, st) in enumerate(rows):
            vals = [vap, f"{st['name']}  ({st['mac']})" if st.get("name") else st["mac"]] + \
                   [str(st.get(c, "")) for c in cols[2:]]
            for c, val in enumerate(vals):
                self.sta.setItem(i, c, QTableWidgetItem(val))
        per_mld = ", ".join(f"{vap}: {len(sts)}" for vap, sts in (m.get("stations") or {}).items()) or "–"
        if m.get("parse_error"):
            self.set_status(f"Parser error: {m['parse_error']}", "#ff6b7a")
        else:
            self.set_status(f"Live · updated {sample.time:%H:%M:%S} · stations {per_mld}", "#3ccf7a")
        if self.raw.isVisible():
            self.raw.setPlainText(sample.raw)


class RamdumpPanel(QWidget):
    list_requested = Signal(str)
    watch_changed = Signal(bool, int)
    pull_requested = Signal(str, bool)   # path, selected files only
    clear_requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(QLabel("Folder"))
        self.path = QLineEdit("/data/vendor/ramdump")
        row.addWidget(self.path, 1)
        b = QPushButton("⟳ List")
        b.clicked.connect(lambda: self.list_requested.emit(self.path.text().strip()))
        row.addWidget(b)
        self.watch = QCheckBox("Watch every")
        self.every = QSpinBox()
        self.every.setRange(5, 3600)
        self.every.setValue(30)
        self.every.setSuffix(" s")
        self.watch.toggled.connect(lambda on: self.watch_changed.emit(on, self.every.value()))
        self.every.valueChanged.connect(lambda s: self.watch.isChecked() and self.watch_changed.emit(True, s))
        row.addWidget(self.watch)
        row.addWidget(self.every)
        v.addLayout(row)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(("Device", "File", "Size", "Date"))
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setMinimumHeight(150)
        v.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.info = muted("Press List to see crash dumps on the target devices.")
        row.addWidget(self.info, 1)
        pull_sel = QPushButton("⇩ Pull selected")
        pull_sel.clicked.connect(lambda: self.pull_requested.emit(self.path.text().strip(), True))
        row.addWidget(pull_sel)
        pull = QPushButton("⇩ Pull all")
        pull.setObjectName("Primary")
        pull.clicked.connect(lambda: self.pull_requested.emit(self.path.text().strip(), False))
        row.addWidget(pull)
        clr = QPushButton("🗑 Clear folder")
        clr.setObjectName("Danger")
        clr.clicked.connect(lambda: self.clear_requested.emit(self.path.text().strip()))
        row.addWidget(clr)
        v.addLayout(row)
        self.files: dict[str, list] = {}   # node id -> files
        self.names: dict[str, str] = {}

    def set_listing(self, ev) -> None:
        self.names[ev.node_id] = ev.name
        if ev.error:
            self.files[ev.node_id] = []
            self._set_info(f"{ev.name}: {ev.error}", "#ff6b7a")
        else:
            self.files[ev.node_id] = ev.files
        self._fill()
        if ev.missing_dir:
            self._set_info(f"{ev.name}: folder does not exist (no dumps)", "#8a90a0")
        elif ev.new:
            self._set_info(f"⚠ {ev.name}: NEW crash dump(s): {', '.join(ev.new)}", "#f0b43c")
        elif not ev.error:
            total = sum(len(f) for f in self.files.values())
            self._set_info(f"{total} file(s) · checked {datetime.now():%H:%M:%S}", "#8a90a0")

    def _set_info(self, text: str, color: str) -> None:
        self.info.setText(text)
        self.info.setStyleSheet(f"color: {color}; background: transparent;")

    def _fill(self) -> None:
        rows = [(nid, f) for nid, files in self.files.items() for f in files]
        self.table.setRowCount(len(rows))
        for i, (nid, f) in enumerate(rows):
            for c, val in enumerate((self.names.get(nid, nid), f.name, human(f.size), f.date)):
                it = QTableWidgetItem(val)
                it.setData(Qt.UserRole, (nid, f.name))
                self.table.setItem(i, c, it)

    def selected(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for idx in self.table.selectionModel().selectedRows():
            nid, name = self.table.item(idx.row(), 0).data(Qt.UserRole)
            out.setdefault(nid, []).append(name)
        return out


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


class ApView(QWidget):
    scan_requested = Signal(bool, str)          # short?, value
    qcmap_requested = Signal(str, int, int)     # preset id, delay ms, settle ms
    qcmap_stop = Signal()
    qcmap_exit = Signal()
    stats_start = Signal()
    stats_stop = Signal()
    shell_open = Signal()
    shell_show = Signal()
    reboot_requested = Signal()
    license_push = Signal()

    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        bar = QHBoxLayout()
        self.target = muted("")
        bar.addWidget(self.target)
        self.shell_state = QLabel("AP shell: not open")
        self.shell_state.setStyleSheet("color: #8a90a0; background: transparent;")
        bar.addWidget(self.shell_state, 1)
        self.max_sta = QLabel()
        self.max_sta.setToolTip("max_sta from the AP's QWES lines:\ntail -f -n +1 /tmp/hostapd.txt | grep QWES")
        bar.addWidget(self.max_sta)
        self.set_max_sta(None)
        b = QPushButton("🔑 Push license")
        b.setToolTip("adb push of the license file (Mappings → AP license) to the AP")
        b.clicked.connect(self.license_push.emit)
        bar.addWidget(b)
        b = QPushButton("⌨ Open AP shell")
        b.setToolTip("adb root + adb shell on the AP. The steps below run in this shell.")
        b.clicked.connect(self.shell_open.emit)
        bar.addWidget(b)
        b = QPushButton("Show shell in Console")
        b.clicked.connect(self.shell_show.emit)
        bar.addWidget(b)
        b = QPushButton("⏻ Reboot AP")
        b.setObjectName("Danger")
        b.setToolTip("Leaves QCMAP_CLI (Ctrl+C / Ctrl+Z), types 'reboot', and waits for the AP to come back")
        b.clicked.connect(self.reboot_requested.emit)
        bar.addWidget(b)
        outer.addLayout(bar)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 6, 0)
        lay.setSpacing(10)
        scroll.setWidget(host)
        outer.addWidget(scroll, 1)

        top = QHBoxLayout()
        top.setSpacing(10)
        # step 1
        box, v = section("1 · Scan")
        r = QHBoxLayout()
        self.short = QCheckBox("Short scan")
        self.short.setChecked(True)
        r.addWidget(self.short)
        r.addWidget(QLabel("hw_ver_override"))
        self.value = QLineEdit("2")
        self.value.setFixedWidth(50)
        r.addWidget(self.value)
        r.addStretch(1)
        v.addLayout(r)
        self.scan_cmd = muted("")
        v.addWidget(self.scan_cmd)
        b = QPushButton("Apply scan")
        b.setObjectName("Primary")
        b.clicked.connect(lambda: self.scan_requested.emit(self.short.isChecked(), self.value.text().strip()))
        v.addWidget(b)
        self.scan_result = QLabel("")
        self.scan_result.setWordWrap(True)
        self.scan_result.setStyleSheet("background: transparent;")
        v.addWidget(self.scan_result)
        self.short.toggled.connect(self._scan_text)
        self.value.textChanged.connect(self._scan_text)
        self._scan_text()
        v.addStretch(1)
        top.addWidget(box, 1)

        # step 2
        box, v = section("2 · QCMAP_CLI")
        r = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.addItem("Bridge mode   1-2-1 → 4-29-1 → 4-1-1", "ap-bridge-mode")
        self.mode.addItem("Routing mode  1-2-1 → 4-1-1", "ap-routing-mode")
        r.addWidget(self.mode, 1)
        v.addLayout(r)
        r = QHBoxLayout()
        r.addWidget(QLabel("Wait between keys"))
        self.delay = QSpinBox()
        self.delay.setRange(0, 30000)
        self.delay.setSingleStep(250)
        self.delay.setValue(1000)
        self.delay.setSuffix(" ms")
        r.addWidget(self.delay)
        r.addWidget(QLabel("quiet"))
        self.settle = QSpinBox()
        self.settle.setRange(100, 10000)
        self.settle.setSingleStep(100)
        self.settle.setValue(800)
        self.settle.setSuffix(" ms")
        self.settle.setToolTip("Output must be quiet this long (menu finished printing) before the next key")
        r.addWidget(self.settle)
        r.addStretch(1)
        v.addLayout(r)
        r = QHBoxLayout()
        r.addWidget(QLabel("Pass if it says"))
        self.success_words = QLineEdit()
        self.success_words.setToolTip("Comma separated. * = any ending (succe* = success, succeeds, succeeded).\n"
                                      "Numbered menu lines such as '13. QoS Configuration' are ignored.")
        r.addWidget(self.success_words, 1)
        v.addLayout(r)
        v.addWidget(muted("Runs in the AP shell. Each step (e.g. 1-2-1) must print one of these words, "
                          "otherwise it stops."))
        r = QHBoxLayout()
        b = QPushButton("▶ Run QCMAP")
        b.setObjectName("Primary")
        b.clicked.connect(lambda: self.qcmap_requested.emit(self.mode.currentData(), self.delay.value(),
                                                            self.settle.value()))
        r.addWidget(b)
        s = QPushButton("■ Stop")
        s.clicked.connect(self.qcmap_stop.emit)
        r.addWidget(s)
        x = QPushButton("⏏ Exit QCMAP_CLI")
        x.setToolTip("Ctrl+C, then Ctrl+Z; if the menu is still there, killall QCMAP_CLI")
        x.clicked.connect(self.qcmap_exit.emit)
        r.addWidget(x)
        r.addStretch(1)
        v.addLayout(r)
        self.qcmap_state = muted("")
        v.addWidget(self.qcmap_state)
        self.checklist = QGridLayout()
        self.checklist.setHorizontalSpacing(10)
        self.checklist.setVerticalSpacing(4)
        v.addLayout(self.checklist)
        self._rows: list[tuple[QLabel, QLabel, QLabel]] = []
        v.addStretch(1)
        top.addWidget(box, 1)

        # crash dumps
        box, v = section("Crash dumps  (/data/vendor/ramdump)")
        self.dumps = RamdumpPanel()
        v.addWidget(self.dumps)
        top.addWidget(box, 2)
        lay.addLayout(top)

        box, v = section("Results")
        self.results = QListWidget()
        self.results.setMinimumHeight(110)
        self.results.setMaximumHeight(160)
        v.addWidget(self.results)
        lay.addWidget(box)

        # step 3
        box, v = section("3 · Stats  –  beacons, MCS, RX, MLD stations")
        r = QHBoxLayout()
        b = QPushButton("▶ Start live stats")
        b.setObjectName("Primary")
        b.clicked.connect(self.stats_start.emit)
        r.addWidget(b)
        s = QPushButton("■ Stop")
        s.clicked.connect(self.stats_stop.emit)
        r.addWidget(s)
        r.addWidget(muted("Runs the AP stats command once per second (like watch -n 1) and draws it."))
        r.addStretch(1)
        v.addLayout(r)
        self.stats = ApStatsView()
        v.addWidget(self.stats)
        lay.addWidget(box)
        lay.addStretch(1)

    def _scan_text(self) -> None:
        if self.short.isChecked():
            self.scan_cmd.setText(f"sends:  cfg80211tool wifi0 hw_ver_override {self.value.text().strip() or '?'}")
        else:
            self.scan_cmd.setText("Long scan: no command is sent.")
        self.value.setEnabled(self.short.isChecked())

    def set_target(self, names: list[str]) -> None:
        if names:
            self.target.setText("AP target: " + ", ".join(names))
            self.target.setStyleSheet("color: #8a90a0; background: transparent;")
        else:
            self.target.setText("No AP in the lab. Add one in Mappings (role AP).")
            self.target.setStyleSheet("color: #f0b43c; background: transparent;")

    STATE = {"pass": ("✔", "#3ccf7a"), "done": ("✔", "#3ccf7a"), "failed": ("✖", "#ff6b7a"),
             "stopped": ("■", "#f0b43c"), "running": ("…", "#2f6fed"), "waiting": ("○", "#5b6170")}

    def set_qcmap_groups(self, groups: list[list[str]]) -> None:
        while self.checklist.count():
            w = self.checklist.takeAt(0).widget()
            if w:
                w.deleteLater()
        self._rows = []
        for i, g in enumerate(groups):
            icon, name, detail = QLabel("○"), QLabel("-".join(g)), QLabel("")
            name.setStyleSheet("font-family: Consolas; font-weight: 600; background: transparent;")
            detail.setObjectName("Muted")
            detail.setStyleSheet("background: transparent;")
            detail.setWordWrap(True)
            for c, wdg in enumerate((icon, name, detail)):
                self.checklist.addWidget(wdg, i, c)
            self.checklist.setColumnStretch(2, 1)
            self._rows.append((icon, name, detail))
        self.qcmap_step(0, "waiting", "")

    def qcmap_reset(self) -> None:
        for icon, _, detail in self._rows:
            icon.setText("○")
            icon.setStyleSheet("color: #5b6170; background: transparent;")
            detail.setText("")

    def qcmap_step(self, group: int, state: str, detail: str) -> None:
        if not (1 <= group <= len(self._rows)):
            return
        sym, color = self.STATE.get(state, ("•", "#8a90a0"))
        icon, _, lbl = self._rows[group - 1]
        icon.setText(sym)
        icon.setStyleSheet(f"color: {color}; background: transparent; font-weight: 600;")
        lbl.setText(detail)

    def set_scan_result(self, ok: bool | None, text: str) -> None:
        color = "#8a90a0" if ok is None else ("#3ccf7a" if ok else "#ff6b7a")
        sym = "…" if ok is None else ("✔ PASS" if ok else "✖ FAIL")
        self.scan_result.setText(f"{sym}  {text}")
        self.scan_result.setStyleSheet(f"color: {color}; background: transparent;")

    def log(self, ok: bool | None, text: str) -> None:
        sym = "•" if ok is None else ("✔" if ok else "✖")
        it = QListWidgetItem(f"{datetime.now():%H:%M:%S}  {sym}  {text}")
        it.setForeground(QColor("#8a90a0" if ok is None else ("#3ccf7a" if ok else "#ff6b7a")))
        self.results.insertItem(0, it)
        while self.results.count() > 200:
            self.results.takeItem(self.results.count() - 1)

    def set_max_sta(self, value: int | None, detail: str = "") -> None:
        """Stations the license allows; None -> N/A in red."""
        ok = value is not None
        self.max_sta.setText(f"  Max stations: {value if ok else 'N/A'}  ")
        self.max_sta.setStyleSheet(f"color: {'#3ccf7a' if ok else '#ff6b7a'}; font-weight: 600; background: transparent;")
        if detail:
            self.max_sta.setToolTip(detail)

    def set_shell(self, status: str, message: str = "") -> None:
        text, color = {"open": ("● AP shell connected", "#3ccf7a"),
                       "starting": ("… AP shell connecting (adb root)", "#2f6fed"),
                       "rebooting": ("⏻ AP rebooting… waiting for it to come back", "#f0b43c")}.get(
            status, (f"○ AP shell {(message or 'not open').removeprefix('shell ')}", "#8a90a0"))
        self.shell_state.setText("   " + text)
        self.shell_state.setStyleSheet(f"color: {color}; background: transparent;")

    def set_qcmap(self, text: str, state: str) -> None:
        color = {"done": "#3ccf7a", "failed": "#ff6b7a", "stopped": "#f0b43c"}.get(state, "#8a90a0")
        self.qcmap_state.setText(text)
        self.qcmap_state.setStyleSheet(f"color: {color}; background: transparent;")
