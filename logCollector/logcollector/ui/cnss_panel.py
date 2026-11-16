"""CNSS, CONGCTRL, Wifistats and Traffic sections: one independent collection per device.

The device check boxes follow the Connected Devices table: every configured
role (AP, CPE1 ... CPE<N>) gets one box, so the panel fits setups with any
number of CPEs.

CongctrlPanel, WifistatsPanel and TrafficPanel are the same panel with their
own title, settings key, default file names and task (see CongctrlTask,
WifistatsTask, TrafficTask).
"""

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QProgressBar, QPushButton,
                               QSpinBox, QVBoxLayout, QWidget)

from ..config import (ATTEN_SWEEP_MAX_STEPS, ATTENUATOR_PORT, ATTENUATORS,
                      DEFAULT_ATTEN_STEP_SECONDS, DEFAULT_TRAFFIC_INTERVAL,
                      DEFAULT_WIFISTATS_INTERVAL, ENV_PREFIX, default_cnss_path,
                      default_congctrl_path, default_traffic_path,
                      default_wifistats_path)
from ..core.attenuator import (format_attenuators, format_db, parse_attenuators,
                               parse_steps)
from ..tasks.atten_sweep_task import AttenSweepTask
from ..tasks.base_task import State
from ..tasks.cnss_task import CnssTask, CongctrlTask
from ..tasks.traffic_task import TrafficTask
from ..tasks.wifistats_task import WifistatsTask
from .theme import COLOR_BLUE, COLOR_GRAY
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

        task = self._make_task(role, serial, row.path.path())
        task.status.connect(lambda s, m, r=role: self._on_status(r, s, m))
        task.done.connect(lambda s, m, r=role: self._on_done(r, s, m))
        row.task = task
        row.stop_btn.setEnabled(True)
        row.path.setEnabled(False)
        task.start()
        return ""

    def _make_task(self, role: str, serial: str, path: str):
        return self.TASK(role, serial, path)

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


class WifistatsPanel(CnssPanel):
    """Runs the wifistats command list every N seconds (see config)."""

    TITLE = "Wifistats Recording"
    LABEL = "Wifistats"
    KEY = "wifistats"
    TASK = WifistatsTask
    default_path = staticmethod(default_wifistats_path)
    DEFAULT_INTERVAL = DEFAULT_WIFISTATS_INTERVAL
    HINT = ("Every round runs the AP or CPE command list "
            "(wifistats tx/rx, wlanconfig / ifconfig / iw) and appends "
            "the output with a timestamp.")

    def __init__(self, devices, settings: QSettings, parent=None):
        super().__init__(devices, settings, parent)
        self.interval = QSpinBox()
        self.interval.setRange(1, 3600)
        self.interval.setSuffix(" s")
        self.interval.setValue(int(settings.value(f"{self.KEY}/interval",
                                                  self.DEFAULT_INTERVAL)))
        self.interval.setToolTip("Time between the start of two rounds.\n"
                                 "Applies to recordings started after a change.")
        hint = QLabel(self.HINT)
        hint.setStyleSheet(f"color: {COLOR_GRAY};")
        hint.setWordWrap(True)
        top = QHBoxLayout()
        top.addWidget(QLabel("Interval:"))
        top.addWidget(self.interval)
        top.addSpacing(14)
        top.addWidget(hint, 1)
        self.layout().insertLayout(0, top)

    def _make_task(self, role: str, serial: str, path: str):
        return self.TASK(role, serial, path, self.interval.value())

    def save_settings(self, s: QSettings):
        super().save_settings(s)
        s.setValue(f"{self.KEY}/interval", self.interval.value())


class TrafficPanel(WifistatsPanel):
    """Attenuation + interface throughput every N seconds (iperf runs on the BBs).

    Optionally runs an attenuation sweep (custom list of values, each held
    for a set time) while recording; every traffic line then names its step.
    """

    TITLE = "Traffic Recording"
    LABEL = "Traffic"
    KEY = "traffic"
    TASK = TrafficTask
    default_path = staticmethod(default_traffic_path)
    DEFAULT_INTERVAL = DEFAULT_TRAFFIC_INTERVAL
    HINT = ("Every line has the attenuation and the rx / tx Mbit/s of ath0 (CPE) "
            "or mld0 + mld1 (AP). Start iperf on the BB PCs yourself.")

    def __init__(self, devices, settings: QSettings, parent=None):
        super().__init__(devices, settings, parent)
        self.sweep: AttenSweepTask | None = None
        self._steps: list[float] = []
        s = settings

        self.atten_edit = QLineEdit(s.value(f"{self.KEY}/attenuators",
                                            format_attenuators(ATTENUATORS)))
        self.atten_edit.setToolTip("Attenuator addresses, comma separated "
                                   f"(port {ATTENUATOR_PORT}, or host:port).\n"
                                   "Every attenuator is read in each line and set "
                                   "to the same value by the sweep.")
        atten_row = QHBoxLayout()
        atten_row.addWidget(QLabel("Attenuators:"))
        atten_row.addWidget(self.atten_edit, 1)

        self.custom = QCheckBox("Custom attenuations")
        self.custom.setChecked(s.value(f"{self.KEY}/custom", False, type=bool))
        self.custom.setToolTip("Start Collection also runs this list on the "
                               "attenuators, holding each value for the step time.")
        self.steps_edit = QLineEdit(s.value(f"{self.KEY}/steps", "0,1,2,3,4,5"))
        self.steps_edit.setPlaceholderText("e.g. 0,1,2,1,2,3,4,5,6")
        self.steps_edit.setToolTip(f"Attenuation values in dB, comma separated, "
                                   f"at most {ATTEN_SWEEP_MAX_STEPS}.")
        self.step_time = QSpinBox()
        self.step_time.setRange(1, 24 * 3600)
        self.step_time.setSuffix(" s")
        self.step_time.setValue(int(s.value(f"{self.KEY}/step_seconds",
                                            DEFAULT_ATTEN_STEP_SECONDS)))
        self.stop_with_sweep = QCheckBox("Stop recording at the end")
        self.stop_with_sweep.setChecked(s.value(f"{self.KEY}/stop_with_sweep",
                                                True, type=bool))
        self.stop_with_sweep.setToolTip("Stop the traffic recordings when the "
                                        "last step has ended.")
        sweep_row = QHBoxLayout()
        sweep_row.addWidget(self.custom)
        sweep_row.addWidget(self.steps_edit, 1)
        sweep_row.addWidget(QLabel("Each step:"))
        sweep_row.addWidget(self.step_time)
        sweep_row.addWidget(self.stop_with_sweep)

        self.sweep_bar = QProgressBar()
        self.sweep_bar.setTextVisible(True)
        self.sweep_bar.setFormat("")
        self.sweep_status = StatusLabel()
        self.sweep_steps = QLabel()
        self.sweep_steps.setTextFormat(Qt.RichText)
        self.sweep_steps.setWordWrap(True)
        progress_row = QHBoxLayout()
        progress_row.addWidget(self.sweep_bar, 1)
        progress_row.addWidget(self.sweep_status, 1)
        self.sweep_box = QWidget()
        box = QVBoxLayout(self.sweep_box)
        box.setContentsMargins(0, 0, 0, 0)
        box.addLayout(progress_row)
        box.addWidget(self.sweep_steps)

        lay = self.layout()
        lay.insertLayout(1, atten_row)
        lay.insertLayout(2, sweep_row)
        lay.insertWidget(3, self.sweep_box)
        self.custom.toggled.connect(self._sync_custom)
        self.steps_edit.textChanged.connect(self._preview_steps)
        self.step_time.valueChanged.connect(self._preview_steps)
        self._sync_custom()

    # ----- sweep ------------------------------------------------------------

    def _attenuators(self):
        return parse_attenuators(self.atten_edit.text())

    def _sweep_running(self) -> bool:
        return self.sweep is not None and self.sweep.isRunning()

    def _sync_custom(self):
        running = self._sweep_running()
        on = self.custom.isChecked()
        for w in (self.steps_edit, self.step_time, self.stop_with_sweep):
            w.setEnabled(on and not running)
        self.custom.setEnabled(not running)
        self.atten_edit.setEnabled(not running)
        self.sweep_box.setVisible(on or running)
        if not running:
            self._preview_steps()

    def _preview_steps(self):
        if self._sweep_running():
            return
        try:
            steps = parse_steps(self.steps_edit.text())
        except ValueError as e:
            self.sweep_bar.setRange(0, 1)
            self.sweep_bar.setValue(0)
            self.sweep_bar.setFormat("")
            self.sweep_status.set_state(State.ERROR, str(e))
            self.sweep_steps.setText("")
            return
        total = len(steps) * self.step_time.value()
        self.sweep_bar.setRange(0, len(steps))
        self.sweep_bar.setValue(0)
        self.sweep_bar.setFormat(f"0/{len(steps)}")
        self.sweep_status.set_state(State.IDLE, f"{len(steps)} steps, "
                                                f"about {_duration(total)}")
        self._show_steps(steps, 0)

    def _show_steps(self, steps, current: int):
        """The list with done steps grayed and the current one highlighted."""
        parts = []
        for i, v in enumerate(steps, 1):
            text = format_db(v)
            if i == current:
                parts.append(f'<b style="color:{COLOR_BLUE}">[{text}]</b>')
            elif i < current:
                parts.append(f'<span style="color:{COLOR_GRAY}">{text}</span>')
            else:
                parts.append(text)
        self.sweep_steps.setText("dB: " + " &nbsp;".join(parts))

    def _start_sweep(self) -> str:
        """Start the sweep. Returns an error message or ''."""
        try:
            attenuators = self._attenuators()
            steps = parse_steps(self.steps_edit.text())
        except ValueError as e:
            return str(e)
        self._steps = steps
        task = AttenSweepTask(attenuators, steps, self.step_time.value())
        task.status.connect(self._on_sweep_status)
        task.progress.connect(self._on_sweep_progress)
        task.done.connect(self._on_sweep_done)
        self.sweep = task
        self.sweep_bar.setRange(0, len(steps) * self.step_time.value())
        task.start()
        self._sync_custom()
        return ""

    def _on_sweep_status(self, state, message):
        self.sweep_status.set_state(state, message)
        self.status_message.emit(state, f"Attenuation sweep: {message}")

    def _on_sweep_progress(self, step, count, value, left):
        per = self.step_time.value()
        self.sweep_bar.setValue((step - 1) * per + (per - left))
        self.sweep_bar.setFormat(f"step {step}/{count}: {format_db(value)} dB")
        self._show_steps(self._steps, step)

    def _on_sweep_done(self, state, message):
        self.sweep_status.set_state(state, message)
        self.status_message.emit(state, f"Attenuation sweep: {message}")
        if state == State.COMPLETED:
            self.sweep_bar.setValue(self.sweep_bar.maximum())
            self._show_steps(self._steps, len(self._steps) + 1)
        self.sweep = None
        self._sync_custom()
        if state == State.COMPLETED and self.stop_with_sweep.isChecked():
            super().stop_all()

    # ----- collection control ----------------------------------------------

    def start_selected(self):
        sweep = self.custom.isChecked() and not self._sweep_running()
        if sweep:
            try:
                parse_steps(self.steps_edit.text())
                self._attenuators()
            except ValueError as e:
                QMessageBox.warning(self, self.TITLE, str(e))
                return
        if self.list.checked() or not sweep:
            super().start_selected()
        if sweep:
            msg = self._start_sweep()
            if msg:
                QMessageBox.warning(self, self.TITLE, msg)

    def _make_task(self, role: str, serial: str, path: str):
        try:
            attenuators = self._attenuators()
        except ValueError:
            attenuators = ATTENUATORS
        return self.TASK(role, serial, path, self.interval.value(), attenuators)

    def stop_all(self):
        if self._sweep_running():
            self.sweep.stop()
        super().stop_all()

    def save_settings(self, s: QSettings):
        super().save_settings(s)
        s.setValue(f"{self.KEY}/attenuators", self.atten_edit.text())
        s.setValue(f"{self.KEY}/custom", self.custom.isChecked())
        s.setValue(f"{self.KEY}/steps", self.steps_edit.text())
        s.setValue(f"{self.KEY}/step_seconds", self.step_time.value())
        s.setValue(f"{self.KEY}/stop_with_sweep", self.stop_with_sweep.isChecked())


def _duration(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m" if h else (f"{m}m {s:02d}s" if m else f"{s}s")
