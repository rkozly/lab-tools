"""
Device strip: one tile per device and backbone. A device and its backbone
sit together as a pair. Click = select one, Ctrl+click = toggle,
Shift+click = range.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from ..core.models import DeviceConfig, NodeView
from .common import STATUS_COLOR, FlowLayout, repolish


class Tile(QFrame):
    clicked = Signal(str, object)  # node id, Qt.KeyboardModifiers

    def __init__(self, node: NodeView) -> None:
        super().__init__()
        self.setObjectName("Tile")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedWidth(128)
        self.node_id = node.id
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(1)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.dot = QLabel("●")
        self.dot.setStyleSheet("background: transparent;")
        self.name = QLabel()
        self.name.setObjectName("TileName")
        top.addWidget(self.dot)
        top.addWidget(self.name, 1)
        lay.addLayout(top)
        self.sub = QLabel()
        self.sub.setObjectName("TileSub")
        self.badge = QLabel()
        self.badge.setObjectName("TileBadge")
        lay.addWidget(self.sub)
        lay.addWidget(self.badge)
        self.update_node(node)

    def update_node(self, node: NodeView) -> None:
        self.name.setText(node.name)
        self.dot.setStyleSheet(f"background: transparent; color: {STATUS_COLOR[node.state.status]};")
        sub = node.subtitle
        self.sub.setText(sub if len(sub) <= 18 else sub[:17] + "…")
        if isinstance(node.config, DeviceConfig):
            role = node.config.role.upper()
            model = node.state.info.get("model", "")
            self.badge.setText(f"{role} · {node.state.status.value}" + (f" · {model}" if model else ""))
        elif node.kind == "backbone":
            n = len(node.linked)
            self.badge.setText(f"BB · {node.state.status.value}" + (f" · {n} dev" if n > 1 else ""))
        else:
            self.badge.setText(f"unassigned · {node.state.status.value}")
        tip = [f"<b>{node.name}</b>", f"Status: {node.state.status.value}"]
        if node.serial:
            tip.append(f"Serial: {node.serial}")
        if node.state.since:
            tip.append(f"Since: {node.state.since:%H:%M:%S}")
        for k in ("model", "product", "usb"):
            if node.state.info.get(k):
                tip.append(f"{k}: {node.state.info[k]}")
        if isinstance(node.config, DeviceConfig) and node.config.backbone:
            b = node.config.backbone
            tip.append(f"Backbone: {b.backbone_id}" + (f" via {', '.join(b.interfaces)}" if b.interfaces else ""))
        if node.kind == "backbone" and node.linked:
            tip.append(f"Serves: {', '.join(node.linked)}")
        if node.state.error:
            tip.append(f"<i>{node.state.error}</i>")
        self.setToolTip("<br>".join(tip))

    def set_selected(self, on: bool) -> None:
        self.setProperty("selected", "true" if on else "false")
        repolish(self)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self.node_id, e.modifiers())
        super().mousePressEvent(e)


class DeviceStrip(QWidget):
    selection_changed = Signal(list)
    assign_requested = Signal(str)  # serial

    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 8, 12, 8)
        outer.setSpacing(6)
        head = QHBoxLayout()
        title = QLabel("DEVICES")
        title.setObjectName("Section")
        head.addWidget(title)
        head.addStretch(1)
        self.summary = QLabel()
        self.summary.setObjectName("Muted")
        head.addWidget(self.summary)
        for text, sel in (("All", "all"), ("APs", "ap"), ("CPEs", "cpe"), ("BBs", "backbone"), ("None", "none")):
            b = QPushButton(text)
            b.setObjectName("Chip")
            b.clicked.connect(lambda _=False, s=sel: self.quick_select(s))
            head.addWidget(b)
        outer.addLayout(head)

        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.area.setFrameShape(QFrame.NoFrame)
        self.inner = QWidget()
        self.flow = FlowLayout(self.inner, spacing=10)
        self.area.setWidget(self.inner)
        outer.addWidget(self.area)

        self.tiles: dict[str, Tile] = {}
        self.order: list[str] = []
        self.nodes: dict[str, NodeView] = {}
        self.selected: list[str] = []
        self._anchor: str | None = None
        self._signature = None

    # --- build --------------------------------------------------------------

    def set_nodes(self, nodes: list[NodeView]) -> None:
        self.nodes = {n.id: n for n in nodes}
        signature = [(n.id, n.kind, tuple(n.linked)) for n in nodes]
        if signature != self._signature:
            self._signature = signature
            self._rebuild(nodes)
        else:
            for n in nodes:
                self.tiles[n.id].update_node(n)
        before = list(self.selected)
        self.selected = [i for i in self.selected if i in self.nodes]
        self._paint()
        if before != self.selected:
            self.selection_changed.emit(list(self.selected))
        conn = sum(1 for n in nodes if n.kind == "device" and n.state.status.value == "connected")
        devs = sum(1 for n in nodes if n.kind == "device")
        un = sum(1 for n in nodes if n.kind == "unassigned")
        self.summary.setText(f"{conn}/{devs} devices connected" + (f" · {un} unassigned" if un else "") + "   ")

    def _rebuild(self, nodes: list[NodeView]) -> None:
        while self.flow.count():
            w = self.flow.takeAt(0).widget()
            if w:
                w.deleteLater()
        self.tiles.clear()
        self.order = [n.id for n in nodes]
        by_id = {n.id: n for n in nodes}
        placed: set[str] = set()
        for n in nodes:
            if n.id in placed:
                continue
            group = [n]
            # pair a device with its backbone when the backbone follows it directly
            if n.kind == "device" and n.linked:
                bb = by_id.get(n.linked[0])
                if bb and bb.id not in placed and nodes.index(bb) == nodes.index(n) + 1:
                    group.append(bb)
            frame = QFrame()
            frame.setObjectName("Pair")
            lay = QHBoxLayout(frame)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(3)
            for g in group:
                t = Tile(g)
                t.clicked.connect(self._on_click)
                self.tiles[g.id] = t
                lay.addWidget(t)
                placed.add(g.id)
            if n.kind == "unassigned":
                b = QPushButton("Assign…")
                b.setObjectName("Chip")
                b.clicked.connect(lambda _=False, s=n.serial: self.assign_requested.emit(s))
                lay.addWidget(b)
            self.flow.addWidget(frame)
        self._fit()

    def _fit(self) -> None:
        """Make the strip exactly as tall as its tiles (up to 3 rows, then scroll)."""
        w = max(200, self.area.viewport().width())
        h = self.flow.heightForWidth(w)
        self.area.setFixedHeight(min(h, 3 * 68 + 20) + 4)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._fit()

    # --- selection ----------------------------------------------------------

    def _on_click(self, node_id: str, mods) -> None:
        if mods & Qt.ShiftModifier and self._anchor in self.order:
            a, b = self.order.index(self._anchor), self.order.index(node_id)
            lo, hi = min(a, b), max(a, b)
            self.selected = self.order[lo:hi + 1]
        elif mods & Qt.ControlModifier:
            if node_id in self.selected:
                self.selected.remove(node_id)
            else:
                self.selected.append(node_id)
            self._anchor = node_id
        else:
            self.selected = [] if self.selected == [node_id] else [node_id]
            self._anchor = node_id
        self._commit()

    def quick_select(self, what: str) -> None:
        if what == "none":
            self.selected = []
        elif what == "all":
            self.selected = [i for i in self.order if self.nodes[i].kind != "unassigned"]
        elif what == "backbone":
            self.selected = [i for i in self.order if self.nodes[i].kind == "backbone"]
        else:
            self.selected = [i for i in self.order if isinstance(self.nodes[i].config, DeviceConfig)
                             and self.nodes[i].config.role == what]
        self._commit()

    def set_selection(self, ids: list[str]) -> None:
        self.selected = [i for i in self.order if i in ids]
        self._commit()

    def _commit(self) -> None:
        self.selected = [i for i in self.order if i in self.selected]
        self._paint()
        self.selection_changed.emit(list(self.selected))

    def _paint(self) -> None:
        for i, t in self.tiles.items():
            t.set_selected(i in self.selected)
