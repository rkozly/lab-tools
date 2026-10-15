"""Device list: model, filter proxy and panel widget."""

from __future__ import annotations

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QScrollArea,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from devreader.core.discovery import ConnectionStatus, Device
from devreader.gui.theme import COLORS
from devreader.gui.widgets import Card, button

STATUS_DISPLAY = {
    ConnectionStatus.CONNECTED: ("✅", "Connected", COLORS["success"]),
    ConnectionStatus.DISCONNECTED: ("❌", "Disconnected", COLORS["error"]),
    ConnectionStatus.UNAUTHORIZED: ("⚠", "Unauthorized", COLORS["warning"]),
    ConnectionStatus.OFFLINE: ("⚠", "Offline", COLORS["warning"]),
    ConnectionStatus.OTHER: ("•", "Other", COLORS["muted"]),
}

COL_SELECT, COL_NAME, COL_SERIAL, COL_STATUS, COL_PROGRESS, COL_ACTION = range(6)
TILE_WIDTH = 160
HEADERS = ["", "Name", "Serial", "Connection", "Progress", "Last Action"]


class DeviceTableModel(QAbstractTableModel):
    selection_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._devices: list[Device] = []
        self._checked: set[str] = set()
        self._progress: dict[str, tuple[int, int, str]] = {}
        self._busy: set[str] = set()

    # -- data management ---------------------------------------------------
    def set_devices(self, devices: list[Device]) -> None:
        previous = {d.serial: d for d in self._devices}
        first_load = not self._devices
        self.beginResetModel()
        for d in devices:  # keep last action across refreshes
            old = previous.get(d.serial)
            if old and not d.last_action:
                d.last_action, d.last_action_ok = old.last_action, old.last_action_ok
        self._devices = devices
        serials = {d.serial for d in devices}
        self._checked &= serials
        if first_load:  # pre-select every connected, mapped device
            self._checked |= {d.serial for d in devices if d.is_ready and d.configured}
        self.endResetModel()
        self.selection_changed.emit()

    def devices(self) -> list[Device]:
        return list(self._devices)

    def device(self, serial: str) -> Device | None:
        return next((d for d in self._devices if d.serial == serial), None)

    def checked_devices(self) -> list[Device]:
        return [d for d in self._devices if d.serial in self._checked]

    def set_checked(self, serials: set[str]) -> None:
        self._checked = serials & {d.serial for d in self._devices}
        self._emit_column(COL_SELECT)
        self.selection_changed.emit()

    def set_last_action(self, serial: str, text: str, ok: bool | None) -> None:
        dev = self.device(serial)
        if dev:
            dev.last_action, dev.last_action_ok = text, ok
            self._emit_row(serial)

    def set_progress(self, serial: str, value: int, maximum: int, text: str) -> None:
        self._progress[serial] = (value, maximum, text)
        self._emit_row(serial)

    def clear_progress(self, serial: str) -> None:
        self._progress.pop(serial, None)
        self._emit_row(serial)

    def progress(self, serial: str) -> tuple[int, int, str] | None:
        return self._progress.get(serial)

    def set_busy(self, serial: str, busy: bool) -> None:
        (self._busy.add if busy else self._busy.discard)(serial)
        self._emit_row(serial)

    def is_busy(self, serial: str) -> bool:
        return serial in self._busy

    def _row(self, serial: str) -> int:
        return next((i for i, d in enumerate(self._devices) if d.serial == serial), -1)

    def _emit_row(self, serial: str) -> None:
        row = self._row(serial)
        if row >= 0:
            self.dataChanged.emit(self.index(row, 0), self.index(row, len(HEADERS) - 1))

    def _emit_column(self, col: int) -> None:
        if self._devices:
            self.dataChanged.emit(self.index(0, col), self.index(len(self._devices) - 1, col))

    # -- Qt model API --------------------------------------------------------
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._devices)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return HEADERS[section]
        return None

    def flags(self, index):
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if index.column() == COL_SELECT:
            base |= Qt.ItemIsUserCheckable
        return base

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        dev = self._devices[index.row()]
        col = index.column()
        if role == Qt.UserRole:
            return dev
        if col == COL_SELECT and role == Qt.CheckStateRole:
            return Qt.Checked if dev.serial in self._checked else Qt.Unchecked
        if col == COL_NAME:
            if role == Qt.DisplayRole:
                return dev.name
            if role == Qt.ForegroundRole and not dev.configured:
                return QColor(COLORS["muted"])
            if role == Qt.ToolTipRole:
                return dev.env_var or "Connected to adb but not mapped by any adb_device_id_* variable"
        if col == COL_SERIAL and role == Qt.DisplayRole:
            return dev.serial
        if col == COL_STATUS:
            icon, text, color = STATUS_DISPLAY[dev.status]
            if role == Qt.DisplayRole:
                return f"{icon}  {text}" if dev.status != ConnectionStatus.OTHER else f"{icon}  {dev.state.value}"
            if role == Qt.ForegroundRole:
                return QColor(color)
            if role == Qt.ToolTipRole:
                return f"adb state: {dev.state.value}"
        if col == COL_PROGRESS and role == Qt.DisplayRole:
            prog = self._progress.get(dev.serial)
            if prog:
                return prog[2]
            return "Working…" if dev.serial in self._busy else ""
        if col == COL_ACTION:
            if role == Qt.DisplayRole:
                return dev.last_action
            if role == Qt.ToolTipRole:
                return dev.last_action
            if role == Qt.ForegroundRole and dev.last_action_ok is not None:
                return QColor(COLORS["success"] if dev.last_action_ok else COLORS["error"])
        return None

    def setData(self, index, value, role=Qt.EditRole) -> bool:
        if index.column() == COL_SELECT and role == Qt.CheckStateRole:
            serial = self._devices[index.row()].serial
            if Qt.CheckState(value) == Qt.Checked:
                self._checked.add(serial)
            else:
                self._checked.discard(serial)
            self.dataChanged.emit(index, index)
            self.selection_changed.emit()
            return True
        return False


class DeviceFilterProxy(QSortFilterProxyModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._text = ""
        self._connected_only = False

    def set_text(self, text: str) -> None:
        self._text = text.strip().lower()
        self.invalidateFilter()

    def set_connected_only(self, value: bool) -> None:
        self._connected_only = value
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent) -> bool:
        dev: Device = self.sourceModel().index(row, 0, parent).data(Qt.UserRole)
        if dev is None:
            return True
        if self._connected_only and not dev.is_ready:
            return False
        if not self._text:
            return True
        hay = f"{dev.name} {dev.serial} {dev.status.value} {dev.env_var} {dev.last_action}".lower()
        return all(tok in hay for tok in self._text.split())


class ProgressDelegate(QStyledItemDelegate):
    """Draws an inline progress bar in the Progress column."""

    def __init__(self, model: DeviceTableModel, parent=None) -> None:
        super().__init__(parent)
        self._model = model
        self._bar = QProgressBar()

    def paint(self, painter, option, index) -> None:
        dev: Device = index.data(Qt.UserRole)
        prog = self._model.progress(dev.serial) if dev else None
        if not prog:
            super().paint(painter, option, index)
            return
        value, maximum, text = prog
        from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionProgressBar

        opt = QStyleOptionProgressBar()
        opt.rect = option.rect.adjusted(4, 6, -4, -6)
        opt.minimum, opt.maximum = 0, max(maximum, 1)
        opt.progress = value
        opt.text = text
        opt.textVisible = True
        opt.state = QStyle.State_Enabled | QStyle.State_Horizontal
        QApplication.style().drawControl(QStyle.CE_ProgressBar, opt, painter, self._bar)


class DeviceTile(QFrame):
    """Compact card for one device: checkbox + name, serial, status, progress / last action."""

    toggled = Signal(str, bool)

    def __init__(self, serial: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.serial = serial
        self.setObjectName("DeviceTile")
        self.setFixedWidth(TILE_WIDTH)
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(2)

        top = QHBoxLayout()
        top.setSpacing(6)
        self.check = QCheckBox()
        self.check.toggled.connect(lambda on: self.toggled.emit(self.serial, on))
        top.addWidget(self.check)
        self.name = QLabel()
        self.name.setObjectName("TileName")
        top.addWidget(self.name, 1)
        self.dot = QLabel("●")
        top.addWidget(self.dot)
        lay.addLayout(top)

        self.serial_label = QLabel(serial)
        self.serial_label.setObjectName("Muted")
        self.serial_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.serial_label)
        self.status = QLabel()
        lay.addWidget(self.status)
        self.bar = QProgressBar()
        self.bar.setFixedHeight(12)
        self.bar.setTextVisible(False)
        self.bar.hide()
        lay.addWidget(self.bar)
        self.action = QLabel()
        self.action.setObjectName("TileAction")
        lay.addWidget(self.action)

    def update_from(self, dev: Device, checked: bool, progress, busy: bool) -> None:
        icon, text, color = STATUS_DISPLAY[dev.status]
        self.name.setText(dev.name)
        self.name.setToolTip(dev.env_var or "Not mapped by any adb_device_id_* variable")
        self.dot.setStyleSheet(f"color: {color};")
        self.dot.setToolTip(f"adb state: {dev.state.value}")
        self.status.setText(text if dev.status != ConnectionStatus.OTHER else dev.state.value)
        self.status.setStyleSheet(f"color: {color};")
        self.check.blockSignals(True)
        self.check.setChecked(checked)
        self.check.blockSignals(False)
        if progress:
            value, maximum, ptext = progress
            self.bar.setRange(0, max(maximum, 1))
            self.bar.setValue(value)
            self.bar.show()
            line, line_color = ptext, COLORS["info"]
        else:
            self.bar.hide()
            if busy:
                line, line_color = "Working…", COLORS["info"]
            else:
                line = dev.last_action
                line_color = (COLORS["muted"] if dev.last_action_ok is None
                              else COLORS["success"] if dev.last_action_ok else COLORS["error"])
        metrics = self.action.fontMetrics()
        self.action.setText(metrics.elidedText(line, Qt.ElideRight, TILE_WIDTH - 22))
        self.action.setToolTip(line)
        self.action.setStyleSheet(f"color: {line_color}; font-size: 8pt;")
        self.setProperty("checked", checked)
        self.setProperty("muted", not dev.configured)
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - click anywhere toggles
        if event.button() == Qt.LeftButton:
            self.check.toggle()
        super().mousePressEvent(event)


class DevicePanel(Card):
    refresh_requested = Signal()
    prepare_requested = Signal()
    cancel_requested = Signal()
    selection_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Devices", "Discovered from adb_device_id_* environment variables", parent)
        self.model = DeviceTableModel(self)
        self.proxy = DeviceFilterProxy(self)
        self.proxy.setSourceModel(self.model)

        # Header controls
        self.connected_only = QCheckBox("Connected only")
        self.connected_only.toggled.connect(lambda _: self._rebuild_tiles())
        self.add_header_widget(self.connected_only)
        self.select_all_btn = button("Select all", tooltip="Select all connected devices")
        self.select_none_btn = button("Select none")
        self.add_header_widget(self.select_all_btn)
        self.add_header_widget(self.select_none_btn)

        # Device tiles, side by side (scrolls horizontally if they do not fit)
        self.tiles: dict[str, DeviceTile] = {}
        strip = QWidget()
        self.tile_row = QHBoxLayout(strip)
        self.tile_row.setContentsMargins(0, 0, 0, 0)
        self.tile_row.setSpacing(8)
        self.tile_row.addStretch(1)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setWidget(strip)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setFixedHeight(118)
        self.body.addWidget(self.scroll)
        self.model.modelReset.connect(self._rebuild_tiles)
        self.model.dataChanged.connect(lambda *_: self._refresh_tiles())

        self.empty_label = QLabel()
        self.empty_label.setObjectName("Muted")
        self.empty_label.setWordWrap(True)
        self.empty_label.hide()
        self.body.addWidget(self.empty_label)

        # Actions (in the header, next to the selection buttons)
        self.summary = QLabel("")
        self.summary.setObjectName("Muted")
        self.add_header_widget(self.summary)
        self.refresh_btn = button("⟳  Refresh", tooltip="Re-run device discovery (F5)")
        self.cancel_btn = button("Cancel", danger=True, tooltip="Cancel running operations")
        self.cancel_btn.hide()
        self.prepare_btn = button(
            "⚙  Prepare Device", primary=True,
            tooltip="root → disable-verity → reboot → root → remount → setenforce 0 → mount rw",
        )
        self.add_header_widget(self.refresh_btn)
        self.add_header_widget(self.cancel_btn)
        self.add_header_widget(self.prepare_btn)

        self.refresh_btn.clicked.connect(self.refresh_requested)
        self.prepare_btn.clicked.connect(self.prepare_requested)
        self.cancel_btn.clicked.connect(self.cancel_requested)
        self.select_all_btn.clicked.connect(self.select_all_connected)
        self.select_none_btn.clicked.connect(lambda: self.model.set_checked(set()))
        self.model.selection_changed.connect(self._update_summary)
        self.model.selection_changed.connect(self.selection_changed)
        self.model.modelReset.connect(self._update_summary)

    # ------------------------------------------------------------------
    def set_devices(self, devices: list[Device], warnings: list[str] | None = None) -> None:
        self.model.set_devices(devices)
        msg = "\n".join(warnings or [])
        self.empty_label.setText(("⚠  " + msg) if msg else "")
        self.empty_label.setVisible(bool(msg))

    def checked_devices(self) -> list[Device]:
        return self.model.checked_devices()

    def select_all_connected(self) -> None:
        self.model.set_checked({d.serial for d in self.model.devices() if d.is_ready})

    def set_busy(self, busy: bool) -> None:
        self.prepare_btn.setEnabled(not busy)
        self.refresh_btn.setEnabled(not busy)
        self.cancel_btn.setVisible(busy)

    def _rebuild_tiles(self) -> None:
        for tile in self.tiles.values():
            tile.deleteLater()
        self.tiles.clear()
        only = self.connected_only.isChecked()
        for i, dev in enumerate(d for d in self.model.devices() if d.is_ready or not only):
            tile = DeviceTile(dev.serial)
            tile.toggled.connect(self._on_tile_toggled)
            self.tiles[dev.serial] = tile
            self.tile_row.insertWidget(i, tile)
        self._refresh_tiles()

    def _refresh_tiles(self) -> None:
        checked = {d.serial for d in self.model.checked_devices()}
        for serial, tile in self.tiles.items():
            dev = self.model.device(serial)
            if dev is not None:
                tile.update_from(dev, serial in checked, self.model.progress(serial), self.model.is_busy(serial))

    def _on_tile_toggled(self, serial: str, on: bool) -> None:
        current = {d.serial for d in self.model.checked_devices()}
        self.model.set_checked(current | {serial} if on else current - {serial})

    def _update_summary(self) -> None:
        devs = self.model.devices()
        connected = sum(d.is_ready for d in devs)
        selected = len(self.model.checked_devices())
        self.summary.setText(f"{len(devs)} device(s) • {connected} connected • {selected} selected")
