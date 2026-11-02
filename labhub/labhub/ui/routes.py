"""
Routing table of one backbone: show, add (<dest> through iface <y>), and
remove the routes ticked in the table. One window per BB, kept open.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout)

from ..core import bbnet

COLS = ("", "#", "Destination", "Via", "Interface", "Metric", "Proto", "Route")


class RoutesDialog(QDialog):
    request = Signal(str, str, str, list)  # node id, family, action ("" / add / del), [args per route]

    def __init__(self, parent, node_id: str, title: str, ifaces: list[str]) -> None:
        super().__init__(parent)
        self.node_id = node_id
        self.routes: list[bbnet.Route] = []
        self.setWindowTitle(f"Routing table – {title}")
        self.resize(900, 480)
        lay = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Table"))
        self.family = QComboBox()
        self.family.addItem("IPv6", "6")
        self.family.addItem("IPv4", "4")
        self.family.currentIndexChanged.connect(lambda _i: self.refresh())
        top.addWidget(self.family)
        top.addStretch(1)
        ref = QPushButton("⟳ Refresh")
        ref.clicked.connect(self.refresh)
        top.addWidget(ref)
        lay.addLayout(top)

        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        h = self.table.horizontalHeader()
        for col in range(len(COLS) - 1):
            h.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(len(COLS) - 1, QHeaderView.Stretch)
        self.table.cellClicked.connect(self._toggle_row)
        self.table.itemChanged.connect(lambda _it: self._update_remove())
        lay.addWidget(self.table, 1)

        rm = QHBoxLayout()
        for text, on in (("Check all", True), ("Check none", False)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, on=on: self._check_all(on))
            rm.addWidget(b)
        rm.addStretch(1)
        self.remove_btn = QPushButton("✕ Remove checked")
        self.remove_btn.setObjectName("Danger")
        self.remove_btn.setEnabled(False)
        self.remove_btn.clicked.connect(self._remove)
        rm.addWidget(self.remove_btn)
        lay.addLayout(rm)

        add = QHBoxLayout()
        add.addWidget(QLabel("Add: to"))
        self.dest = QLineEdit()
        self.dest.setPlaceholderText("2001:db8::/64, 10.0.0.0/24 or default")
        add.addWidget(self.dest, 2)
        add.addWidget(QLabel("through iface"))
        self.iface = QComboBox()
        self.iface.setEditable(True)
        self.iface.addItems(ifaces)
        add.addWidget(self.iface, 1)
        add.addWidget(QLabel("via"))
        self.via = QLineEdit()
        self.via.setPlaceholderText("gateway (optional)")
        add.addWidget(self.via, 1)
        b = QPushButton("+ Add route")
        b.clicked.connect(self._add)
        add.addWidget(b)
        lay.addLayout(add)

        self.msg = QLabel()
        self.msg.setObjectName("Muted")
        self.msg.setWordWrap(True)
        self.msg.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.msg)

    def fam(self) -> str:
        return self.family.currentData()

    def set_ifaces(self, ifaces: list[str]) -> None:
        cur = self.iface.currentText()
        self.iface.clear()
        self.iface.addItems(ifaces)
        self.iface.setCurrentText(cur or (ifaces[0] if ifaces else ""))

    def refresh(self) -> None:
        self.msg.setText("loading…")
        self.request.emit(self.node_id, self.fam(), "", [])

    def set_routes(self, family: str, routes: list, error: str, message: str) -> None:
        if family != self.fam():
            return
        self.routes = routes
        self.table.blockSignals(True)
        self.table.setRowCount(len(routes))
        for r, rt in enumerate(routes):
            box = QTableWidgetItem()
            box.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            box.setCheckState(Qt.Unchecked)
            self.table.setItem(r, 0, box)
            dest = f"{rt.kind} {rt.dest}" if rt.kind else rt.dest
            for col, val in enumerate((str(r), dest, rt.via, rt.dev, rt.metric, rt.proto, rt.line), 1):
                it = QTableWidgetItem(val)
                if col == 1:
                    it.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(r, col, it)
        self.table.blockSignals(False)
        self._update_remove()
        text = message or ""
        if error:
            text = (text + "\n" if text else "") + f"✖ {error}"
        if not text:
            text = f"{len(routes)} route(s)"
        failed = bool(error) or "✖" in text
        color = "#ff6b7a" if failed else ("#3ccf7a" if message else "")
        self.msg.setStyleSheet(f"color: {color};" if color else "")
        self.msg.setText(text)

    # --- remove: tick the rows, then one button ----------------------------------

    def _checked(self) -> list[int]:
        return [r for r in range(self.table.rowCount())
                if self.table.item(r, 0) and self.table.item(r, 0).checkState() == Qt.Checked]

    def _toggle_row(self, row: int, col: int) -> None:
        """A click anywhere on the row ticks it (the box itself toggles by itself)."""
        it = self.table.item(row, 0)
        if it is not None and col != 0:
            it.setCheckState(Qt.Unchecked if it.checkState() == Qt.Checked else Qt.Checked)

    def _check_all(self, on: bool) -> None:
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            self.table.item(r, 0).setCheckState(Qt.Checked if on else Qt.Unchecked)
        self.table.blockSignals(False)
        self._update_remove()

    def _update_remove(self) -> None:
        n = len(self._checked())
        self.remove_btn.setEnabled(n > 0)
        self.remove_btn.setText(f"✕ Remove checked ({n})" if n else "✕ Remove checked")

    def _remove(self) -> None:
        rows = [r for r in self._checked() if r < len(self.routes)]
        if not rows:
            return
        listing = "\n".join(f"#{r}  {self.routes[r].line}" for r in rows)
        if QMessageBox.question(self, "Remove routes", f"Remove {len(rows)} route(s)?\n\n{listing}") != QMessageBox.Yes:
            return
        self.msg.setText("removing…")
        self.request.emit(self.node_id, self.fam(), "del", [self.routes[r].del_args() for r in rows])

    def _add(self) -> None:
        try:
            args = bbnet.add_args(self.dest.text(), self.iface.currentText(), self.via.text())
        except ValueError as e:
            QMessageBox.warning(self, "Add route", str(e))
            return
        self.msg.setText("adding…")
        self.request.emit(self.node_id, self.fam(), "add", [args])
