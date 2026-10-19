"""CNSS Logging and CONGCTRL Logging sections: one independent collection per device.

The device check boxes follow the Connected Devices table: every configured
role (AP, CPE1 ... CPE<N>) gets one box, so the panel fits setups with any
number of CPEs.

CongctrlPanel is the same panel with its own title, settings key, default
file names and task (see CongctrlTask).
"""

from PySide6.QtCore import QSettings, Signal
from PySide6.QtWidgets import (QGroupBox, QHBoxLayout, QLabel, QMessageBox,
                               QPushButton, QVBoxLayout, QWidget)

from ..config import ENV_PREFIX, default_cnss_path, default_congctrl_path
from ..tasks.base_task import State
from ..tasks.cnss_task import CnssTask, CongctrlTask
from .widgets import CheckedRows, PathEdit, StatusLabel


class _CnssRow(QWidget):
    """Output path, Stop button and status for one device."""

    def __init__(self, role: str, path: str, parent=None):
        super().__init__(parent)
        self.role = role
        self.task: CnssTask | None = None

        self.path = PathEdit(path, mode="save",
                             file_filter="Text files (*.txt);;All files (*)")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.status = StatusLabel()
        self.status.setMinimumWidth(160)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        name = QLabel(role)
        name.setMinimumWidth(44)
        lay.addWidget(name)
        lay.addWidget(QLabel("Output Path:"))
        lay.addWidget(self.path, 1)
        lay.addWidget(self.stop_btn)
        lay.addWidget(self.status)

    def is_running(self) -> bool:
        return self.task is not None and self.task.isRunning()


class CnssPanel(QGroupBox):
    status_message = Signal(str, str)

    TITLE = "CNSS Logging"
    LABEL = "CNSS"           # prefix for status / error messages
    KEY = "cnss"             # settings prefix
    TASK = CnssTask
    default_path = staticmethod(default_cnss_path)

    def __init__(self, devices, settings: QSettings, parent=None):
        """devices: DevicesPanel, used to look up serials per role."""
        super().__init__(self.TITLE, parent)
        self.devices = devices
        self.settings = settings

        self.list = CheckedRows(self._make_row, empty_text=(
            f"No devices configured. Set {ENV_PREFIX}ap / {ENV_PREFIX}cpe1 ... "
            "and press Refresh."))

        self.start_btn = QPushButton("Start Collection")
        self.start_btn.clicked.connect(self.start_selected)
        self.stop_all_btn = QPushButton("Stop All")
        self.stop_all_btn.clicked.connect(self.stop_all)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.start_btn)
        buttons.addWidget(self.stop_all_btn)

        lay = QVBoxLayout(self)
        lay.addWidget(self.list)
        lay.addLayout(buttons)

        devices.roles_changed.connect(self.set_roles)
        self.set_roles([])

    @property
    def rows(self) -> dict[str, _CnssRow]:
        return self.list.rows

    def _make_row(self, role: str) -> _CnssRow:
        path = self.settings.value(f"{self.KEY}/{role}/path", self.default_path(role))
        row = _CnssRow(role, path)
        row.stop_btn.clicked.connect(lambda _=False, r=role: self.stop(r))
        return row

    def set_roles(self, roles: list[str]):
        for role in self.list.set_items(roles):
            if self.settings.value(f"{self.KEY}/{role}/checked", False, type=bool):
                self.list.grid.set_checked(role, True)

    # ----- collection control ----------------------------------------------

    def start_selected(self):
        selected = self.list.checked()
        if not selected:
            self.status_message.emit(State.ERROR, f"{self.LABEL}: no device selected")
            return
        problems = []
        for role in selected:
            if self.rows[role].is_running():
                continue  # already collecting, leave it alone
            msg = self._start(role)
            if msg:
                problems.append(f"{role}: {msg}")
        if problems:
            QMessageBox.warning(self, self.TITLE, "\n".join(problems))

    def _start(self, role: str) -> str:
        """Start one device. Returns an error message or ''."""
        row = self.rows[role]
        serial = self.devices.serial_for(role)
        if not serial:
            row.status.set_state(State.ERROR, "No serial configured")
            return "no serial configured (environment variable missing)"
        if not self.devices.is_connected(role):
            row.status.set_state(State.ERROR, "Device not connected")
            return f"device {serial} is not connected"
        if not row.path.path():
            row.status.set_state(State.ERROR, "Output path is empty")
            return "output path is empty"

        task = self.TASK(role, serial, row.path.path())
        task.status.connect(lambda s, m, r=role: self._on_status(r, s, m))
        task.done.connect(lambda s, m, r=role: self._on_done(r, s, m))
        row.task = task
        row.stop_btn.setEnabled(True)
        row.path.setEnabled(False)
        task.start()
        return ""

    def stop(self, role: str):
        row = self.rows.get(role)
        if row and row.is_running():
            row.task.stop()

    def stop_all(self):
        for role in list(self.rows):
            self.stop(role)

    def _on_status(self, role, state, message):
        if role in self.rows:
            self.rows[role].status.set_state(state, message)
        self.status_message.emit(state, f"{self.LABEL} {role}: {message}")

    def _on_done(self, role, state, message):
        row = self.rows.get(role)
        if row:
            row.status.set_state(state, message)
            row.stop_btn.setEnabled(False)
            row.path.setEnabled(True)
            row.task = None
        self.status_message.emit(state, f"{self.LABEL} {role}: {message}")

    # ----- settings ---------------------------------------------------------

    def save_settings(self, s: QSettings):
        for role, row in self.rows.items():
            s.setValue(f"{self.KEY}/{role}/path", row.path.path())
            s.setValue(f"{self.KEY}/{role}/checked", self.list.grid.is_checked(role))


class CongctrlPanel(CnssPanel):
    TITLE = "CONGCTRL Logging"
    LABEL = "CONGCTRL"
    KEY = "congctrl"
    TASK = CongctrlTask
    default_path = staticmethod(default_congctrl_path)
