"""Connected Devices section: role / serial / status table + Refresh."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout)

from ..config import ENV_PREFIX
from ..core.adb import is_cpe
from ..tasks.base_task import State
from ..tasks.device_scan_task import DeviceScanTask
from .theme import COLOR_GRAY, COLOR_GREEN, COLOR_RED

_STATE_LABELS = {
    "device": "Connected",
    "missing": "Not connected",
    "offline": "Offline",
    "unauthorized": "Unauthorized",
    "scan failed": "adb failed (see status bar)",
}


class DevicesPanel(QGroupBox):
    # Emitted after each scan with the configured roles, in display order
    # (e.g. ["AP", "CPE1", "CPE2"]). Panels rebuild their check boxes from it.
    roles_changed = Signal(list)
    # (state, message) for the main status bar.
    status_message = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__("Connected Devices", parent)
        self._task = None
        self.role_serials: dict[str, str] = {}   # all configured roles
        self.connected: dict[str, str] = {}      # connected roles only

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Role", "Serial", "Status"])
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setFocusPolicy(Qt.NoFocus)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.setMinimumHeight(110)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        hint = QLabel(f"Serials are read from environment variables "
                      f"{ENV_PREFIX}ap, {ENV_PREFIX}cpe1 ... {ENV_PREFIX}cpe<N>")
        hint.setStyleSheet(f"color: {COLOR_GRAY};")

        top = QHBoxLayout()
        top.addWidget(hint, 1)
        top.addWidget(self.refresh_btn)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.table)

    def refresh(self):
        if self._task and self._task.isRunning():
            return
        self.refresh_btn.setEnabled(False)
        self.status_message.emit(State.WAITING, "Scanning devices...")
        self._task = DeviceScanTask()
        self._task.result.connect(self._fill_table)
        self._task.done.connect(self._scan_done)
        self._task.start()

    def _scan_done(self, state, message):
        self.refresh_btn.setEnabled(True)
        if state == State.ERROR:
            self.status_message.emit(state, message)
        else:
            self.status_message.emit(State.IDLE, f"Idle - {message}")

    def _fill_table(self, rows):
        self.table.setRowCount(len(rows))
        self.role_serials.clear()
        self.connected.clear()
        for i, (role, serial, state) in enumerate(rows):
            if role != "(unassigned)":
                self.role_serials[role] = serial
                if state == "device":
                    self.connected[role] = serial
            if role == "(unassigned)":
                color = COLOR_GRAY   # connected but not mapped to a role
            elif state == "device":
                color = COLOR_GREEN
            else:
                color = COLOR_RED
            items = [QTableWidgetItem(role),
                     QTableWidgetItem(serial or "-"),
                     QTableWidgetItem(_STATE_LABELS.get(state, state))]
            for col, item in enumerate(items):
                item.setForeground(QColor(color))
                self.table.setItem(i, col, item)
        self.table.resizeRowsToContents()
        # Grow the table with the setup (1..16 CPEs) up to a sensible height.
        visible = min(max(len(rows), 3), 10)
        height = (self.table.horizontalHeader().height() + 4
                  + sum(self.table.rowHeight(r) for r in range(min(len(rows), visible)))
                  + max(0, visible - len(rows)) * 24)
        self.table.setFixedHeight(height)
        self.roles_changed.emit(list(self.role_serials))

    def serial_for(self, role: str) -> str | None:
        """Serial configured for a role (connected or not)."""
        return self.role_serials.get(role)

    def is_connected(self, role: str) -> bool:
        return role in self.connected

    def roles(self) -> list[str]:
        """Configured roles in display order."""
        return list(self.role_serials)

    def cpe_roles(self) -> list[str]:
        return [r for r in self.role_serials if is_cpe(r)]
