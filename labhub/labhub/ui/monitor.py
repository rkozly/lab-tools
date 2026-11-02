"""
Monitor tab: one live card per device with parsed stats
(RSSI + quality, MCS, bandwidth, EVM, association), plus a sparkline.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QPlainTextEdit,
                               QPushButton, QScrollArea, QSizePolicy,
                               QToolButton, QVBoxLayout, QWidget)

from ..core.live import Sample


def quality(rssi: float | None, good: float, poor: float) -> tuple[str, str, float]:
    if rssi is None:
        return "no data", "#5b6170", 0.0
    frac = max(0.0, min(1.0, (rssi - (poor - 15)) / ((good + 10) - (poor - 15))))
    if rssi >= good:
        return "good", "#3ccf7a", frac
    if rssi >= poor:
        return "fair", "#f0b43c", frac
    return "poor", "#ff6b7a", frac


def fmt(v, unit: str = "", digits: int = 0) -> str:
    if v is None or v == "":
        return "–"
    if isinstance(v, float):
        v = f"{v:.{digits}f}"
    return f"{v}{unit}"


class Sparkline(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.values: list[float] = []
        self.color = QColor("#2f6fed")
        self.setFixedHeight(34)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_values(self, values: list[float], color: str) -> None:
        self.values = values[-60:]
        self.color = QColor(color)
        self.update()

    def paintEvent(self, _e) -> None:
        if len(self.values) < 2:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height() - 4
        lo, hi = min(self.values), max(self.values)
        span = (hi - lo) or 1.0
        step = w / (len(self.values) - 1)
        path = QPainterPath()
        for i, v in enumerate(self.values):
            pt = QPointF(i * step, 2 + h - (v - lo) / span * h)
            path.moveTo(pt) if i == 0 else path.lineTo(pt)
        p.setPen(QPen(self.color, 1.6))
        p.drawPath(path)


class QualityBar(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.frac, self.color = 0.0, QColor("#5b6170")
        self.setFixedHeight(6)

    def set_value(self, frac: float, color: str) -> None:
        self.frac, self.color = frac, QColor(color)
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#2c3039"))
        p.drawRoundedRect(self.rect(), 3, 3)
        if self.frac > 0:
            r = self.rect()
            r.setWidth(int(r.width() * self.frac))
            p.setBrush(self.color)
            p.drawRoundedRect(r, 3, 3)


def metric(label: str) -> tuple[QWidget, QLabel]:
    w = QWidget()
    w.setStyleSheet("background: transparent;")
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(0)
    val = QLabel("–")
    val.setObjectName("Metric")
    lbl = QLabel(label)
    lbl.setObjectName("MetricLabel")
    v.addWidget(val)
    v.addWidget(lbl)
    return w, val


class StatCard(QFrame):
    close_requested = Signal(str)
    export_requested = Signal(str)

    def __init__(self, node_id: str, name: str, role: str, thresholds: dict) -> None:
        super().__init__()
        self.setObjectName("Card")
        self.node_id, self.role, self.th = node_id, role, thresholds
        self.setMinimumWidth(330)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 10, 12)
        lay.setSpacing(6)
        head = QHBoxLayout()
        title = QLabel(name)
        title.setStyleSheet("font-weight: 600; font-size: 11pt; background: transparent;")
        head.addWidget(title)
        self.assoc = QLabel()
        self.assoc.setStyleSheet("background: transparent;")
        head.addWidget(self.assoc, 1)
        self.age = QLabel()
        self.age.setObjectName("MetricLabel")
        head.addWidget(self.age)
        for text, tip, fn in (("≡", "Show raw output", self._toggle_raw),
                              ("⇩", "Export history as CSV", lambda: self.export_requested.emit(self.node_id)),
                              ("✕", "Stop and close", lambda: self.close_requested.emit(self.node_id))):
            b = QToolButton()
            b.setObjectName("Mini")
            b.setText(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            head.addWidget(b)
        lay.addLayout(head)

        rssi_row = QHBoxLayout()
        w, self.rssi = metric("RSSI")
        rssi_row.addWidget(w)
        qbox = QVBoxLayout()
        self.qtext = QLabel("no data")
        self.qtext.setObjectName("MetricLabel")
        self.qbar = QualityBar()
        qbox.addWidget(self.qtext)
        qbox.addWidget(self.qbar)
        rssi_row.addLayout(qbox, 1)
        lay.addLayout(rssi_row)
        self.spark = Sparkline()
        lay.addWidget(self.spark)

        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        self.vals: dict[str, QLabel] = {}
        for i, (key, label) in enumerate((("tx_mcs", "TX MCS"), ("rx_mcs", "RX MCS"), ("rx_bw_mhz", "BW"),
                                          ("evm0_db", "EVM pilot 0"), ("evm1_db", "EVM pilot 1"))):
            w, v = metric(label)
            self.vals[key] = v
            grid.addWidget(w, i // 3, i % 3)
        lay.addLayout(grid)
        self.detail = QLabel()
        self.detail.setObjectName("TileSub")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.detail)
        self.error = QLabel()
        self.error.setStyleSheet("color: #ff6b7a; background: transparent;")
        self.error.setWordWrap(True)
        self.error.hide()
        lay.addWidget(self.error)
        self.raw = QPlainTextEdit()
        self.raw.setObjectName("PaneText")
        self.raw.setReadOnly(True)
        self.raw.setMinimumHeight(160)
        self.raw.hide()
        lay.addWidget(self.raw)
        self.last: datetime | None = None

    def _toggle_raw(self) -> None:
        self.raw.setVisible(not self.raw.isVisible())

    def set_error(self, text: str) -> None:
        self.error.setText(text)
        self.error.setVisible(bool(text))

    def tick(self) -> None:
        if self.last:
            s = int((datetime.now() - self.last).total_seconds())
            self.age.setText(f"{s}s ago " if s > 2 else "live ")
            self.age.setStyleSheet(f"color: {'#f0b43c' if s > 5 else '#8a90a0'}; background: transparent;")

    def update_sample(self, s: Sample, rssi_hist: list[float]) -> None:
        m = s.metrics
        self.last = s.time
        self.set_error("")
        rssi = m.get("rssi_dbm")
        q, color, frac = quality(rssi, self.th.get("rssi_good_dbm", -55), self.th.get("rssi_poor_dbm", -70))
        self.rssi.setText(fmt(rssi, " dBm"))
        self.rssi.setStyleSheet(f"color: {color}; background: transparent;")
        self.qtext.setText(f"signal quality: {q}")
        self.qbar.set_value(frac, color)
        self.spark.set_values(rssi_hist, color)

        if self.role == "ap":
            radios = m.get("radios", {})
            r0 = next(iter(radios.values()), {})
            vals = {k: r0.get(k) for k in self.vals}
            for k in vals:
                vals[k] = vals[k] if vals[k] is not None else next(
                    (r.get(k) for r in radios.values() if r.get(k) is not None), None)
            stations = m.get("stations", {})
            n = sum(len(v) for v in stations.values())
            self.assoc.setText(f"  <span style='color:#3ccf7a'>●</span> {n} station(s)" if n
                               else "  <span style='color:#8a90a0'>○ no stations</span>")
            lines = []
            for rname, r in radios.items():
                lines.append(f"{rname}: TX MCS {fmt(r.get('tx_mcs'))} · RX MCS {fmt(r.get('rx_mcs'))} · "
                             f"BW {fmt(r.get('rx_bw_mhz'), ' MHz')} · RSSI {fmt(r.get('rssi_dbm'), ' dBm')}")
            for vap, sts in stations.items():
                names = [st.get("name") or st["mac"] for st in sts]
                lines.append(f"{vap}: {', '.join(names) if names else '–'}")
            self.detail.setText("\n".join(lines))
        else:
            vals = {k: m.get(k) for k in self.vals}
            link = m.get("link", {})
            if link.get("associated"):
                peer = link.get("peer_name") or link.get("bssid", "")
                self.assoc.setText(f"  <span style='color:#3ccf7a'>●</span> associated → {peer}")
            elif link:
                self.assoc.setText("  <span style='color:#ff6b7a'>○ not associated</span>")
            else:
                self.assoc.setText("")
            ifc = m.get("ifconfig", {})
            parts = []
            if link.get("freq_mhz"):
                parts.append(f"freq {link['freq_mhz']:g}")
            if link.get("bssid"):
                parts.append(f"BSSID {link['bssid']}")
            if link.get("tx_bitrate"):
                parts.append(f"TX {link['tx_bitrate']}")
            if "rx_bytes" in ifc:
                parts.append(f"ath0 RX {ifc['rx_bytes'] / 1e6:.1f} MB · TX {ifc.get('tx_bytes', 0) / 1e6:.1f} MB")
            self.detail.setText(" · ".join(parts))
        for k, lbl in self.vals.items():
            v = vals.get(k)
            lbl.setText(fmt(v, " MHz" if k == "rx_bw_mhz" else (" dB" if k.startswith("evm") else ""),
                            1 if k.startswith("evm") else 0))
        if self.raw.isVisible() or not self.raw.toPlainText():
            self.raw.setPlainText(s.raw)


class MonitorView(QWidget):
    start_requested = Signal(str)        # preset id
    stop_requested = Signal(object)      # node id or None for all
    export_requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        bar = QHBoxLayout()
        self.info = QLabel("Start a stats preset (AP Stats / CPE Stats) on the selected devices.")
        self.info.setObjectName("Muted")
        bar.addWidget(self.info, 1)
        self.preset_box = QComboBox()
        bar.addWidget(self.preset_box)
        start = QPushButton("▶ Start on target")
        start.setObjectName("Primary")
        start.clicked.connect(lambda: self.start_requested.emit(self.preset_box.currentData() or ""))
        bar.addWidget(start)
        stop = QPushButton("■ Stop all")
        stop.setObjectName("Danger")
        stop.clicked.connect(lambda: self.stop_requested.emit(None))
        bar.addWidget(stop)
        lay.addLayout(bar)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.host = QWidget()
        self.grid = QGridLayout(self.host)
        self.grid.setSpacing(10)
        self.grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.scroll.setWidget(self.host)
        lay.addWidget(self.scroll, 1)
        self.cards: dict[str, StatCard] = {}

    def set_presets(self, presets: list) -> None:
        cur = self.preset_box.currentData()
        self.preset_box.clear()
        for p in presets:
            self.preset_box.addItem(p.name, p.id)
        i = self.preset_box.findData(cur)
        self.preset_box.setCurrentIndex(max(0, i))

    def card(self, node_id: str, name: str, role: str, thresholds: dict) -> StatCard:
        c = self.cards.get(node_id)
        if c is None:
            c = StatCard(node_id, name, role, thresholds)
            c.close_requested.connect(lambda nid: self.stop_requested.emit(nid))
            c.export_requested.connect(self.export_requested.emit)
            self.cards[node_id] = c
            self._relayout()
        return c

    def remove(self, node_id: str) -> None:
        c = self.cards.pop(node_id, None)
        if c:
            c.deleteLater()
            self._relayout()

    def _relayout(self) -> None:
        while self.grid.count():
            self.grid.takeAt(0)
        cols = max(1, (self.scroll.viewport().width() or 1000) // 360)
        for i, c in enumerate(self.cards.values()):
            self.grid.addWidget(c, i // cols, i % cols)
        self.info.setText(f"{len(self.cards)} device(s) monitored" if self.cards else
                          "Start a stats preset (AP Stats / CPE Stats) on the selected devices.")

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._relayout()

    def tick(self) -> None:
        for c in self.cards.values():
            c.tick()

    def ask_csv(self, name: str) -> str:
        path, _ = QFileDialog.getSaveFileName(self, "Export history", f"{name}_stats.csv", "CSV (*.csv)")
        return path
