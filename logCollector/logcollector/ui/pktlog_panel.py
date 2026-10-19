"""
PKTLOG Logging section.

Contains three groups:
    AP Collection   - PHY0 / PHY1 on the AP
    CPE Collection  - radio0 on every configured CPE (CPE1 ... CPE<N>)
    PKTLOG Translation (see translate_panel.py)

The AP and CPE groups use the same widget (`PktlogGroup`); they differ only
in their capture entries. The CPE entries follow the Connected Devices table,
so a setup with 1, 2, 4, 8 or 16 CPEs shows exactly that many.
"""

from dataclasses import dataclass

from PySide6.QtCore import QSettings, Signal
from PySide6.QtWidgets import (QGroupBox, QHBoxLayout, QLabel, QMessageBox,
                               QPushButton, QSpinBox, QVBoxLayout, QWidget)

from ..config import (AP_PHY_PROFILES, AP_ROLE, DEFAULT_OUTPUT_DIR,
                      DEFAULT_PKTLOG_DURATION, ENV_PREFIX, PktlogProfile,
                      cpe_profile)
from ..tasks.base_task import State
from ..tasks.pktlog_task import PktlogTask
from .translate_panel import TranslatePanel
from .widgets import CheckedRows, FolderFileEdit, StatusLabel


@dataclass
class CaptureEntry:
    label: str        # check box text ("PHY0", "CPE1")
    source: str       # unique name ("AP PHY0", "CPE1")
    role: str         # device role the capture runs on
    profile: PktlogProfile


class _CaptureRow(QWidget):
    def __init__(self, entry: CaptureEntry, dest: str, parent=None):
        super().__init__(parent)
        self.entry = entry
        self.task: PktlogTask | None = None

        self.dest = FolderFileEdit(DEFAULT_OUTPUT_DIR, entry.profile.default_name,
                                   file_filter="PKTLOG files (*.dat);;All files (*)")
        if dest:
            self.dest.set_path(dest)
        self.status = StatusLabel()
        self.status.setMinimumWidth(150)
        # Shown when a capture finished recording but "cp" / "adb pull"
        # failed: redoes only those two steps, without a new capture.
        self.retry_btn = QPushButton("Retry Copy")
        self.retry_btn.setToolTip("Copy and pull the last capture again "
                                  "(no new capture)")
        self.retry_btn.setVisible(False)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        name = QLabel(entry.label)
        name.setMinimumWidth(44)
        lay.addWidget(name)
        lay.addWidget(self.dest, 1)
        lay.addWidget(self.retry_btn)
        lay.addWidget(self.status)

    def is_running(self) -> bool:
        return self.task is not None and self.task.isRunning()


class PktlogGroup(QGroupBox):
    status_message = Signal(str, str)
    # (source, destination path) after a successful pull
    capture_completed = Signal(str, str)

    def __init__(self, title: str, key: str, devices, settings: QSettings,
                 empty_text: str = "", parent=None):
        super().__init__(title, parent)
        self.key = key  # settings prefix
        self.devices = devices
        self.settings = settings
        self.entries: dict[str, CaptureEntry] = {}

        self.list = CheckedRows(self._make_row, empty_text=empty_text)

        self.duration = QSpinBox()
        self.duration.setRange(1, 3600)
        self.duration.setSuffix(" s")
        self.duration.setValue(settings.value(f"{key}/duration",
                                              DEFAULT_PKTLOG_DURATION, type=int))
        self.start_btn = QPushButton("Start")
        self.start_btn.clicked.connect(self.start_selected)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop_all)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Duration:"))
        controls.addWidget(self.duration)
        controls.addStretch(1)
        controls.addWidget(self.start_btn)
        controls.addWidget(self.stop_btn)

        lay = QVBoxLayout(self)
        lay.addWidget(self.list)
        lay.addLayout(controls)

    @property
    def rows(self) -> dict[str, _CaptureRow]:
        return self.list.rows

    def set_entries(self, entries: list[CaptureEntry]):
        for e in entries:
            self.entries.setdefault(e.source, e)
        labels = {e.source: e.label for e in entries}
        for source in self.list.set_items([e.source for e in entries], labels):
            if self.settings.value(f"{self.key}/{source}/checked", False, type=bool):
                self.list.grid.set_checked(source, True)

    def _make_row(self, source: str) -> _CaptureRow:
        dest = self.settings.value(f"{self.key}/{source}/dest", "")
        row = _CaptureRow(self.entries[source], dest)
        row.retry_btn.clicked.connect(lambda _=False, s=source: self.retry_copy(s))
        return row

    # ----- control ----------------------------------------------------------

    def start_selected(self):
        selected = self.list.checked()
        if not selected:
            self.status_message.emit(State.ERROR, f"{self.title()}: nothing selected")
            return
        problems = []
        for source in selected:
            if self.rows[source].is_running():
                continue
            msg = self._start(source)
            if msg:
                problems.append(f"{source}: {msg}")
        if problems:
            QMessageBox.warning(self, self.title(), "\n".join(problems))

    def retry_copy(self, source: str):
        row = self.rows.get(source)
        if row is None or row.is_running():
            return
        msg = self._start(source, copy_only=True)
        if msg:
            QMessageBox.warning(self, self.title(), f"{source}: {msg}")

    def _start(self, source: str, copy_only: bool = False) -> str:
        row = self.rows[source]
        e = row.entry
        serial = self.devices.serial_for(e.role)
        if not serial:
            row.status.set_state(State.ERROR, "No serial configured")
            return f"no serial configured for {e.role}"
        if not self.devices.is_connected(e.role):
            row.status.set_state(State.ERROR, "Device not connected")
            return f"{e.role} ({serial}) is not connected"
        dest = row.dest.path()
        if not dest:
            row.status.set_state(State.ERROR, "Destination is empty")
            return "destination is empty"

        task = PktlogTask(source, serial, e.profile, self.duration.value(), dest,
                          copy_only=copy_only)
        task.status.connect(lambda s, m, src=source: self._on_status(src, s, m))
        task.done.connect(lambda s, m, src=source: self._on_done(src, s, m))
        row.task = task
        row.dest.setEnabled(False)
        row.retry_btn.setVisible(False)
        task.start()
        return ""

    def stop_all(self):
        for row in self.rows.values():
            if row.is_running():
                row.task.stop()

    def _on_status(self, source, state, message):
        if source in self.rows:
            self.rows[source].status.set_state(state, message)
        self.status_message.emit(state, f"PKTLOG {source}: {message}")

    def _on_done(self, source, state, message):
        row = self.rows.get(source)
        dest = ""
        if row:
            row.status.set_state(state, message)
            row.dest.setEnabled(True)
            # Offer Retry Copy only if the capture itself was done.
            row.retry_btn.setVisible(state == State.ERROR and row.task is not None
                                     and row.task.captured)
            row.task = None
            dest = row.dest.path()
        self.status_message.emit(state, f"PKTLOG {source}: {message}")
        if state == State.COMPLETED and dest:
            self.capture_completed.emit(source, dest)

    # ----- settings ---------------------------------------------------------

    def save_settings(self, s: QSettings):
        s.setValue(f"{self.key}/duration", self.duration.value())
        for source, row in self.rows.items():
            s.setValue(f"{self.key}/{source}/dest", row.dest.path())
            s.setValue(f"{self.key}/{source}/checked", self.list.grid.is_checked(source))


class PktlogPanel(QGroupBox):
    """Container with the AP, CPE and translation groups."""

    def __init__(self, devices, settings: QSettings, parent=None):
        super().__init__("PKTLOG Logging", parent)
        self.devices = devices
        self.ap = PktlogGroup("AP Collection", "pktlog_ap", devices, settings,
                              empty_text=f"No AP configured ({ENV_PREFIX}ap).")
        self.cpe = PktlogGroup("CPE Collection", "pktlog_cpe", devices, settings,
                               empty_text=f"No CPEs configured ({ENV_PREFIX}cpe1 ...).")
        self.translate = TranslatePanel()
        for group in self.groups:
            group.capture_completed.connect(self.translate.on_capture_completed)

        # Stacked vertically so the folder / file fields get the full width.
        lay = QVBoxLayout(self)
        lay.addWidget(self.ap)
        lay.addWidget(self.cpe)
        lay.addWidget(self.translate)

        devices.roles_changed.connect(self.set_roles)
        self.set_roles([])

    def set_roles(self, roles: list[str]):
        # Same rule as the device table: only configured devices are shown.
        ap = AP_ROLE in roles
        self.ap.set_entries([CaptureEntry(phy, f"AP {phy}", AP_ROLE, prof)
                             for phy, prof in AP_PHY_PROFILES.items()] if ap else [])
        self.cpe.set_entries([CaptureEntry(r, r, r, cpe_profile(r))
                              for r in self.devices.cpe_roles()])

    @property
    def groups(self):
        return (self.ap, self.cpe)

    def save_settings(self, s: QSettings):
        for g in self.groups:
            g.save_settings(s)
        self.translate.save_settings(s)
