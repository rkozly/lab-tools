"""
Traffic drawing for the Traffic tab:

    [AP BB] ── [AP] ))) RF ((( [CPE1] ── [CPE1 BB]
                           ((( [CPE2] ── [CPE2 BB]

While a scenario runs, packets move along each AP <-> CPE path:
  downlink  AP BB -> AP -> RF -> CPE -> CPE BB      uplink the other way
  UDP       data packets one way only
  TCP       data packets one way + smaller ACKs coming back
"""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from .common import c as tc

BOX_W, BOX_H = 130, 46
ROW_H = 70
DATA = {"udp": "#4aa3ff", "tcp": "#3ccf7a"}
ACK = "#f0b43c"
SPEED = 140.0          # px / s
DATA_GAP, ACK_GAP = 34.0, 70.0


def _font(p: QPainter, size: float, bold: bool = False) -> None:
    f = p.font()
    f.setPointSizeF(size)
    f.setBold(bold)
    p.setFont(f)


class TrafficDiagram(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.ap: dict | None = None          # {name, bb_name, addr}
        self.cpes: list[dict] = []
        self.flow: tuple[str, str] | None = None   # ("ul" | "dl", "udp" | "tcp")
        self.active: set[str] | None = None         # CPE names in the run; None = all
        self.note = ""
        self.rates: dict[str, str] = {}             # CPE name -> "195 Mbit/s · 0.1 % loss"
        self.beacons: dict[str, int | None] = {}    # CPE name -> beacons heard (rx_legacy_ofdm_rate[3])
        self._t0 = time.monotonic()
        self._timer = QTimer(self, interval=33, timeout=self.update)
        self._resize()

    # --- data ------------------------------------------------------------------

    def set_nodes(self, ap: dict | None, cpes: list[dict]) -> None:
        self.ap, self.cpes = ap, list(cpes)
        self._resize()
        self.update()

    def set_flow(self, direction: str | None, proto: str = "udp", cpes: list[str] | None = None,
                 note: str = "") -> None:
        """direction 'ul' / 'dl' starts the animation; None stops it."""
        self.flow = (direction, proto) if direction else None
        self.active = set(cpes) if cpes else None
        self.note = note
        self._t0 = time.monotonic()
        if direction:
            self.rates.clear()
        if self.flow:
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def set_beacons(self, cpe: str, value: int | None) -> None:
        self.beacons[cpe] = value
        self.update()

    def set_rate(self, cpe: str, text: str) -> None:
        self.rates[cpe] = text
        self.update()

    def _resize(self) -> None:
        self.setMinimumHeight(max(1, len(self.cpes)) * ROW_H + 60)

    # --- layout -----------------------------------------------------------------

    def _boxes(self):
        """(ap_bb, ap, [(cpe, cpe_bb)]) rectangles."""
        w = self.width()
        top = 36
        rows = max(1, len(self.cpes))
        mid = top + rows * ROW_H / 2
        ap_bb = QRectF(16, mid - BOX_H / 2, BOX_W, BOX_H)
        ap = QRectF(ap_bb.right() + 40, mid - BOX_H / 2, BOX_W, BOX_H)
        cpe_bb_x = w - 16 - BOX_W
        cpe_x = cpe_bb_x - 40 - BOX_W
        pairs = []
        for i in range(len(self.cpes)):
            y = top + i * ROW_H + (ROW_H - BOX_H) / 2
            pairs.append((QRectF(cpe_x, y, BOX_W, BOX_H), QRectF(cpe_bb_x, y, BOX_W, BOX_H)))
        return ap_bb, ap, pairs

    # --- painting ---------------------------------------------------------------

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        ap_bb, ap, pairs = self._boxes()
        self._legend(p)
        if self.ap is None and not self.cpes:
            p.setPen(QColor(tc("#5b6170")))
            p.drawText(QRectF(self.rect()), Qt.AlignCenter, "No AP / CPEs in the lab (✎ Mappings)")
            return
        # wires BB -- device
        p.setPen(QPen(QColor(tc("#46506a")), 2))
        p.drawLine(QPointF(ap_bb.right(), ap_bb.center().y()), QPointF(ap.left(), ap.center().y()))
        for cpe, bb in pairs:
            p.drawLine(QPointF(cpe.right(), cpe.center().y()), QPointF(bb.left(), bb.center().y()))
        # RF: dashed links and waves at the antennas
        for i, (cpe, _bb) in enumerate(pairs):
            on = self._is_active(i)
            pen = QPen(QColor(tc("#5b6170") if not on else tc("#8a90a0")), 1.4, Qt.DashLine)
            p.setPen(pen)
            p.drawLine(QPointF(ap.right(), ap.center().y()), QPointF(cpe.left(), cpe.center().y()))
            self._waves(p, QPointF(cpe.left() - 4, cpe.center().y()), 180)
        self._waves(p, QPointF(ap.right() + 4, ap.center().y()), 0)
        if pairs:
            p.setPen(QColor(tc("#8a90a0")))
            _font(p, 9, True)
            x = (ap.right() + pairs[0][0].left()) / 2
            p.drawText(QRectF(x - 60, 8, 120, 18), Qt.AlignCenter, "))) RF (((")
        # packets
        if self.flow:
            for i, (cpe, bb) in enumerate(pairs):
                if self._is_active(i):
                    self._packets(p, [ap_bb.center(), ap.center(), cpe.center(), bb.center()])
        # boxes on top of the packet lines
        if self.ap is not None:
            self._box(p, ap_bb, self.ap.get("bb_name") or "AP BB", self.ap.get("addr", ""), bb=True)
            self._box(p, ap, self.ap.get("name") or "AP", "AP")
        for i, (cpe, bb) in enumerate(pairs):
            d = self.cpes[i]
            self._box(p, cpe, d.get("name", ""), "CPE", dim=not self._is_active(i) and self.flow is not None)
            bcn = self.beacons.get(d.get("name", ""))
            if bcn is not None:
                _font(p, 8)
                p.setPen(QColor(tc("#56d4dd")))
                p.drawText(QRectF(cpe.left() - 230, cpe.bottom() - 14, 222, 14), Qt.AlignRight | Qt.AlignVCenter,
                           f"📶 {bcn} bcn")
            rate = self.rates.get(d.get("name", ""))
            if rate:
                _font(p, 8.5, True)
                p.setPen(QColor(tc("#3ccf7a")))
                p.drawText(QRectF(cpe.left() - 230, cpe.top() - 16, 222, 16), Qt.AlignRight | Qt.AlignVCenter, rate)
            self._box(p, bb, d.get("bb_name") or "–", d.get("addr", ""), bb=True,
                      dim=not self._is_active(i) and self.flow is not None)

    def _is_active(self, i: int) -> bool:
        return self.active is None or self.cpes[i].get("name") in self.active

    def _legend(self, p: QPainter) -> None:
        _font(p, 8.5)
        if not self.flow:
            p.setPen(QColor(tc("#5b6170")))
            p.drawText(QRectF(8, 6, 400, 18), Qt.AlignLeft | Qt.AlignVCenter, "idle – no traffic running")
            return
        direction, proto = self.flow
        way = "Downlink  AP → CPEs" if direction == "dl" else "Uplink  CPEs → AP"
        p.setPen(QColor(tc("#d8dce4")))
        p.drawText(QRectF(8, 6, 420, 18), Qt.AlignLeft | Qt.AlignVCenter,
                   f"▶ {way}  ·  {proto.upper()}" + (f"   {self.note}" if self.note else ""))
        x = self.width() - 10
        items = [("data", DATA[proto], 4.5)] + ([("TCP ACK", ACK, 3.0)] if proto == "tcp" else [])
        for text, color, r in reversed(items):
            w = p.fontMetrics().horizontalAdvance(text) + 24
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(color))
            p.drawEllipse(QPointF(x - w + 6, 15), r, r)
            p.setPen(QColor(tc("#8a90a0")))
            p.drawText(QRectF(x - w + 14, 6, w - 14, 18), Qt.AlignLeft | Qt.AlignVCenter, text)
            x -= w + 6

    def _box(self, p: QPainter, r: QRectF, title: str, sub: str, bb: bool = False, dim: bool = False) -> None:
        fill = QColor(tc("#1a1d23") if bb else tc("#1d2740"))
        edge = QColor(tc("#3a3f4b") if bb else tc("#2f6fed"))
        if dim:
            fill.setAlpha(120)
            edge.setAlpha(90)
        p.setPen(QPen(edge, 1.5))
        p.setBrush(fill)
        p.drawRoundedRect(r, 7, 7)
        p.setPen(QColor(tc("#e8eaf0") if not dim else tc("#5b6170")))
        _font(p, 9.5, True)
        p.drawText(r.adjusted(6, 4, -6, -r.height() / 2), Qt.AlignCenter, title)
        p.setPen(QColor(tc("#8a90a0") if not dim else tc("#5b6170")))
        _font(p, 7.5)
        p.drawText(r.adjusted(4, r.height() / 2, -4, -3), Qt.AlignCenter,
                   p.fontMetrics().elidedText(sub, Qt.ElideMiddle, int(r.width() - 8)))

    def _waves(self, p: QPainter, c: QPointF, start_deg: float) -> None:
        p.setPen(QPen(QColor(tc("#5b6170")), 1.2))
        p.setBrush(Qt.NoBrush)
        for k in (6, 11, 16):
            p.drawArc(QRectF(c.x() - k, c.y() - k, 2 * k, 2 * k), int((start_deg - 40) * 16), int(80 * 16))

    def _packets(self, p: QPainter, pts: list[QPointF]) -> None:
        """Dots moving along the path (pts is in downlink order: AP BB ... CPE BB)."""
        direction, proto = self.flow
        segs = [(a, b, math.hypot(b.x() - a.x(), b.y() - a.y())) for a, b in zip(pts, pts[1:])]
        total = sum(L for _a, _b, L in segs) or 1.0
        t = time.monotonic() - self._t0

        def at(d: float) -> QPointF:
            for a, b, L in segs:
                if d <= L or (a, b, L) == segs[-1]:
                    f = min(1.0, d / L) if L else 0.0
                    return QPointF(a.x() + (b.x() - a.x()) * f, a.y() + (b.y() - a.y()) * f)
                d -= L
            return pts[-1]

        def dots(gap: float, forward: bool, color: str, r: float, offset: float = 0.0, dy: float = 0.0) -> None:
            p.setPen(Qt.NoPen)
            shift = (t * SPEED + offset) % gap
            d = shift
            while d < total:
                pos = d if forward else total - d
                fade = min(1.0, d / 30.0, (total - d) / 30.0)   # appear / vanish at the ends
                col = QColor(color)
                col.setAlphaF(max(0.15, fade))
                p.setBrush(col)
                q = at(pos) + QPointF(0, dy)
                p.drawEllipse(q, r, r)
                d += gap

        dl = direction == "dl"
        dots(DATA_GAP, dl, DATA[proto], 4.5)
        if proto == "tcp":
            dots(ACK_GAP, not dl, ACK, 3.0, offset=ACK_GAP / 2, dy=7)   # just below the data, going back
