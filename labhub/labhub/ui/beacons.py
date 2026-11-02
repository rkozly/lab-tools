"""
Beacons tab: scan results from all observers as a table and an
RSSI-by-channel chart. The same store will feed future topology /
occupancy / heatmap views.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QHeaderView,
                               QLabel, QPushButton, QSpinBox, QSplitter,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from ..core.live import BeaconObservation

TYPE_COLOR = {"AP": "#2f6fed", "CPE": "#3ccf7a", "?": "#8a90a0"}


class BeaconChart(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[BeaconObservation] = []
        self.setMinimumHeight(220)

    def set_items(self, items: list[BeaconObservation]) -> None:
        self.items = items
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(48, 14, -16, -30)
        p.setPen(QPen(QColor("#2c3039")))
        lo, hi = -100.0, -20.0
        for db in range(-100, -19, 20):
            y = r.bottom() - (db - lo) / (hi - lo) * r.height()
            p.drawLine(int(r.left()), int(y), int(r.right()), int(y))
            p.setPen(QColor("#8a90a0"))
            p.drawText(QRectF(0, y - 8, 44, 16), Qt.AlignRight | Qt.AlignVCenter, f"{db}")
            p.setPen(QPen(QColor("#2c3039")))
        chans = sorted({o.channel or fmt_freq(o.freq_mhz) for o in self.items})
        if not chans:
            p.setPen(QColor("#5b6170"))
            p.drawText(r, Qt.AlignCenter, "No scan results yet")
            return
        slot = r.width() / len(chans)
        p.setPen(QColor("#8a90a0"))
        for i, ch in enumerate(chans):
            p.drawText(QRectF(r.left() + i * slot, r.bottom() + 6, slot, 18), Qt.AlignCenter, str(ch))
        font = QFont(p.font())
        font.setPointSizeF(8)
        p.setFont(font)
        per: dict[str, int] = {}
        for o in sorted(self.items, key=lambda o: o.rssi_dbm or -200):
            if o.rssi_dbm is None:
                continue
            ch = o.channel or fmt_freq(o.freq_mhz)
            i = chans.index(ch)
            k = per.get(ch, 0)
            per[ch] = k + 1
            x = r.left() + i * slot + slot / 2 + ((k % 5) - 2) * 14
            y = r.bottom() - (max(lo, min(hi, o.rssi_dbm)) - lo) / (hi - lo) * r.height()
            color = QColor(TYPE_COLOR.get(o.device_type, "#8a90a0"))
            p.setPen(QPen(color.lighter(130), 1.5))
            p.setBrush(QBrush(color) if o.matched_node_id else Qt.NoBrush)
            p.drawEllipse(QRectF(x - 6, y - 6, 12, 12))
            p.setPen(QColor("#d8dce4"))
            label = o.ssid or o.bssid[-8:]
            if o.matched_node_id:
                label = f"{label}"
            p.drawText(QRectF(x + 8, y - 8, 120, 16), Qt.AlignLeft | Qt.AlignVCenter, label)


def fmt_freq(f) -> str:
    return f"{f:g}" if isinstance(f, (int, float)) else "?"


class BeaconsView(QWidget):
    scan_requested = Signal()
    auto_changed = Signal(bool, int)
    clear_requested = Signal()

    COLS = ("Name", "Type", "Channel", "Freq (MHz)", "RSSI (dBm)", "Assoc", "BSSID", "Seen by", "Last seen")

    def __init__(self) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        bar = QHBoxLayout()
        self.observer = QComboBox()
        self.observer.addItem("All observers", "")
        self.observer.currentIndexChanged.connect(self._refresh)
        bar.addWidget(QLabel("Observer"))
        bar.addWidget(self.observer)
        self.lab_only = QCheckBox("Lab devices only")
        self.lab_only.toggled.connect(self._refresh)
        bar.addWidget(self.lab_only)
        bar.addStretch(1)
        self.last = QLabel()
        self.last.setObjectName("Muted")
        bar.addWidget(self.last)
        self.auto = QCheckBox("Auto-collect every")
        self.interval = QSpinBox()
        self.interval.setRange(3, 600)
        self.interval.setValue(10)
        self.interval.setSuffix(" s")
        self.auto.toggled.connect(lambda on: self.auto_changed.emit(on, self.interval.value()))
        self.interval.valueChanged.connect(lambda v: self.auto.isChecked() and self.auto_changed.emit(True, v))
        bar.addWidget(self.auto)
        bar.addWidget(self.interval)
        scan = QPushButton("Collect now")
        scan.setObjectName("Primary")
        scan.setToolTip("Read scan results from the target devices (Scan results preset)")
        scan.clicked.connect(self.scan_requested.emit)
        bar.addWidget(scan)
        clr = QPushButton("Clear")
        clr.clicked.connect(self.clear_requested.emit)
        bar.addWidget(clr)
        lay.addLayout(bar)

        split = QSplitter(Qt.Vertical)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        split.addWidget(self.table)
        self.chart = BeaconChart()
        split.addWidget(self.chart)
        split.setSizes([300, 240])
        lay.addWidget(split, 1)
        self.items: list[BeaconObservation] = []
        self.names: dict[str, str] = {}

    def set_items(self, items: list[BeaconObservation], names: dict[str, str]) -> None:
        self.items, self.names = items, names
        observers = sorted({o.observer_id for o in items})
        cur = self.observer.currentData()
        self.observer.blockSignals(True)
        self.observer.clear()
        self.observer.addItem("All observers", "")
        for ob in observers:
            self.observer.addItem(names.get(ob, ob), ob)
        self.observer.setCurrentIndex(max(0, self.observer.findData(cur)))
        self.observer.blockSignals(False)
        self._refresh()

    def _refresh(self) -> None:
        ob = self.observer.currentData()
        rows = [o for o in self.items if (not ob or o.observer_id == ob)
                and (not self.lab_only.isChecked() or o.matched_node_id)]
        # one row per BSSID: strongest observation, list of observers
        merged: dict[str, tuple[BeaconObservation, list[str], datetime]] = {}
        for o in rows:
            best, seen, last = merged.get(o.bssid, (o, [], o.time))
            if (o.rssi_dbm or -999) > (best.rssi_dbm or -999):
                best = o
            seen.append(self.names.get(o.observer_id, o.observer_id))
            merged[o.bssid] = (best, seen, max(last, o.time))
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(merged))
        now = datetime.now()
        for i, (best, seen, last) in enumerate(merged.values()):
            name = self.names.get(best.matched_node_id, "") if best.matched_node_id else ""
            vals = [name or best.ssid or "?", best.device_type, best.channel or "",
                    best.freq_mhz, best.rssi_dbm, "● yes" if best.associated else "–",
                    best.bssid, ", ".join(sorted(set(seen))), f"{last:%H:%M:%S}"]
            for c, v in enumerate(vals):
                it = QTableWidgetItem()
                if isinstance(v, (int, float)):
                    it.setData(Qt.DisplayRole, v)
                else:
                    it.setText(str(v))
                if (now - last).total_seconds() > 60:
                    it.setForeground(QColor("#5b6170"))
                elif c == 1:
                    it.setForeground(QColor(TYPE_COLOR.get(best.device_type, "#8a90a0")))
                self.table.setItem(i, c, it)
        self.table.setSortingEnabled(True)
        self.chart.set_items([m[0] for m in merged.values()])
        if self.items:
            self.last.setText(f"{len(merged)} BSS · last {max(o.time for o in self.items):%H:%M:%S}   ")
        else:
            self.last.setText("")
