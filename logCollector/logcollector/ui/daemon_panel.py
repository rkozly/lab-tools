"""WPA Supplicant (CPE) and Hostapd (AP) Logging sections.

Ticking a device checks it straight away: the start-up files must contain the
debug changes and the running daemon must have been started with them. If
not, one dialog lists every device that needs changes and offers to apply
them (restart the service or reboot). Start only runs on verified devices.
"""

from PySide6.QtCore import QSettings, QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QGroupBox, QHBoxLayout, QLabel,
                               QMessageBox, QPushButton, QVBoxLayout, QWidget)

from ..config import (AP_ROLE, ENV_PREFIX, HOSTAPD_PROFILE, WPA_PROFILE,
                      DaemonLogProfile, default_hostapd_path, default_wpa_path)
from ..core.adb import is_cpe
from ..tasks.base_task import State
from ..tasks.daemon_log_task import DaemonCheckTask, DaemonLogTask
from .theme import COLOR_GRAY
from .widgets import CheckedRows, PathEdit, StatusLabel

RESTART = "Restart service"
REBOOT = "Reboot device"


class _DaemonRow(QWidget):
    """Output path, Check / Stop buttons and status for one device."""

    def __init__(self, role: str, path: str, parent=None):
        super().__init__(parent)
        self.role = role
        self.task: DaemonLogTask | None = None
        self.check_task: DaemonCheckTask | None = None
        self.verified = False

        self.path = PathEdit(path, mode="save",
                             file_filter="Log files (*.log *.txt);;All files (*)")
        self.check_btn = QPushButton("Check")
        self.check_btn.setToolTip("Check the device again")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.status = StatusLabel()
        self.status.setMinimumWidth(220)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        name = QLabel(role)
        name.setMinimumWidth(44)
        lay.addWidget(name)
        lay.addWidget(QLabel("Output Path:"))
        lay.addWidget(self.path, 1)
        lay.addWidget(self.check_btn)
        lay.addWidget(self.stop_btn)
        lay.addWidget(self.status)

    def is_collecting(self) -> bool:
        return self.task is not None and self.task.isRunning()

    def is_checking(self) -> bool:
        return self.check_task is not None and self.check_task.isRunning()

    def is_running(self) -> bool:
        return self.is_collecting() or self.is_checking()

    def update_buttons(self) -> None:
        self.check_btn.setEnabled(not self.is_running())
        self.stop_btn.setEnabled(self.is_collecting())
        self.path.setEnabled(not self.is_collecting())


class DaemonLogPanel(QGroupBox):
    status_message = Signal(str, str)

    PROFILE: DaemonLogProfile = WPA_PROFILE
    default_path = staticmethod(default_wpa_path)

    def __init__(self, devices, settings: QSettings, parent=None):
        """devices: DevicesPanel, used to look up serials per role."""
        p = self.PROFILE
        super().__init__(p.title, parent)
        self.devices = devices
        self.settings = settings
        self._fix_pending: list[str] = []   # roles waiting for the fix dialog
        self._restoring = False

        self.restart = QComboBox()
        self.restart.addItems([RESTART, REBOOT])
        self.restart.setCurrentText(settings.value(f"{p.key}/restart", RESTART))
        self.restart.setToolTip(
            f"How to make the device use the changed files.\n{RESTART}: "
            + "; ".join(p.restart_commands) + f"\n{REBOOT}: reboot")
        top = QHBoxLayout()
        top.addWidget(QLabel("After changing the files:"))
        top.addWidget(self.restart)
        top.addStretch(1)

        hint = QLabel(
            f"Ticking a device checks that {', '.join(p.files())} enable debug logging "
            f"and that {p.binary} runs with it. The log ({p.remote_log}) is copied "
            "to the PC live, so a crash or reboot does not lose it.")
        hint.setStyleSheet(f"color: {COLOR_GRAY};")
        hint.setWordWrap(True)

        side = "AP" if p.side == "ap" else "CPEs"
        self.list = CheckedRows(self._make_row, empty_text=(
            f"No {side} configured. Set {ENV_PREFIX}"
            f"{'ap' if p.side == 'ap' else 'cpe1 ...'} and press Refresh."))
        self.list.grid.toggled.connect(self._on_toggled)

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
        lay.addWidget(hint)
        lay.addWidget(self.list)
        lay.addLayout(buttons)

        devices.roles_changed.connect(self.set_roles)
        self.set_roles([])

    @property
    def rows(self) -> dict[str, _DaemonRow]:
        return self.list.rows

    def _make_row(self, role: str) -> _DaemonRow:
        path = self.settings.value(f"{self.PROFILE.key}/{role}/path", self.default_path(role))
        row = _DaemonRow(role, path)
        row.status.set_state(State.IDLE, "Not checked")
        row.check_btn.clicked.connect(lambda _=False, r=role: self.check(r))
        row.stop_btn.clicked.connect(lambda _=False, r=role: self.stop(r))
        return row

    def _wanted(self, role: str) -> bool:
        return role == AP_ROLE if self.PROFILE.side == "ap" else is_cpe(role)

    def set_roles(self, roles: list[str]):
        roles = [r for r in roles if self._wanted(r)]
        self._restoring = True
        try:
            for role in self.list.set_items(roles):
                if self.settings.value(f"{self.PROFILE.key}/{role}/checked", False, type=bool):
                    self.list.grid.set_checked(role, True)
        finally:
            self._restoring = False
        # Devices may have (re)connected: re-check the ticked ones. A device
        # refresh never opens the fix dialog by itself; ticking or Check does.
        for role in self.list.checked():
            row = self.rows[role]
            if not row.verified and not row.is_running():
                self.check(role, quiet=True)

    def _on_toggled(self, role: str, on: bool):
        if on and not self._restoring:
            self.check(role)

    # ----- checking ---------------------------------------------------------

    def check(self, role: str, fix: bool = False, quiet: bool = False):
        """Check one device (fix=True: apply the changes first if needed)."""
        row = self.rows.get(role)
        if row is None or row.is_running():
            return
        serial = self.devices.serial_for(role)
        if not serial:
            row.status.set_state(State.ERROR, "No serial configured")
            return
        if not self.devices.is_connected(role):
            row.status.set_state(State.ERROR, "Device not connected")
            return
        row.verified = False
        task = DaemonCheckTask(self.PROFILE, role, serial, fix=fix,
                               reboot=self.restart.currentText() == REBOOT)
        task.status.connect(lambda s, m, r=role: self._on_status(r, s, m))
        task.result.connect(lambda rep, r=role, q=quiet: self._on_check_result(r, rep, q))
        task.done.connect(lambda s, m, r=role: self._on_check_done(r, s, m))
        row.check_task = task
        task.start()
        row.update_buttons()

    def _on_check_result(self, role: str, report: dict, quiet: bool):
        row = self.rows.get(role)
        if row is None:
            return
        row.verified = report["ok"]
        if not report["ok"] and report["fixable"] and not quiet \
                and self.list.grid.is_checked(role) and role not in self._fix_pending:
            self._fix_pending.append(role)

    def _on_check_done(self, role, state, message):
        row = self.rows.get(role)
        if row:
            row.check_task = None
            row.status.set_state(State.COMPLETED if row.verified else State.ERROR, message)
            row.update_buttons()
        self.status_message.emit(state, f"{self.PROFILE.label} {role}: {message}")
        # Ask once for every device that needs changes, after all checks ended.
        if self._fix_pending and not any(r.is_checking() for r in self.rows.values()):
            QTimer.singleShot(0, self._ask_fix)

    def _ask_fix(self):
        roles = [r for r in self._fix_pending if r in self.rows
                 and not self.rows[r].verified and not self.rows[r].is_running()]
        self._fix_pending.clear()
        if not roles:
            return
        p = self.PROFILE
        details = "\n".join(f"  {r}: {self.rows[r].status.toolTip()}" for r in roles)
        changes = "\n".join(
            f"  {e.path}: " + (f"add debug flags" if e.kind == "replace" else "comment out close_log")
            for e in p.edits)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle(p.title)
        box.setText(f"Debug logging is not enabled on:\n{details}\n\n"
                    f"Apply these changes (a .orig backup is kept)?\n{changes}\n\n"
                    f"Afterwards the device must use the new files:\n"
                    f"  {RESTART}: " + "; ".join(p.restart_commands) +
                    f"\n  {REBOOT}: reboot")
        restart_btn = box.addButton(f"Apply + {RESTART}", QMessageBox.AcceptRole)
        reboot_btn = box.addButton(f"Apply + {REBOOT}", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(restart_btn if self.restart.currentText() == RESTART
                             else reboot_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked not in (restart_btn, reboot_btn):
            for r in roles:
                self.rows[r].status.set_state(State.ERROR, "Not enabled - not changed")
            return
        self.restart.setCurrentText(RESTART if clicked is restart_btn else REBOOT)
        for r in roles:
            self.check(r, fix=True)

    # ----- collection control ----------------------------------------------

    def start_selected(self):
        selected = self.list.checked()
        if not selected:
            self.status_message.emit(State.ERROR, f"{self.PROFILE.label}: no device selected")
            return
        problems = []
        for role in selected:
            if self.rows[role].is_collecting():
                continue  # already collecting, leave it alone
            msg = self._start(role)
            if msg:
                problems.append(f"{role}: {msg}")
        if problems:
            QMessageBox.warning(self, self.PROFILE.title, "\n".join(problems))

    def _start(self, role: str) -> str:
        """Start one device. Returns an error message or ''."""
        row = self.rows[role]
        serial = self.devices.serial_for(role)
        if not serial:
            return "no serial configured (environment variable missing)"
        if not self.devices.is_connected(role):
            row.status.set_state(State.ERROR, "Device not connected")
            return f"device {serial} is not connected"
        if row.is_checking():
            return "still being checked - press Start again when it shows Verified"
        if not row.verified:
            return "not verified - debug logging must be enabled first (press Check)"
        if not row.path.path():
            row.status.set_state(State.ERROR, "Output path is empty")
            return "output path is empty"

        task = DaemonLogTask(self.PROFILE, role, serial, row.path.path())
        task.status.connect(lambda s, m, r=role: self._on_status(r, s, m))
        task.done.connect(lambda s, m, r=role: self._on_done(r, s, m))
        row.task = task
        task.start()
        row.update_buttons()
        return ""

    def stop(self, role: str):
        row = self.rows.get(role)
        if row and row.is_collecting():
            row.task.stop()

    def stop_all(self):
        for role in list(self.rows):
            self.stop(role)

    def _on_status(self, role, state, message):
        if role in self.rows:
            self.rows[role].status.set_state(state, message)
        self.status_message.emit(state, f"{self.PROFILE.label} {role}: {message}")

    def _on_done(self, role, state, message):
        row = self.rows.get(role)
        if row:
            row.status.set_state(state, message)
            row.task = None
            row.update_buttons()
        self.status_message.emit(state, f"{self.PROFILE.label} {role}: {message}")

    # ----- settings ---------------------------------------------------------

    def save_settings(self, s: QSettings):
        key = self.PROFILE.key
        s.setValue(f"{key}/restart", self.restart.currentText())
        for role, row in self.rows.items():
            s.setValue(f"{key}/{role}/path", row.path.path())
            s.setValue(f"{key}/{role}/checked", self.list.grid.is_checked(role))


class WpaPanel(DaemonLogPanel):
    PROFILE = WPA_PROFILE
    default_path = staticmethod(default_wpa_path)


class HostapdPanel(DaemonLogPanel):
    PROFILE = HOSTAPD_PROFILE
    default_path = staticmethod(default_hostapd_path)
