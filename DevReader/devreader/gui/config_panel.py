"""Configuration editor generated dynamically from :class:`ConfigFileDef`."""

from __future__ import annotations

from PySide6.QtCore import QRegularExpression, Qt, Signal
from PySide6.QtGui import QDoubleValidator, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from devreader.config.parameters import ConfigFileDef, ParameterDef, ParamType
from devreader.config.table import TableDef
from devreader.core.discovery import Device
from devreader.core.errors import ConfigValidationError
from devreader.gui.replace_panel import FileReplacePanel
from devreader.gui.table_panel import ConfigTablePanel
from devreader.gui.theme import COLORS, repolish
from devreader.gui.widgets import Badge, Card, button


class ParameterField(QWidget):
    """Label + input + hint for one :class:`ParameterDef`. Widget type depends on ``param.type``."""

    changed = Signal()

    def __init__(self, param: ParameterDef, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.param = param
        self.original: str | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        self.label = QLabel(param.label)
        self.label.setObjectName("ParamLabel")
        self.label.setToolTip(param.description or param.id)
        lay.addWidget(self.label)

        self.input = self._make_input()
        lay.addWidget(self.input)

        self.hint = QLabel(self._default_hint())
        self.hint.setObjectName("ParamHint")
        self.hint.setWordWrap(True)
        lay.addWidget(self.hint)

    # -- widget factory ------------------------------------------------------
    def _make_input(self) -> QWidget:
        p = self.param
        if p.type in (ParamType.CHOICE, ParamType.BOOL):
            box = QComboBox()
            if p.type == ParamType.BOOL:
                box.addItem("Enabled (1)", "1")
                box.addItem("Disabled (0)", "0")
            else:
                for c in p.choices:
                    box.addItem(c, c)
            box.setCurrentIndex(-1)
            box.setEnabled(not p.read_only)
            box.currentIndexChanged.connect(self._on_changed)
            return box
        edit = QLineEdit()
        edit.setPlaceholderText(p.placeholder or ("Read from device…"))
        edit.setReadOnly(p.read_only)
        edit.setClearButtonEnabled(not p.read_only)
        if p.type == ParamType.INT:
            edit.setValidator(QRegularExpressionValidator(QRegularExpression(r"[-+]?\d{0,18}"), edit))
        elif p.type == ParamType.FLOAT:
            v = QDoubleValidator(edit)
            v.setNotation(QDoubleValidator.StandardNotation)
            edit.setValidator(v)
        elif p.pattern:
            edit.setValidator(QRegularExpressionValidator(QRegularExpression(p.pattern), edit))
        edit.textChanged.connect(self._on_changed)
        return edit

    def _default_hint(self) -> str:
        p = self.param
        bits = [f"{p.section}.{p.key}"]
        if p.minimum is not None and p.maximum is not None:
            bits.append(f"{_fmt(p.minimum)} – {_fmt(p.maximum)}")
        elif p.minimum is not None:
            bits.append(f"≥ {_fmt(p.minimum)}")
        elif p.maximum is not None:
            bits.append(f"≤ {_fmt(p.maximum)}")
        if p.unit:
            bits.append(p.unit)
        return " • ".join(bits)

    # -- value access --------------------------------------------------------
    def value(self) -> str:
        if isinstance(self.input, QComboBox):
            data = self.input.currentData()
            return "" if data is None else str(data)
        return self.input.text().strip()

    def set_value(self, value: str | None) -> None:
        self.input.blockSignals(True)
        if isinstance(self.input, QComboBox):
            idx = self.input.findData(value) if value is not None else -1
            if idx < 0 and value is not None and self.param.type == ParamType.CHOICE:
                self.input.addItem(f"{value} (device value)", value)
                idx = self.input.count() - 1
            self.input.setCurrentIndex(idx)
        else:
            self.input.setText(value or "")
        self.input.blockSignals(False)
        self._on_changed()

    def load(self, value: str | None) -> None:
        """Set the value read from the device (becomes the 'unmodified' baseline)."""
        self.original = value
        self.set_value(value)
        if value is None:
            self.hint.setText(f"⚠ Not present on device • {self._default_hint()}")

    def clear(self) -> None:
        self.original = None
        self.set_value(None)
        self.hint.setText(self._default_hint())

    @property
    def is_modified(self) -> bool:
        return self.original is not None and self.value() != self.original

    def validate(self) -> str:
        return self.param.validate(self.value())

    def revert(self) -> None:
        self.set_value(self.original)

    def _on_changed(self, *_):
        invalid = False
        if self.value():
            try:
                self.param.validate(self.value())
            except ConfigValidationError as exc:
                invalid = True
                self.hint.setText(f"⛔ {exc}")
        if not invalid:
            if self.is_modified:
                self.hint.setText(f"✎ Modified • device value: {self.original}")
            elif self.original is not None:
                self.hint.setText(self._default_hint())
        self.input.setProperty("modified", self.is_modified and not invalid)
        self.input.setProperty("invalid", invalid)
        repolish(self.input)
        self.changed.emit()


def _fmt(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else str(n)


class ConfigFileEditor(QWidget):
    """Form for one configuration file, with Read / Write actions."""

    read_requested = Signal(object, object)            # ConfigFileDef, Device
    write_requested = Signal(object, object, object)   # ConfigFileDef, values, [Device]

    def __init__(self, config: ConfigFileDef, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.fields: dict[str, ParameterField] = {}
        self._devices: list[Device] = []
        self._selected: list[Device] = []
        self._loaded_from: str | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(10)

        # Device picker row
        top = QHBoxLayout()
        lbl = QLabel("Device")
        lbl.setObjectName("Muted")
        top.addWidget(lbl)
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(240)
        self.device_combo.setToolTip("Device to read from / write to")
        self.device_combo.currentIndexChanged.connect(self._update_state)
        top.addWidget(self.device_combo, 1)
        self.path_label = QLabel(config.remote_path)
        self.path_label.setObjectName("Muted")
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        top.addWidget(self.path_label)
        self.status_badge = Badge("Not loaded", COLORS["muted"])
        top.addWidget(self.status_badge)
        lay.addLayout(top)

        # Parameter grid (grouped)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        grid_lay = QVBoxLayout(inner)
        grid_lay.setContentsMargins(0, 0, 4, 0)
        grid_lay.setSpacing(12)
        for group in config.group_order:
            params = [p for p in config.parameters if p.group == group]
            if not params:
                continue
            if len(config.group_order) > 1:
                g = QLabel(group.upper())
                g.setObjectName("Muted")
                g.setStyleSheet("font-weight: 700; letter-spacing: 1px; font-size: 8pt;")
                grid_lay.addWidget(g)
            grid = QGridLayout()
            grid.setHorizontalSpacing(18)
            grid.setVerticalSpacing(12)
            columns = 2
            for i, p in enumerate(params):
                f = ParameterField(p)
                f.changed.connect(self._update_state)
                self.fields[p.id] = f
                grid.addWidget(f, i // columns, i % columns)
            for c in range(columns):
                grid.setColumnStretch(c, 1)
            grid_lay.addLayout(grid)
        grid_lay.addStretch(1)
        scroll.setWidget(inner)
        lay.addWidget(scroll, 1)

        # Actions
        actions = QHBoxLayout()
        self.all_selected = QCheckBox("Write to all selected devices")
        self.all_selected.setToolTip("Apply the values to every checked, connected device in the Devices list")
        self.all_selected.toggled.connect(self._update_state)
        actions.addWidget(self.all_selected)
        actions.addStretch(1)
        self.revert_btn = button("Revert", tooltip="Discard edits and restore values read from the device")
        self.read_btn = button("⭳  Read Configuration", tooltip=f"Read {config.remote_path} from the device")
        self.write_btn = button("⭱  Write Configuration", primary=True,
                                tooltip="Update only the managed parameters, push, and verify")
        actions.addWidget(self.revert_btn)
        actions.addWidget(self.read_btn)
        actions.addWidget(self.write_btn)
        lay.addLayout(actions)

        self.read_btn.clicked.connect(self._emit_read)
        self.write_btn.clicked.connect(self._emit_write)
        self.revert_btn.clicked.connect(self._revert)
        self._busy = False
        self._update_state()

    # -- device list ---------------------------------------------------------
    def set_devices(self, devices: list[Device], selected: list[Device]) -> None:
        self._devices = [d for d in devices if d.is_ready]
        self._selected = [d for d in selected if d.is_ready]
        current = self.device_combo.currentData()
        preferred = current if current in {d.serial for d in self._devices} else None
        if preferred is None and self._selected:
            preferred = self._selected[0].serial
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for d in self._devices:
            self.device_combo.addItem(f"{d.name}  —  {d.serial}" if d.configured else d.serial, d.serial)
        if not self._devices:
            self.device_combo.addItem("No connected devices", None)
        idx = self.device_combo.findData(preferred) if preferred else 0
        self.device_combo.setCurrentIndex(max(idx, 0))
        self.device_combo.blockSignals(False)
        self._update_state()

    def current_device(self) -> Device | None:
        serial = self.device_combo.currentData()
        return next((d for d in self._devices if d.serial == serial), None)

    def write_targets(self) -> list[Device]:
        if self.all_selected.isChecked():
            return list(self._selected)
        dev = self.current_device()
        return [dev] if dev else []

    # -- values ----------------------------------------------------------------
    def load_values(self, serial: str, values: dict[str, str | None], missing: list[str]) -> None:
        self._loaded_from = serial
        for pid, f in self.fields.items():
            f.load(values.get(pid))
        dev = next((d for d in self._devices if d.serial == serial), None)
        name = dev.name if dev else serial
        if missing:
            self.status_badge.set(f"Loaded from {name} • {len(missing)} missing", COLORS["warning"])
        else:
            self.status_badge.set(f"Loaded from {name}", COLORS["success"])
        self._update_state()

    def mark_written(self, serial: str, verified: dict[str, str | None]) -> None:
        for pid, value in verified.items():
            if pid in self.fields and serial == self._loaded_from:
                self.fields[pid].load(value)
        dev = next((d for d in self._devices if d.serial == serial), None)
        self.status_badge.set(f"Written & verified on {dev.name if dev else serial}", COLORS["success"])
        self._update_state()

    def mark_error(self, text: str) -> None:
        self.status_badge.set(text, COLORS["error"])

    def collect(self) -> dict[str, str]:
        """Validated values of modified fields only. Raises ConfigValidationError."""
        out: dict[str, str] = {}
        errors = []
        for pid, f in self.fields.items():
            if f.param.read_only or f.original is None:
                continue
            try:
                value = f.validate()
            except ConfigValidationError as exc:
                errors.append(str(exc))
                continue
            if self.all_selected.isChecked() or value != f.original:
                out[pid] = value
        if errors:
            raise ConfigValidationError("\n".join(errors))
        return out

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_state()

    # -- internal ----------------------------------------------------------------
    def _emit_read(self) -> None:
        dev = self.current_device()
        if dev:
            self.read_requested.emit(self.config, dev)

    def _emit_write(self) -> None:
        self.write_requested.emit(self.config, None, self.write_targets())

    def _revert(self) -> None:
        for f in self.fields.values():
            f.revert()

    def _update_state(self, *_):
        dev = self.current_device()
        loaded = self._loaded_from is not None
        modified = any(f.is_modified for f in self.fields.values())
        if dev and loaded and dev.serial != self._loaded_from and not self.all_selected.isChecked():
            self.status_badge.set("Values are from another device - Read first", COLORS["warning"])
        self.read_btn.setEnabled(not self._busy and dev is not None)
        can_write = (not self._busy and loaded and bool(self.write_targets())
                     and (modified or self.all_selected.isChecked()))
        self.write_btn.setEnabled(can_write)
        self.revert_btn.setEnabled(not self._busy and modified)
        n = len(self.write_targets())
        self.write_btn.setText(f"⭱  Write Configuration ({n})" if self.all_selected.isChecked() else
                               "⭱  Write Configuration")


class ConfigPanel(Card):
    """Tabbed container: one editable per-device table per file, plus 'Replace File'."""

    def __init__(self, tables: tuple[TableDef, ...], parent: QWidget | None = None) -> None:
        super().__init__("Configuration", "Read and edit values on all devices at once", parent)
        self.tables: dict[str, ConfigTablePanel] = {}
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        for t in tables:
            panel = ConfigTablePanel(t)
            self.tables[t.id] = panel
            self.tabs.addTab(panel, t.title)
        self.replace_panel = FileReplacePanel()
        self.tabs.addTab(self.replace_panel, "Replace File")
        self.body.addWidget(self.tabs)

    def table(self, table_id: str) -> ConfigTablePanel:
        return self.tables[table_id]

    def set_devices(self, devices: list[Device], selected: list[Device]) -> None:
        for p in self.tables.values():
            p.set_devices(devices, selected)
        self.replace_panel.set_devices(devices, selected)

    def set_busy(self, busy: bool) -> None:
        for p in self.tables.values():
            p.set_busy(busy)
        self.replace_panel.set_busy(busy)
