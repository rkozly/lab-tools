"""Serial Logging section: console (dmesg) collection per device over COM ports.

Devices are the roles that have a COM port (from the MobaXterm sessions
"<ROLE> - COM<N>" or serial_port_<role> variables) plus the roles in the
Connected Devices table. The port of each device can be changed by hand.
"""

from PySide6.QtCore import QSettings, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QGroupBox, QHBoxLayout,
                               QLabel, QMessageBox, QPushButton, QVBoxLayout,
                               QWidget)

from ..config import (BAUD_RATES, DEFAULT_BAUD_RATE, SERIAL_ENV_PREFIX,
                      default_serial_path)
from ..core.adb import order_roles
from ..core.serial_ports import available_ports, load_port_map
from ..tasks.base_task import State
from ..tasks.serial_task import SerialTask
from .theme import COLOR_GRAY
from .widgets import CheckedRows, PathEdit, StatusLabel


class _SerialRow(QWidget):
    """COM port, output path, Stop button and status for one device."""

    def __init__(self, role: str, port: str, path: str, parent=None):
        super().__init__(parent)
        self.role = role
        self.task: SerialTask | None = None

        self.port = QComboBox()
        self.port.setEditable(True)  # any name works, e.g. a port not plugged in yet
        self.port.setMinimumWidth(90)
        self.set_ports([], port)
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
        lay.addWidget(QLabel("Port:"))
        lay.addWidget(self.port)
        lay.addWidget(QLabel("Output Path:"))
        lay.addWidget(self.path, 1)
        lay.addWidget(self.stop_btn)
        lay.addWidget(self.status)

    def set_ports(self, ports: list[str], current: str) -> None:
        self.port.blockSignals(True)
        self.port.clear()
        items = list(ports)
        if current and current not in items:
            items.insert(0, current)
        self.port.addItems(items)
        self.port.setCurrentText(current)
        self.port.blockSignals(False)

    def port_name(self) -> str:
        return self.port.currentText().strip().upper()

    def is_running(self) -> bool:
        return self.task is not None and self.task.isRunning()

    def set_busy(self, busy: bool) -> None:
        self.stop_btn.setEnabled(busy)
        self.port.setEnabled(not busy)
        self.path.setEnabled(not busy)


class SerialPanel(QGroupBox):
    status_message = Signal(str, str)

    TITLE = "Serial Logging (dmesg / console)"
    LABEL = "Serial"
    KEY = "serial"

    def __init__(self, devices, settings: QSettings, parent=None):
        """devices: DevicesPanel, its roles are shown even without a COM port."""
        super().__init__(self.TITLE, parent)
        self.settings = settings
        self._device_roles: list[str] = []
        self._ports: dict[str, str] = {}   # role -> COM port found automatically
        self._present: list[str] = []      # COM ports present on this PC

        self.baud = QComboBox()
        self.baud.setEditable(True)
        self.baud.addItems([str(b) for b in BAUD_RATES])
        self.baud.setCurrentText(str(settings.value(f"{self.KEY}/baud", DEFAULT_BAUD_RATE)))
        self.timestamps = QCheckBox("Add PC timestamps")
        self.timestamps.setToolTip("Prefix every line with the time it was received")
        self.timestamps.setChecked(settings.value(f"{self.KEY}/timestamps", False, type=bool))
        self.reload_btn = QPushButton("Reload Ports")
        self.reload_btn.clicked.connect(self.reload_ports)
        self.source = QLabel()
        self.source.setStyleSheet(f"color: {COLOR_GRAY};")
        self.source.setWordWrap(True)

        top = QHBoxLayout()
        top.addWidget(QLabel("Baud rate:"))
        top.addWidget(self.baud)
        top.addSpacing(14)
        top.addWidget(self.timestamps)
        top.addStretch(1)
        top.addWidget(self.reload_btn)

        hint = QLabel("Close the device's session in MobaXterm first: "
                      "a COM port can only be opened by one program at a time.")
        hint.setStyleSheet(f"color: {COLOR_GRAY};")
        hint.setWordWrap(True)

        self.list = CheckedRows(self._make_row, empty_text=(
            "No serial ports found. Name the MobaXterm sessions '<ROLE> - COM<N>' "
            f"(e.g. 'CPE2 - COM12') or set {SERIAL_ENV_PREFIX}cpe2=COM12, "
            "then press Reload Ports."))

        self.start_btn = QPushButton("Start Collection")
        self.start_btn.clicked.connect(self.start_selected)
        self.stop_all_btn = QPushButton("Stop All")
        self.stop_all_btn.clicked.connect(self.stop_all)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.start_btn)
        buttons.addWidget(self.stop_all_btn)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.source)
        lay.addWidget(hint)
        lay.addWidget(self.list)
        lay.addLayout(buttons)

        devices.roles_changed.connect(self._on_device_roles)
        self.reload_ports()

    @property
    def rows(self) -> dict[str, _SerialRow]:
        return self.list.rows

    def _make_row(self, role: str) -> _SerialRow:
        port = self.settings.value(f"{self.KEY}/{role}/port", "") or self._ports.get(role, "")
        path = self.settings.value(f"{self.KEY}/{role}/path", default_serial_path(role))
        row = _SerialRow(role, port, path)
        row.set_ports(self._present, port)
        row.stop_btn.clicked.connect(lambda _=False, r=role: self.stop(r))
        return row

    # ----- device / port list ----------------------------------------------

    def _on_device_roles(self, roles: list[str]):
        self._device_roles = list(roles)
        self._update_roles()

    def reload_ports(self):
        """Read the MobaXterm sessions and the COM ports present again."""
        self._ports, source = load_port_map()
        self._present = available_ports()
        found = ", ".join(f"{r}={p}" for r, p in
                          ((r, self._ports[r]) for r in order_roles(self._ports)))
        self.source.setText(f"Ports from {source}" + (f": {found}" if found else ""))
        for role, row in self.rows.items():
            if not row.is_running():
                saved = self.settings.value(f"{self.KEY}/{role}/port", "")
                row.set_ports(self._present, saved or self._ports.get(role, row.port_name()))
        self._update_roles()

    def _update_roles(self):
        roles = order_roles(set(self._device_roles) | set(self._ports))
        for role in self.list.set_items(roles):
            if self.settings.value(f"{self.KEY}/{role}/checked", False, type=bool):
                self.list.grid.set_checked(role, True)

    # ----- collection control ----------------------------------------------

    def _baud_rate(self) -> int | None:
        try:
            baud = int(self.baud.currentText().strip())
        except ValueError:
            return None
        return baud if baud > 0 else None

    def start_selected(self):
        selected = self.list.checked()
        if not selected:
            self.status_message.emit(State.ERROR, f"{self.LABEL}: no device selected")
            return
        baud = self._baud_rate()
        if baud is None:
            self.status_message.emit(State.ERROR, f"{self.LABEL}: invalid baud rate")
            QMessageBox.warning(self, self.TITLE,
                                f"Invalid baud rate: {self.baud.currentText()!r}")
            return
        problems = []
        for role in selected:
            if self.rows[role].is_running():
                continue  # already collecting, leave it alone
            msg = self._start(role, baud)
            if msg:
                problems.append(f"{role}: {msg}")
        if problems:
            QMessageBox.warning(self, self.TITLE, "\n".join(problems))

    def _start(self, role: str, baud: int) -> str:
        """Start one device. Returns an error message or ''."""
        row = self.rows[role]
        port = row.port_name()
        if not port:
            row.status.set_state(State.ERROR, "No COM port")
            return "no COM port selected"
        if not row.path.path():
            row.status.set_state(State.ERROR, "Output path is empty")
            return "output path is empty"
        for other, other_row in self.rows.items():
            if other != role and other_row.is_running() and other_row.port_name() == port:
                row.status.set_state(State.ERROR, f"{port} used by {other}")
                return f"{port} is already being collected for {other}"

        task = SerialTask(role, port, baud, row.path.path(),
                          timestamps=self.timestamps.isChecked())
        task.status.connect(lambda s, m, r=role: self._on_status(r, s, m))
        task.done.connect(lambda s, m, r=role: self._on_done(r, s, m))
        row.task = task
        row.set_busy(True)
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
        # Size updates every second would flood the status bar.
        if not message.startswith("Collecting from"):
            self.status_message.emit(state, f"{self.LABEL} {role}: {message}")

    def _on_done(self, role, state, message):
        row = self.rows.get(role)
        if row:
            row.status.set_state(state, message)
            row.set_busy(False)
            row.task = None
        self.status_message.emit(state, f"{self.LABEL} {role}: {message}")

    # ----- settings ---------------------------------------------------------

    def save_settings(self, s: QSettings):
        s.setValue(f"{self.KEY}/baud", self.baud.currentText().strip())
        s.setValue(f"{self.KEY}/timestamps", self.timestamps.isChecked())
        for role, row in self.rows.items():
            s.setValue(f"{self.KEY}/{role}/path", row.path.path())
            s.setValue(f"{self.KEY}/{role}/checked", self.list.grid.is_checked(role))
            # Only remember a port the user changed by hand.
            port = row.port_name()
            s.setValue(f"{self.KEY}/{role}/port",
                       "" if port == self._ports.get(role, "") else port)

