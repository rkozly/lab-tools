"""
Topology tab: every device and backbone as a card, with its interfaces
drawn as a tree (child under its father: br-lan -> eth0 / ath0, wifi0 ->
ath0, eth0 -> eth0.960). A device is joined to its backbone by a line to
the BB interfaces from Mappings. Click an interface to see its config.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QCheckBox, QGraphicsItem, QGraphicsObject, QGraphicsScene,
                               QGraphicsView, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
                               QSplitter, QVBoxLayout, QWidget)

from .common import c

ROW_H, INDENT, PILL_W, CARD_PAD, HEAD_H = 26, 22, 250, 12, 34
CARD_W = PILL_W + 4 * INDENT + 2 * CARD_PAD
COL_GAP, ROW_GAP = 230, 40
KIND_ICON = {"bridge": "⛓", "bond": "⛓", "wireless": "📶", "vlan": "⑂", "virtual": "◇", "loopback": "↺"}
GREEN, RED, BLUE, MUTED = "#3ccf7a", "#ff6b7a", "#2f6fed", "#8a90a0"


class IfacePill(QGraphicsObject):
    clicked = Signal(str, str)     # node id, iface name

    def __init__(self, node_id: str, ia, width: float) -> None:
        super().__init__()
        self.node_id, self.ia, self.w = node_id, ia, width
        self.highlight = False
        self.selected = False
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.PointingHandCursor)
        addr = (ia.ipv4 + ia.ipv6)[:1]
        tip = [f"{ia.name}  ({ia.kind or 'ethernet'}, {ia.state or '?'})"] + ia.relations() + ia.ipv4 + ia.ipv6
        self.setToolTip("\n".join(tip) + "\n\nClick for the full config")
        self.text = f"{KIND_ICON.get(ia.kind, '▭')} {ia.name}" + (f"   {addr[0]}" if addr else "")
        self._hover = False

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self.w, ROW_H - 6)

    def paint(self, p: QPainter, _opt, _w=None) -> None:
        up = self.ia.state in ("up", "unknown", "")
        border = BLUE if self.selected else (GREEN if self.highlight else ("#46506a" if up else "#6b2b35"))
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(c(border)), 2 if self.selected or self.highlight else 1))
        p.setBrush(QBrush(QColor(c("#253354" if self._hover or self.selected else "#1f232b"))))
        p.drawRoundedRect(self.boundingRect(), 6, 6)
        dot = GREEN if self.ia.state == "up" else (RED if self.ia.state == "down" else MUTED)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(c(dot)))
        p.drawEllipse(QRectF(7, (ROW_H - 6) / 2 - 3, 6, 6))
        p.setPen(QColor(c("#d8dce4" if up else "#8a90a0")))
        f = QFont("Consolas", 8)
        p.setFont(f)
        p.drawText(QRectF(18, 0, self.w - 22, ROW_H - 6), Qt.AlignVCenter | Qt.AlignLeft,
                   p.fontMetrics().elidedText(self.text, Qt.ElideRight, int(self.w - 24)))

    def hoverEnterEvent(self, _e) -> None:
        self._hover = True
        self.update()

    def hoverLeaveEvent(self, _e) -> None:
        self._hover = False
        self.update()

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self.node_id, self.ia.name)


class Card(QGraphicsItem):
    """A node: title bar + its interface tree."""

    def __init__(self, title: str, subtitle: str, status_color: str, height: float, message: str = "") -> None:
        super().__init__()
        self.title, self.subtitle, self.color, self.h, self.message = title, subtitle, status_color, height, message

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, CARD_W, self.h)

    def paint(self, p: QPainter, _opt, _w=None) -> None:
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(c("#2c3039")), 1))
        p.setBrush(QColor(c("#1c1f26")))
        p.drawRoundedRect(self.boundingRect(), 10, 10)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(c(self.color)))
        p.drawEllipse(QRectF(CARD_PAD, HEAD_H / 2 - 5, 10, 10))
        f = QFont()
        f.setBold(True)
        f.setPointSizeF(10)
        p.setFont(f)
        p.setPen(QColor(c("#ffffff")))
        p.drawText(QRectF(CARD_PAD + 16, 0, CARD_W, HEAD_H), Qt.AlignVCenter, self.title)
        w = p.fontMetrics().horizontalAdvance(self.title)
        f.setBold(False)
        f.setPointSizeF(8)
        p.setFont(f)
        p.setPen(QColor(c(MUTED)))
        p.drawText(QRectF(CARD_PAD + 24 + w, 0, CARD_W - w - 40, HEAD_H), Qt.AlignVCenter, self.subtitle)
        if self.message:
            p.drawText(QRectF(CARD_PAD, HEAD_H, CARD_W - 2 * CARD_PAD, self.h - HEAD_H - 6),
                       Qt.AlignLeft | Qt.TextWordWrap, self.message)


class TopologyView(QWidget):
    refresh_requested = Signal()
    iface_clicked = Signal(str, str)     # node id, iface name

    def __init__(self) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        bar = QHBoxLayout()
        self.status = QLabel("Press ⟳ Refresh to read the interfaces of every device and backbone.")
        self.status.setObjectName("Muted")
        bar.addWidget(self.status, 1)
        self.hide_lo = QCheckBox("Hide lo")
        self.hide_lo.setChecked(True)
        self.hide_lo.toggled.connect(lambda _on: self.redraw())
        bar.addWidget(self.hide_lo)
        self.hide_down = QCheckBox("Hide down")
        self.hide_down.setToolTip("Hide interfaces that are down (and have nothing up under them)")
        self.hide_down.toggled.connect(lambda _on: self.redraw())
        bar.addWidget(self.hide_down)
        for text, fn in (("⊕", lambda: self.view.scale(1.15, 1.15)), ("⊖", lambda: self.view.scale(1 / 1.15, 1 / 1.15)),
                         ("Fit", self.fit)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            bar.addWidget(b)
        ref = QPushButton("⟳ Refresh")
        ref.setObjectName("Primary")
        ref.clicked.connect(self.refresh_requested.emit)
        bar.addWidget(ref)
        lay.addLayout(bar)

        split = QSplitter(Qt.Horizontal)
        self.scene = QGraphicsScene(self)
        self.view = QGraphicsView(self.scene)
        self.view.setRenderHint(QPainter.Antialiasing)
        self.view.setDragMode(QGraphicsView.ScrollHandDrag)
        self.view.setFrameShape(QGraphicsView.NoFrame)
        self.view.setBackgroundBrush(QColor(c("#16181d")))
        split.addWidget(self.view)
        side = QWidget()
        sl = QVBoxLayout(side)
        sl.setContentsMargins(6, 0, 0, 0)
        self.detail_title = QLabel("Interface")
        self.detail_title.setStyleSheet("font-weight: 600;")
        sl.addWidget(self.detail_title)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.detail.setStyleSheet("font-family: Consolas; font-size: 9pt;")
        self.detail.setPlaceholderText("Click an interface to see its config.")
        sl.addWidget(self.detail, 1)
        split.addWidget(side)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        split.setSizes([1000, 380])
        lay.addWidget(split, 1)

        self.nodes: list = []                  # NodeView, in display order
        self.data: dict[str, list] = {}        # node id -> [Iface]
        self.errors: dict[str, str] = {}
        self.links: dict[str, list[str]] = {}  # device id -> its interfaces on the BB
        self.pills: dict[tuple[str, str], IfacePill] = {}
        self._sel: tuple[str, str] | None = None
        self._fitted = False

    # --- data ---------------------------------------------------------------

    def set_nodes(self, nodes: list, links: dict[str, list[str]]) -> None:
        self.nodes = [n for n in nodes if n.kind in ("device", "backbone")]
        self.links = links
        self.redraw()

    def set_loading(self, node_id: str) -> None:
        self.errors[node_id] = "reading interfaces…"
        self.data.pop(node_id, None)

    def set_result(self, node_id: str, ifaces: list, error: str) -> None:
        self.data[node_id] = ifaces
        self.errors[node_id] = error
        if error and not ifaces:
            self.data.pop(node_id, None)
        self.redraw()

    def set_error(self, node_id: str, error: str) -> None:
        self.errors[node_id] = error
        self.redraw()

    def set_detail(self, node_id: str, name: str, text: str) -> None:
        if self._sel == (node_id, name):
            self.detail.setPlainText(self._relations(node_id, name) + text)

    # --- drawing ------------------------------------------------------------

    def _visible(self, node_id: str) -> list:
        ifaces = self.data.get(node_id, [])
        if self.hide_lo.isChecked():
            ifaces = [i for i in ifaces if i.kind != "loopback" and i.name != "lo"]
        if self.hide_down.isChecked():
            keep = {i.name for i in ifaces if i.state != "down"}
            by = {i.name: i for i in ifaces}
            for name in list(keep):            # keep the fathers of what is shown
                f = by[name].father
                while f and f in by and f not in keep:
                    keep.add(f)
                    f = by[f].father
            ifaces = [i for i in ifaces if i.name in keep]
        return ifaces

    @staticmethod
    def _tree(ifaces: list) -> list[tuple[object, int]]:
        """Depth-first order: (iface, depth), each child right under its father."""
        by = {i.name: i for i in ifaces}
        kids: dict[str, list] = {}
        roots = []
        for i in ifaces:
            f = i.father
            if f and f in by and f != i.name:
                kids.setdefault(f, []).append(i)
            else:
                roots.append(i)
        # a bridge first, then interfaces with children, then the rest
        order = lambda i: (i.kind != "bridge", i.name not in kids, i.name)
        out, seen = [], set()

        def walk(i, d):
            if i.name in seen:
                return
            seen.add(i.name)
            out.append((i, d))
            for k in sorted(kids.get(i.name, []), key=order):
                walk(k, d + 1)

        for r in sorted(roots, key=order):
            walk(r, 0)
        for i in ifaces:                       # a loop of fathers: show it anyway
            walk(i, 0)
        return out

    def _card(self, node, x: float, y: float) -> float:
        """Draw one node at (x, y); returns its height."""
        from .common import STATUS_COLOR
        rows = self._tree(self._visible(node.id))
        msg = "" if rows else (self.errors.get(node.id) or "not read yet – press ⟳ Refresh")
        h = HEAD_H + (len(rows) * ROW_H + CARD_PAD if rows else 40)
        card = Card(node.name, node.subtitle, STATUS_COLOR.get(node.state.status, MUTED), h, msg)
        card.setPos(x, y)
        self.scene.addItem(card)
        pos: dict[str, tuple[float, float, float]] = {}   # name -> left x, mid y, depth
        pen = QPen(QColor(c("#46506a")), 1.2)
        for r, (ia, depth) in enumerate(rows):
            depth = min(depth, 4)
            px = x + CARD_PAD + depth * INDENT
            py = y + HEAD_H + r * ROW_H
            pill = IfacePill(node.id, ia, PILL_W + (4 - depth) * INDENT)
            pill.setPos(px, py)
            pill.setZValue(2)
            pill.clicked.connect(self._clicked)
            pill.selected = self._sel == (node.id, ia.name)
            self.scene.addItem(pill)
            self.pills[(node.id, ia.name)] = pill
            pos[ia.name] = (px, py + (ROW_H - 6) / 2, depth)
            f = ia.father
            if f in pos:                       # elbow from the father down to this one
                fx, fy, _ = pos[f]
                path = QPainterPath()
                path.moveTo(fx + 10, fy + (ROW_H - 6) / 2)
                path.lineTo(fx + 10, py + (ROW_H - 6) / 2)
                path.lineTo(px, py + (ROW_H - 6) / 2)
                item = self.scene.addPath(path, pen)
                item.setZValue(1)
        return h

    def redraw(self) -> None:
        self.scene.clear()
        self.pills.clear()
        devices = [n for n in self.nodes if n.kind == "device"]
        bbs = {n.id: n for n in self.nodes if n.kind == "backbone"}
        y, bb_y, placed = 0.0, {}, set()
        dev_box: dict[str, tuple[float, float]] = {}
        for d in devices:
            h = self._card(d, 0, y)
            dev_box[d.id] = (y, h)
            bid = d.linked[0] if d.linked else None
            if bid in bbs and bid not in placed:
                bh = self._card(bbs[bid], CARD_W + COL_GAP, y)
                bb_y[bid] = (y, bh)
                placed.add(bid)
                h = max(h, bh)
            y += h + ROW_GAP
        for bid, bb in bbs.items():            # backbones without a device
            if bid not in placed:
                bh = self._card(bb, CARD_W + COL_GAP, y)
                bb_y[bid] = (y, bh)
                y += bh + ROW_GAP
        # device -> its interfaces on the BB
        for d in devices:
            bid = d.linked[0] if d.linked else None
            if bid not in bb_y:
                continue
            dy, dh = dev_box[d.id]
            start_y = dy + HEAD_H / 2
            names = self.links.get(d.id, [])
            targets = []
            for name in names:
                pill = self.pills.get((bid, name)) or next(
                    (p for (n, i), p in self.pills.items() if n == bid and i.lower() == name.lower()), None)
                if pill is not None:
                    pill.highlight = True
                    targets.append((pill.scenePos().y() + (ROW_H - 6) / 2, pill.ia.name, True))
                else:
                    targets.append((None, name, False))
            if not any(t[0] is not None for t in targets):
                targets = [(bb_y[bid][0] + HEAD_H / 2, " / ".join(names) or "backbone", False)]
            for ty, name, found in targets:
                ty = ty if ty is not None else bb_y[bid][0] + HEAD_H / 2
                x0, x1 = CARD_W, CARD_W + COL_GAP
                path = QPainterPath()
                path.moveTo(x0, start_y)
                path.cubicTo(x0 + COL_GAP / 2, start_y, x1 - COL_GAP / 2, ty, x1, ty)
                color = GREEN if found else MUTED
                pen = QPen(QColor(c(color)), 1.6, Qt.SolidLine if found else Qt.DashLine)
                self.scene.addPath(path, pen).setZValue(0)
                label = self.scene.addSimpleText(name, QFont("Consolas", 8))
                label.setBrush(QColor(c(color)))
                label.setPos(x1 - label.boundingRect().width() - 6, ty - 15)
        self.scene.setSceneRect(self.scene.itemsBoundingRect().adjusted(-20, -20, 20, 20))
        if not self._fitted and self.data:
            self._fitted = True
            self.fit()

    def fit(self) -> None:
        r = self.scene.itemsBoundingRect()
        if r.isValid():
            self.view.fitInView(r.adjusted(-20, -20, 20, 20), Qt.KeepAspectRatio)
            if self.view.transform().m11() > 1:
                self.view.resetTransform()

    # --- click --------------------------------------------------------------

    def _relations(self, node_id: str, name: str) -> str:
        ia = next((i for i in self.data.get(node_id, []) if i.name == name), None)
        if ia is None:
            return ""
        kids = [i.name for i in self.data.get(node_id, []) if i.father == name]
        lines = [f"type:      {ia.kind or 'ethernet'}", f"state:     {ia.state or '?'}"]
        if ia.father:
            lines.append(f"father:    {ia.father}")
        lines += [f"           {r}" for r in ia.relations()]
        if kids:
            lines.append(f"children:  {', '.join(kids)}")
        return "\n".join(lines) + "\n" + "─" * 60 + "\n"

    def _clicked(self, node_id: str, name: str) -> None:
        old = self.pills.get(self._sel) if self._sel else None
        if old is not None:
            old.selected = False
            old.update()
        self._sel = (node_id, name)
        pill = self.pills.get(self._sel)
        if pill is not None:
            pill.selected = True
            pill.update()
        node = next((n for n in self.nodes if n.id == node_id), None)
        self.detail_title.setText(f"{node.name if node else node_id}  ·  {name}")
        ia = pill.ia if pill else None
        self.detail.setPlainText(self._relations(node_id, name) + (ia.config if ia else "") + "\n\n(loading live config…)")
        self.iface_clicked.emit(node_id, name)
