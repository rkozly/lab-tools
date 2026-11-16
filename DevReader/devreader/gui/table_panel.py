"""Editable per-device table for one configuration file (one row per device)."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemDelegate,
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from devreader.config.parameters import ParameterDef
from devreader.config.table import AP_NAME, Cell, Column, TableDef, TableRead, merge_columns
from devreader.core.discovery import Device
from devreader.core.errors import ConfigValidationError
from devreader.gui.theme import COLORS
from devreader.gui.widgets import Badge, button

FIXED = ["Device", "Serial"]


class ChoiceDelegate(QStyledItemDelegate):
    """Drop-down editor for cells whose parameter has fixed choices (e.g. 0 / 1); text editor otherwise."""

    def __init__(self, panel: "ConfigTablePanel") -> None:
        super().__init__(panel.table)
        self._panel = panel

    def createEditor(self, parent, option, index):
        param = self._panel.param_at(index.row(), index.column())
        if param is None or not param.choices:
            return super().createEditor(parent, option, index)
        box = QComboBox(parent)
        for text, value in param.choice_items():
            box.addItem(text, value)
        box.activated.connect(lambda _i, b=box: self._commit(b))
        QTimer.singleShot(0, box, box.showPopup)  # open the list right away: one double-click to pick
        return box

    def setEditorData(self, editor, index):
        if not isinstance(editor, QComboBox):
            return super().setEditorData(editor, index)
        value = (index.data(Qt.EditRole) or "").strip()
        i = editor.findData(value)
        if i < 0 and value:  # unexpected device value: keep it selectable so "no change" is possible
            editor.addItem(f"{value} (device value)", value)
            i = editor.count() - 1
        editor.setCurrentIndex(i)

    def setModelData(self, editor, model, index):
        if not isinstance(editor, QComboBox):
            return super().setModelData(editor, model, index)
        if editor.currentIndex() >= 0:
            model.setData(index, editor.currentData(), Qt.EditRole)

    def _commit(self, box: QComboBox) -> None:
        self.commitData.emit(box)
        self.closeEditor.emit(box, QAbstractItemDelegate.NoHint)


class ConfigTablePanel(QWidget):
    read_requested = Signal(object, object)    # TableDef, [Device]
    write_requested = Signal(object, object)   # TableDef, {serial: {ParameterDef: value}}
    flash_requested = Signal(object)           # TableDef

    def __init__(self, table: TableDef, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.table_def = table
        self._devices: list[Device] = []
        self._cells: dict[str, dict[str, Cell]] = {}   # serial -> column id -> cell read from device
        self._status: dict[str, tuple[str, bool | None]] = {}
        self._busy = False
        self._rendering = False
        # Fixed tables: table.columns. Discovered tables: union of the columns found per device.
        self._columns: list[Column] = list(table.columns)
        self._discovered: dict[str, tuple[Column, ...]] = {}  # serial -> columns in that device's file
        self._col_ids: list[str] = []
        self._status_col = 0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(10)

        self.table = QTableWidget(0, 0)
        self.table.setItemDelegate(ChoiceDelegate(self))
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
                                   | QAbstractItemView.AnyKeyPressed)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.horizontalHeader().setHighlightSections(False)
        self._apply_columns()
        self.table.itemChanged.connect(self._on_item_changed)
        lay.addWidget(self.table, 1)

        if table.discover is not None:
            key = table.discover.key
            what = f"Columns = 'uci show {table.config_name} | grep {key}', filled by Read All. "
            edit = "Double-click a cell to pick a value. " if table.discover.choices else ""
        else:
            what, edit = "", "Double-click a cell to edit. "
        hint = QLabel(f"{what}{edit}Edited cells are highlighted; only they are written. "
                      "Empty cells do not exist in that device's file.")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        actions = QHBoxLayout()
        self.status_badge = Badge("Not loaded", COLORS["muted"])
        actions.addWidget(self.status_badge)
        actions.addStretch(1)
        self.revert_btn = button("Revert", tooltip="Discard all edits")
        self.read_btn = button("⭳  Read All", tooltip=f"Read {table.remote_path} from every connected device")
        self.write_btn = button("⭱  Write Changes", primary=True,
                                tooltip="Write the edited values (backup + verify on each device)")
        self.write_btn.setVisible(not table.read_only)
        self.revert_btn.setVisible(not table.read_only)
        self.flash_btn = button("⚡  Flash wlan_wkk…",
                                tooltip="Push WLAN firmware from a build folder to the devices ticked in the "
                                        "Devices list, then reboot them")
        self.flash_btn.setVisible(table.flash_wlan)
        self.flash_btn.clicked.connect(lambda: self.flash_requested.emit(self.table_def))
        actions.addWidget(self.flash_btn)
        actions.addWidget(self.revert_btn)
        actions.addWidget(self.read_btn)
        actions.addWidget(self.write_btn)
        lay.addLayout(actions)

        self.read_btn.clicked.connect(lambda: self.read_requested.emit(self.table_def, self.ready_devices()))
        self.write_btn.clicked.connect(self._emit_write)
        self.revert_btn.clicked.connect(self._render)
        self._update_state()

    # -- devices -----------------------------------------------------------------
    def set_devices(self, devices: list[Device], selected: list[Device] | None = None) -> None:
        edits = self._current_edits_text()
        self._devices = [d for d in devices if d.configured or d.is_ready]
        self._render(keep=edits)

    def ready_devices(self) -> list[Device]:
        return [d for d in self._devices if d.is_ready]

    # -- results -----------------------------------------------------------------
    def set_read(self, result: TableRead) -> None:
        edits = self._current_edits_text()
        edits.pop(result.serial, None)
        self._cells[result.serial] = result.cells
        self._status[result.serial] = ("Read OK", True)
        if self.table_def.discover is not None:
            self._discovered[result.serial] = result.columns
            self._update_discovered_columns()
            if not result.cells:
                self._status[result.serial] = (f"No 'option {self.table_def.discover.key}' in the file", None)
        self._render(keep=edits)

    def set_row_status(self, serial: str, text: str, ok: bool | None) -> None:
        self._status[serial] = (text, ok)
        row = self._row_of(serial)
        if row is not None:
            self._set_status_item(row, serial)

    def set_summary(self, text: str, color_key: str) -> None:
        self.status_badge.set(text, COLORS[color_key])

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_state()

    def collect(self) -> dict[str, dict[ParameterDef, str]]:
        """Validated edits per device. Raises ConfigValidationError listing every bad cell."""
        out: dict[str, dict[ParameterDef, str]] = {}
        errors = []
        for serial, edits in self._current_edits_text().items():
            name = self._name(serial)
            for col_id, text in edits.items():
                cell = self._cells[serial][col_id]
                try:
                    out.setdefault(serial, {})[cell.param] = cell.param.validate(text)
                except ConfigValidationError as exc:
                    errors.append(f"{name}: {exc}")
        if errors:
            raise ConfigValidationError("\n".join(errors))
        return out

    def describe(self, edits: dict[str, dict[ParameterDef, str]]) -> str:
        lines = []
        for serial, values in edits.items():
            for p, v in values.items():
                old = next((c.value for c in self._cells[serial].values() if c.param == p), None)
                lines.append(f"  {self._name(serial)}: {p.label}  {old} → {v}")
        return "\n".join(lines)

    # -- rendering -----------------------------------------------------------------
    def _render(self, *_args, keep: dict[str, dict[str, str]] | None = None) -> None:
        keep = keep or {}
        self._rendering = True
        self.table.setRowCount(len(self._devices))
        for row, dev in enumerate(self._devices):
            for c, text in enumerate((dev.name, dev.serial)):
                item = QTableWidgetItem(text)
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                item.setData(Qt.UserRole, dev.serial)
                if dev.name == AP_NAME:
                    f = item.font()
                    f.setBold(True)
                    item.setFont(f)
                self.table.setItem(row, c, item)
            cells = self._cells.get(dev.serial)
            for i, col in enumerate(self._columns, start=len(FIXED)):
                cell = cells.get(col.id) if cells else None
                editable = (cell is not None and cell.param is not None and not cell.param.read_only
                            and not self.table_def.read_only)
                text = keep.get(dev.serial, {}).get(col.id, cell.value if cell and cell.param else "")
                item = QTableWidgetItem(text or "")
                flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
                if editable:
                    flags |= Qt.ItemIsEditable
                    item.setToolTip(cell.info or f"{cell.param.section}.{cell.param.key}")
                elif cells is not None and (cell is None or cell.param is None):
                    item.setToolTip("Not present on this device")
                item.setFlags(flags)
                self.table.setItem(row, i, item)
                self._style_cell(item, dev.serial, col.id)
            if not dev.is_ready:
                self._status[dev.serial] = (dev.status.value, None)
            elif self._status.get(dev.serial, ("", None))[0] in ("Disconnected", "Unauthorized", "Offline", "Other"):
                self._status.pop(dev.serial)
            self._set_status_item(row, dev.serial)
        self._rendering = False
        self._update_state()

    def _apply_columns(self) -> None:
        """Rebuild the header from ``self._columns``. Cells are filled by the next ``_render``."""
        self._col_ids = [c.id for c in self._columns]
        self._status_col = len(FIXED) + len(self._columns)
        self.table.setColumnCount(self._status_col + 1)
        self.table.setHorizontalHeaderLabels(FIXED + [c.label for c in self._columns] + ["Status"])
        for i, c in enumerate(self._columns, start=len(FIXED)):
            self.table.horizontalHeaderItem(i).setToolTip(c.tooltip)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(self._status_col, QHeaderView.Stretch)

    def _update_discovered_columns(self) -> None:
        # Listed devices first (AP on top), so the column order does not depend on which read finished first.
        order = [d.serial for d in self._devices if d.serial in self._discovered]
        order += [s for s in self._discovered if s not in order]
        columns = merge_columns(*(self._discovered[s] for s in order))
        if [c.id for c in columns] != self._col_ids:
            self._columns = columns
            self._apply_columns()

    def _set_status_item(self, row: int, serial: str) -> None:
        text, ok = self._status.get(serial, ("", None))
        item = QTableWidgetItem(text)
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        item.setToolTip(text)
        item.setForeground(QColor(COLORS["muted"] if ok is None else COLORS["success"] if ok else COLORS["error"]))
        was = self._rendering
        self._rendering = True
        self.table.setItem(row, self._status_col, item)
        self._rendering = was

    def _style_cell(self, item: QTableWidgetItem, serial: str, col_id: str) -> None:
        cell = self._cells.get(serial, {}).get(col_id)
        bold = QFont(item.font())
        if cell is None or cell.param is None:
            item.setBackground(QColor(0, 0, 0, 0))
            return
        modified = item.text().strip() != (cell.value or "")
        invalid = False
        if modified:
            try:
                cell.param.validate(item.text())
            except ConfigValidationError as exc:
                invalid = True
                item.setToolTip(f"⛔ {exc}")
            else:
                item.setToolTip(f"✎ device value: {cell.value}")
        item.setBackground(QColor("#4a1d1f") if invalid else QColor("#4a3b12") if modified else QColor(0, 0, 0, 0))
        bold.setBold(modified)
        item.setFont(bold)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._rendering or item.column() < len(FIXED) or item.column() >= self._status_col:
            return
        serial = self.table.item(item.row(), 0).data(Qt.UserRole)
        self._rendering = True
        self._style_cell(item, serial, self._col_ids[item.column() - len(FIXED)])
        self._rendering = False
        self._update_state()

    # -- helpers -------------------------------------------------------------------
    def _current_edits_text(self) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        for row in range(self.table.rowCount()):
            head = self.table.item(row, 0)
            if head is None:
                continue
            serial = head.data(Qt.UserRole)
            cells = self._cells.get(serial, {})
            for i, col_id in enumerate(self._col_ids, start=len(FIXED)):
                cell, item = cells.get(col_id), self.table.item(row, i)
                if cell and cell.param and item and item.text().strip() != (cell.value or ""):
                    out.setdefault(serial, {})[col_id] = item.text().strip()
        return out

    def param_at(self, row: int, column: int) -> ParameterDef | None:
        """Parameter shown in a value cell, or None (fixed/status column, or not on that device)."""
        head = self.table.item(row, 0)
        if head is None or not len(FIXED) <= column < self._status_col:
            return None
        cell = self._cells.get(head.data(Qt.UserRole), {}).get(self._col_ids[column - len(FIXED)])
        return cell.param if cell else None

    def _row_of(self, serial: str) -> int | None:
        for row in range(self.table.rowCount()):
            head = self.table.item(row, 0)
            if head is not None and head.data(Qt.UserRole) == serial:
                return row
        return None

    def _name(self, serial: str) -> str:
        return next((d.name for d in self._devices if d.serial == serial), serial)

    def _emit_write(self) -> None:
        try:
            edits = self.collect()
        except ConfigValidationError as exc:
            self.write_requested.emit(self.table_def, exc)
            return
        self.write_requested.emit(self.table_def, edits)

    def _update_state(self, *_):
        n = len(self._current_edits_text()) if not self._rendering else 0
        self.read_btn.setEnabled(not self._busy and bool(self.ready_devices()))
        self.write_btn.setEnabled(not self._busy and n > 0)
        self.revert_btn.setEnabled(not self._busy and n > 0)
        self.write_btn.setText(f"⭱  Write Changes ({n} device{'s' if n != 1 else ''})" if n else "⭱  Write Changes")
