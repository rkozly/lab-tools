"""
Live throughput graph of a traffic run (Traffic tab, next to the iperf3 shells).

Per CPE: received Mbit/s (server [SUM] lines, solid) and sent Mbit/s (client
[SUM] lines, dashed); under it the UDP loss %. The x axis is the whole run,
0 s .. now, so the drawing gets denser the longer the traffic runs.

split (default): one chart per CPE, stacked, same time axis and same Mbit/s
scale, so CPEs with the same rate do not hide each other; else all in one.

capture() renders the same chart with labelled axes plus a table of 10
samples spread over the run, for saving as a PNG.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from .common import c as tc

COLORS = ["#4aa3ff", "#3ccf7a", "#ff6b7a", "#56d4dd", "#e5c07b", "#98c379"]        # received, per CPE
SENT_COLORS = ["#f0a030", "#c678dd", "#ff9f43", "#d19aff", "#ffd166", "#f78fb3"]   # sent: never a received colour
RX_WIDTH, TX_WIDTH, LOSS_WIDTH = 2.8, 2.2, 2.0
MAX_POINTS = 200_000
CPE_H = 230                  # height of one CPE's chart (live, split view)


def _nice(top: float) -> float:
    """A round axis maximum >= top (1, 2, 2.5, 5 x 10^n)."""
    if top <= 0:
        return 1.0
    mag = 10 ** len(str(int(top))) / 10
    for m in (1, 2, 2.5, 5, 10):
        if m * mag >= top:
            return m * mag
    return 10 * mag


def _time_step(span: float) -> float:
    for s in (5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400):
        if span / s <= 8:
            return s
    return 28800


def fmt_rate(mbps: float) -> str:
    """1923 -> '1.92 Gbit/s', 671 -> '671 Mbit/s'."""
    return f"{mbps / 1000:.2f} Gbit/s" if mbps >= 1000 else f"{mbps:.0f} Mbit/s"


def fmt_t(sec: float) -> str:
    sec = int(sec)
    if sec < 120:
        return f"{sec}s"
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class TrafficGraph(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.series: dict[tuple[str, str], list[tuple[float, float]]] = {}   # (cpe, "rx"/"tx") -> [(t, Mbit/s)]
        self.loss: dict[str, list[tuple[float, float]]] = {}                 # cpe -> [(t, %)]
        self.order: list[str] = []                                           # CPEs, colour order
        self.title = ""
        self.direction = "dl"                 # dl: AP -> CPEs (sent = AP -> CPE1), ul: CPEs -> AP
        self.sent = True                      # draw what was sent (UDP); TCP: only what arrived
        self.t0: float | None = None
        self.running = False
        self.split = True
        self.setMinimumHeight(320)
        self._timer = QTimer(self, interval=1000, timeout=self.update)

    # --- data -------------------------------------------------------------------

    def reset(self, title: str, cpes: list[str], direction: str = "dl", sent: bool = True) -> None:
        self.direction = direction
        self.sent = sent
        self.series.clear()
        self.loss.clear()
        self.order = list(cpes)
        self._fit_height()
        self.title = title
        self.t0 = time.monotonic()
        self.running = True
        self._timer.start()
        self.update()

    def stop(self) -> None:
        self.running = False
        self._timer.stop()
        self.update()

    def add(self, cpe: str, kind: str, mbps: float, loss_pct: float | None = None) -> None:
        if self.t0 is None or not self.running or (kind == "tx" and not self.sent):
            return
        t = time.monotonic() - self.t0
        if cpe not in self.order:
            self.order.append(cpe)
            self._fit_height()
        pts = self.series.setdefault((cpe, kind), [])
        pts.append((t, mbps))
        del pts[:-MAX_POINTS]
        if loss_pct is not None:
            lp = self.loss.setdefault(cpe, [])
            lp.append((t, loss_pct))
            del lp[:-MAX_POINTS]

    def set_split(self, split: bool) -> None:
        self.split = split
        self._fit_height()
        self.update()

    def _groups(self) -> list[list[str]]:
        return [[c] for c in self.order] if self.split and len(self.order) > 1 else [list(self.order)]

    def _fit_height(self) -> None:
        self.setMinimumHeight(max(320, CPE_H * len(self._groups())) if self.split else 320)

    def has_data(self) -> bool:
        return any(self.series.values())

    def span(self) -> float:
        end = max((p[-1][0] for p in self.series.values() if p), default=0.0)
        if self.running and self.t0 is not None:
            end = max(end, time.monotonic() - self.t0)
        return max(end, 10.0)

    def label(self, cpe: str, kind: str) -> str:
        """Name of a line: sent = who sends to whom (downlink AP -> CPE1), received = where it arrives."""
        if kind == "loss":
            return f"{cpe} loss"
        if self.direction == "ul":
            return f"{cpe} → AP" if kind == "tx" else f"AP received from {cpe}"
        return f"AP → {cpe}" if kind == "tx" else f"{cpe} received"

    def color(self, cpe: str, kind: str = "rx") -> str:
        i = self.order.index(cpe) if cpe in self.order else 0
        return (SENT_COLORS if kind == "tx" else COLORS)[i % len(COLORS)]

    # --- painting ---------------------------------------------------------------------

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        self.draw(p, QRectF(self.rect()), live=True)

    def draw(self, p: QPainter, area: QRectF, live: bool = False, print_mode: bool = False) -> None:
        pal = {"fg": "#1f2430" if print_mode else tc("#d8dce4"), "muted": "#5f6675" if print_mode else tc("#8a90a0"),
               "grid": "#d4d8e0" if print_mode else tc("#23262e"), "frame": "#9aa3b5" if print_mode else tc("#2c3039")}
        f = QFont(p.font())
        f.setPointSizeF(8.5)
        p.setFont(f)
        span = self.span()
        bold = QFont(f)
        bold.setBold(True)
        p.setFont(bold)
        p.setPen(QColor(pal["fg"]))
        head = self.title or "Throughput"
        if live and self.running:
            head += f"   ·   running {fmt_t(span)}"
        p.drawText(QRectF(area.left() + 8, area.top() + 2, area.width() - 16, 20), Qt.AlignLeft | Qt.AlignVCenter, head)
        p.setFont(f)
        body = area.adjusted(0, 22, 0, 0)
        if not self.has_data():
            r = body.adjusted(62, 22, -14, -34)
            p.setPen(QPen(QColor(pal["frame"])))
            p.drawRect(r)
            p.setPen(QColor(pal["muted"]))
            p.drawText(r, Qt.AlignCenter, "No traffic yet – the [SUM] lines of the iperf3 shells are drawn here")
            return
        # one scale for every chart, so the CPEs can be compared
        top = _nice(max(v for pts in self.series.values() for _t, v in pts) * 1.05)
        ltop = _nice(max(max((v for pts in self.loss.values() for _t, v in pts), default=0), 1.0) * 1.1)
        groups = self._groups()
        h = body.height() / len(groups)
        for gi, cpes in enumerate(groups):
            block = QRectF(body.left(), body.top() + gi * h, body.width(), h - (8 if gi < len(groups) - 1 else 0))
            self._block(p, block, cpes, span, top, ltop, pal, print_mode, label=len(groups) > 1,
                        x_title=gi == len(groups) - 1)

    def _block(self, p: QPainter, area: QRectF, cpes: list[str], span: float, top: float, ltop: float, pal: dict,
               print_mode: bool, label: bool, x_title: bool) -> None:
        """The chart of some CPEs (one in the split view): throughput, and the loss % under it."""
        fg, muted, grid, frame = pal["fg"], pal["muted"], pal["grid"], pal["frame"]
        has_loss = any(self.loss.get(c) for c in cpes)
        bottom = 34 if x_title else 18
        if label:                             # which CPE this chart is
            bold = QFont(p.font())
            bold.setBold(True)
            bold.setPointSizeF(10)
            f = p.font()
            p.setFont(bold)
            p.setPen(QColor(self.color(cpes[0])))
            p.drawText(QRectF(area.left() + 62, area.top(), 200, 20), Qt.AlignLeft | Qt.AlignVCenter, cpes[0])
            p.setFont(f)
        x = area.right() - 8
        for cpe in reversed(cpes):
            kinds = (("tx", f"┄ {self.label(cpe, 'tx')}"),) if self.sent else ()
            for kind, txt in kinds + (("rx", f"━ {self.label(cpe, 'rx')}"),):
                w = p.fontMetrics().horizontalAdvance(txt) + 14
                p.setPen(QColor(self.color(cpe, kind)))
                p.drawText(QRectF(x - w, area.top(), w, 20), Qt.AlignRight | Qt.AlignVCenter, txt)
                x -= w
            x -= 10
        inner = area.adjusted(62, 22, -14, 0)
        top_h = (inner.height() - bottom) * (0.74 if has_loss else 1.0)
        chart = QRectF(inner.left(), inner.top(), inner.width(), top_h - (18 if has_loss else 0))
        self._axes(p, chart, span, top, "Gbit/s" if top >= 1000 else "Mbit/s", fg, muted, grid, frame, print_mode,
                   x_title=x_title and not has_loss, scale=1000 if top >= 1000 else 1)
        p.save()
        p.setClipRect(chart.adjusted(-1, -4, 1, 1))
        for (cpe, kind), pts in sorted(self.series.items(), key=lambda kv: kv[0][1] == "rx"):   # sent under received
            if cpe in cpes:
                self._line(p, chart, pts, span, top, self.color(cpe, kind), kind == "tx",
                           TX_WIDTH if kind == "tx" else RX_WIDTH)
        p.restore()
        if has_loss:
            lc = QRectF(chart.left(), inner.top() + top_h + 4, chart.width(), inner.height() - top_h - 4 - bottom)
            self._axes(p, lc, span, ltop, "loss %", fg, muted, grid, frame, print_mode, ticks=2, x_title=x_title)
            p.save()
            p.setClipRect(lc.adjusted(-1, -4, 1, 1))
            for cpe in cpes:
                self._line(p, lc, self.loss.get(cpe, []), span, ltop, self.color(cpe), False, LOSS_WIDTH)
            p.restore()

    def _axes(self, p, r: QRectF, span: float, top: float, unit: str, fg, muted, grid, frame, print_mode,
              ticks: int = 5, x_title: bool = True, scale: float = 1) -> None:
        p.setPen(QPen(QColor(frame)))
        p.drawRect(r)
        for i in range(ticks + 1):
            v = top * i / ticks
            y = r.bottom() - v / top * r.height()
            p.setPen(QPen(QColor(grid)))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(QColor(muted))
            sv = v / scale                    # Gbit/s labels when the scale is 1000
            label = (f"{sv:.2f}" if scale > 1 else f"{sv:,.0f}") if top >= 10 else f"{sv:.1f}"
            p.drawText(QRectF(r.left() - 60, y - 8, 54, 16), Qt.AlignRight | Qt.AlignVCenter, label)
        p.save()
        p.translate(r.left() - 50, r.center().y())
        p.rotate(-90)
        p.setPen(QColor(fg))
        p.drawText(QRectF(-60, -14, 120, 14), Qt.AlignCenter, unit)
        p.restore()
        step = _time_step(span)
        t = 0.0
        while t <= span + 0.01:
            xx = r.left() + t / span * r.width()
            p.setPen(QPen(QColor(grid)))
            p.drawLine(QPointF(xx, r.top()), QPointF(xx, r.bottom()))
            p.setPen(QColor(muted))
            p.drawText(QRectF(xx - 30, r.bottom() + 3, 60, 14), Qt.AlignCenter, fmt_t(t))
            t += step
        if x_title:
            p.setPen(QColor(fg))
            p.drawText(QRectF(r.left(), r.bottom() + 16, r.width(), 14), Qt.AlignCenter, "time since the traffic started")

    @staticmethod
    def _line(p, r: QRectF, pts, span: float, top: float, color: str, dashed: bool, width: float = 1.6) -> None:
        if not pts:
            return
        # many more points than pixels: keep one (the mean) per pixel column so long runs stay fast
        cols: dict[int, list[float]] = {}
        for t, v in pts:
            cols.setdefault(int(t / span * r.width()), []).append(v)
        path = QPainterPath()
        for i, (cx, vals) in enumerate(sorted(cols.items())):
            pt = QPointF(r.left() + cx, r.bottom() - min(1.0, (sum(vals) / len(vals)) / top) * r.height())
            path.moveTo(pt) if i == 0 else path.lineTo(pt)
        pen = QPen(QColor(color), width, Qt.DashLine if dashed else Qt.SolidLine)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)

    # --- capture ------------------------------------------------------------------------

    def samples(self, n: int = 10) -> list[dict]:
        """n rows spread evenly over the run: {t, (cpe, kind): Mbit/s, (cpe, 'loss'): %}."""
        span = self.span()
        near = max(2.0, span / (2 * n))        # a sample only from a point close to its time (a gap stays a gap)
        rows = []
        for i in range(n):
            t = span * (i + 1) / n
            row = {"t": t}
            for key, pts in list(self.series.items()) + [((c, "loss"), v) for c, v in self.loss.items()]:
                best = min(pts, key=lambda tv: abs(tv[0] - t), default=None)
                if best is not None and abs(best[0] - t) <= near:
                    row[key] = best[1]
            rows.append(row)
        return rows

    def capture(self, width: int = 1600) -> QImage:
        """The chart (whole run, white background) with a table of 10 samples under it."""
        rows = self.samples(10)
        cols = [(c, "rx") for c in self.order if (c, "rx") in self.series]   # throughput only
        row_h = 26
        chart_h = max(620, 330 * len(self._groups())) if self.split else 620
        height = chart_h + 60 + row_h * (len(rows) + 1) + 30
        img = QImage(width, height, QImage.Format_ARGB32)
        img.fill(QColor("#ffffff"))
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        # the chart with throughput only: no "sent" lines, no loss strip
        series, loss, sent = self.series, self.loss, self.sent
        self.series = {k: v for k, v in series.items() if k[1] == "rx"}
        self.loss, self.sent = {}, False
        try:
            self.draw(p, QRectF(10, 10, width - 20, chart_h), print_mode=True)
        finally:
            self.series, self.loss, self.sent = series, loss, sent
        # table
        f = QFont(p.font())
        f.setPointSizeF(9)
        p.setFont(f)
        top = chart_h + 40
        heads = ["time"] + [f"{c} throughput" for c, _k in cols]
        cw = (width - 40) / max(1, len(heads))
        p.setPen(QPen(QColor("#9aa3b5")))
        for r in range(len(rows) + 2):
            y = top + r * row_h
            p.drawLine(QPointF(20, y), QPointF(width - 20, y))
        for ci in range(len(heads) + 1):
            p.drawLine(QPointF(20 + ci * cw, top), QPointF(20 + ci * cw, top + row_h * (len(rows) + 1)))
        bold = QFont(f)
        bold.setBold(True)
        p.setFont(bold)
        p.setPen(QColor("#1f2430"))
        for ci, h in enumerate(heads):
            p.drawText(QRectF(20 + ci * cw, top, cw, row_h), Qt.AlignCenter, h)
        p.setFont(f)
        for ri, row in enumerate(rows, 1):
            y = top + ri * row_h
            p.setPen(QColor("#1f2430"))
            p.drawText(QRectF(20, y, cw, row_h), Qt.AlignCenter, fmt_t(row["t"]))
            for ci, key in enumerate(cols, 1):
                v = row.get(key)
                txt = "–" if v is None else (f"{v:.2f}" if key[1] == "loss" else fmt_rate(v))
                p.setPen(QColor(self.color(key[0], key[1])) if v is not None else QColor("#9aa0ad"))
                p.drawText(QRectF(20 + ci * cw, y, cw, row_h), Qt.AlignCenter, txt)
        p.end()
        return img
