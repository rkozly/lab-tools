"""'Replace File' tab: push a local file over a device file and compare key options."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from devreader.config.replace import REPLACEABLE_PATHS, SUMMARY_KEYS, summarize
from devreader.core.discovery import Device
from devreader.gui.theme import COLORS
from devreader.gui.widgets import Badge, button

COLUMNS = ["Device", "Serial", *(f"option {k}" for k in SUMMARY_KEYS), "Status"]


@dataclass
class _Row:
    name: str
    serial: str
    values: dict[str, list[str]] | None = None  # None = not read yet
    status: str = ""
    ok: bool | None = None
    local: bool = False


class FileReplacePanel(QWidget):
    """Pick a device path + local file, compare key options per device, replace."""

    read_requested = Signal(str, object)              # remote path, [Device]
    replace_requested = Signal(str, str, object)      # remote path, local path, [Device]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._devices: list[Device] = []
        self._selected: list[Device] = []
        self._rows: dict[str, _Row] = {}  # serial -> row (device rows only)
        self._local: _Row | None = None
        self._busy = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(10)

        row = QHBoxLayout()
        lbl = QLabel("Device file")
        lbl.setObjectName("Muted")
        lbl.setMinimumWidth(70)
        row.addWidget(lbl)
        self.path_combo = QComboBox()
        self.path_combo.setEditable(True)
        self.path_combo.addItems(REPLACEABLE_PATHS)
        self.path_combo.setToolTip("Path of the file on the device to replace (pick or type)")
        self.path_combo.currentTextChanged.connect(self._on_path_changed)
        row.addWidget(self.path_combo, 1)
        lay.addLayout(row)

        row = QHBoxLayout()
        lbl = QLabel("Local file")
        lbl.setObjectName("Muted")
        lbl.setMinimumWidth(70)
        row.addWidget(lbl)
        self.local_edit = QLineEdit()
        self.local_edit.setPlaceholderText("Choose the file to push…")
        self.local_edit.textChanged.connect(self._on_local_changed)
        row.addWidget(self.local_edit, 1)
        self.browse_btn = button("Browse…", tooltip="Choose the local file to push")
        self.browse_btn.clicked.connect(self._browse)
        row.addWidget(self.browse_btn)
        lay.addLayout(row)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(len(COLUMNS) - 1, QHeaderView.Stretch)
        lay.addWidget(self.table, 1)

        hint = QLabel("Values that differ from the local file are highlighted. "
                      "The device file is backed up before it is replaced.")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        actions = QHBoxLayout()
        self.status_badge = Badge("Not loaded", COLORS["muted"])
        actions.addWidget(self.status_badge)
        actions.addStretch(1)
        self.read_btn = button("⭳  Read Values", tooltip="Read the file from every connected device")
        self.replace_btn = button("⭱  Replace on Selected", primary=True,
                                  tooltip="Back up, replace and verify the file on every checked device")
        actions.addWidget(self.read_btn)
        actions.addWidget(self.replace_btn)
        lay.addLayout(actions)

        self.read_btn.clicked.connect(self._emit_read)
        self.replace_btn.clicked.connect(self._emit_replace)
        self._update_state()

    # -- inputs ----------------------------------------------------------------
    def remote_path(self) -> str:
        return self.path_combo.currentText().strip()

    def local_path(self) -> str:
        return self.local_edit.text().strip()

    def _browse(self) -> None:
        start = str(Path(self.local_path()).parent) if self.local_path() else ""
        path, _ = QFileDialog.getOpenFileName(self, "Choose file to push", start, "All files (*)")
        if path:
            self.local_edit.setText(path)

    def _on_path_changed(self, *_):
        for r in self._rows.values():
            r.values, r.status, r.ok = None, "", None
        self.status_badge.set("Not loaded", COLORS["muted"])
        self._render()

    def _on_local_changed(self, *_):
        path = Path(self.local_path()) if self.local_path() else None
        if path is None or not path.is_file():
            self._local = None
        else:
            self._local = _Row("Local file", path.name, local=True)
            try:
                data = path.read_bytes()
                self._local.values = summarize(data)
                self._local.status = f"{len(data)} bytes • {path}"
            except (OSError, UnicodeError) as exc:
                self._local.status, self._local.ok = f"Cannot read: {exc}", False
        self._render()

    # -- devices -----------------------------------------------------------------
    def set_devices(self, devices: list[Device], selected: list[Device]) -> None:
        self._devices = [d for d in devices if d.configured or d.is_ready]
        self._selected = [d for d in selected if d.is_ready]
        old = self._rows
        self._rows = {}
        for d in self._devices:
            r = old.get(d.serial) or _Row(d.name, d.serial)
            r.name = d.name
            if not d.is_ready:
                r.status, r.ok = d.status.value, None
            elif r.status in {s.value for s in type(d.status)}:
                r.status = ""  # reconnected
            self._rows[d.serial] = r
        self._render()

    def ready_devices(self) -> list[Device]:
        return [d for d in self._devices if d.is_ready]

    # -- results -----------------------------------------------------------------
    def set_values(self, serial: str, values: dict[str, list[str]] | None, status: str,
                   ok: bool | None = True) -> None:
        r = self._rows.get(serial)
        if r is None:
            return
        if values is not None:
            r.values = values
        r.status, r.ok = status, ok
        self._render()

    def set_summary(self, text: str, color_key: str) -> None:
        self.status_badge.set(text, COLORS[color_key])

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_state()

    # -- rendering -----------------------------------------------------------------
    def _render(self) -> None:
        rows = ([self._local] if self._local else []) + list(self._rows.values())
        local_vals = self._local.values if self._local else None
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(r.name))
            self.table.setItem(i, 1, QTableWidgetItem(r.serial))
            for c, key in enumerate(SUMMARY_KEYS, start=2):
                vals = None if r.values is None else r.values.get(key, [])
                item = QTableWidgetItem("" if vals is None else ("\n".join(vals) or "N/A"))
                if vals is not None and not vals:
                    item.setForeground(QColor(COLORS["muted"]))
                elif vals is not None and not r.local and local_vals is not None and local_vals.get(key) != vals:
                    item.setForeground(QColor(COLORS["warning"]))
                    item.setToolTip("Local file: " + (", ".join(local_vals.get(key, [])) or "N/A"))
                self.table.setItem(i, c, item)
            status = QTableWidgetItem(r.status)
            color = COLORS["muted"] if r.ok is None else (COLORS["success"] if r.ok else COLORS["error"])
            status.setForeground(QColor(color))
            status.setToolTip(r.status)
            self.table.setItem(i, len(COLUMNS) - 1, status)
            if r.local:
                for c in range(len(COLUMNS)):
                    f = self.table.item(i, c).font()
                    f.setBold(True)
                    self.table.item(i, c).setFont(f)
        self.table.resizeRowsToContents()
        self._update_state()

    # -- actions -------------------------------------------------------------------
    def _emit_read(self) -> None:
        self.read_requested.emit(self.remote_path(), self.ready_devices())

    def _emit_replace(self) -> None:
        self.replace_requested.emit(self.remote_path(), self.local_path(), list(self._selected))

    def _update_state(self, *_):
        has_path = bool(self.remote_path())
        has_local = self._local is not None and self._local.ok is not False
        self.read_btn.setEnabled(not self._busy and has_path and bool(self.ready_devices()))
        self.replace_btn.setEnabled(not self._busy and has_path and has_local and bool(self._selected))
        self.replace_btn.setText(f"⭱  Replace on Selected ({len(self._selected)})")
