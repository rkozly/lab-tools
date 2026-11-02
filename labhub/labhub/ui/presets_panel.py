"""
Preset panel (left side): presets grouped by category, with choice buttons
(e.g. channel A/B/C) and a delay box for sequences. Plus the preset editor.
"""

from __future__ import annotations

import re

import yaml
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QMenu, QMessageBox,
                               QPlainTextEdit, QPushButton, QScrollArea,
                               QSpinBox, QVBoxLayout, QWidget)

from ..core.presets import Preset, PresetError, parse_preset, preset_to_dict


class PresetRow(QFrame):
    activated = Signal(object, dict, object)  # preset, params, delay_ms|None
    edit = Signal(object)
    delete = Signal(object)
    duplicate = Signal(object)

    def __init__(self, preset: Preset) -> None:
        super().__init__()
        self.preset = preset
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 4)
        lay.setSpacing(3)
        top = QHBoxLayout()
        top.setSpacing(4)
        kind = {"sequence": "⇶ ", "monitor": "◉ "}.get(preset.kind, "")
        btn = QPushButton(f"{kind}{preset.name}")
        btn.setStyleSheet("text-align: left; padding-left: 8px;")
        btn.setToolTip(self._tip())
        roles = ", ".join(preset.roles) if preset.roles else "any"
        btn.setContextMenuPolicy(Qt.CustomContextMenu)
        btn.customContextMenuRequested.connect(lambda pos: self._menu(btn.mapToGlobal(pos)))
        top.addWidget(btn, 1)
        self.delay = None
        if preset.kind == "sequence":
            self.delay = QSpinBox()
            self.delay.setRange(0, 60000)
            self.delay.setSingleStep(100)
            self.delay.setSuffix(" ms")
            self.delay.setValue(preset.sequence.delay_ms)
            self.delay.setToolTip("Delay after each prompt, before the next key is sent")
            self.delay.setFixedWidth(84)
            top.addWidget(self.delay)
        lay.addLayout(top)

        choice_params = {k: v for k, v in preset.params.items() if v.choices}
        if choice_params:
            # One row of buttons per parameter; clicking a button runs the preset.
            for pname, param in choice_params.items():
                row = QHBoxLayout()
                row.setSpacing(4)
                lbl = QLabel(pname)
                lbl.setObjectName("Muted")
                row.addWidget(lbl)
                for label, value in param.choices.items():
                    b = QPushButton(label)
                    b.setObjectName("Choice")
                    b.setToolTip(f"{pname} = {value}")
                    b.clicked.connect(lambda _=False, p=pname, v=value: self._fire({p: v}))
                    row.addWidget(b)
                row.addStretch(1)
                lay.addLayout(row)
            btn.clicked.connect(lambda: self._fire({k: (v.default or next(iter(v.choices.values())))
                                                    for k, v in choice_params.items()}))
            btn.setToolTip(self._tip() + "<br><i>Click a choice button to run.</i>")
        else:
            btn.clicked.connect(lambda: self._fire({}))
        _ = roles

    def _tip(self) -> str:
        p = self.preset
        lines = [f"<b>{p.name}</b>  <i>({p.source}, for {', '.join(p.roles) or 'any'})</i>"]
        if p.kind == "sequence":
            s = p.sequence
            lines.append(f"Starts <code>{s.start}</code> and types: {' → '.join(s.steps)}")
        elif p.kind == "monitor":
            lines.append(f"Live dashboard (every {p.monitor.interval_s:g} s) – opens the Monitor tab")
        else:
            body = p.script if len(p.script) < 400 else p.script[:400] + " …"
            lines.append("<pre>" + body.replace("&", "&amp;").replace("<", "&lt;") + "</pre>")
        lines.append("Right-click to edit / duplicate / delete")
        return "<br>".join(lines)

    def _fire(self, params: dict) -> None:
        self.activated.emit(self.preset, params, self.delay.value() if self.delay else None)

    def _menu(self, pos) -> None:
        m = QMenu(self)
        m.addAction("Edit…", lambda: self.edit.emit(self.preset))
        m.addAction("Duplicate…", lambda: self.duplicate.emit(self.preset))
        if self.preset.source == "user":
            m.addAction("Delete" if self.preset.source == "user" else "", lambda: self.delete.emit(self.preset))
        m.exec(pos)


class PresetPanel(QWidget):
    activated = Signal(object, dict, object)
    changed = Signal()  # presets were edited

    def __init__(self, ctx) -> None:
        super().__init__()
        self.ctx = ctx
        self.setMinimumWidth(250)
        self.setMaximumWidth(340)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 6, 8)
        lay.setSpacing(6)
        head = QHBoxLayout()
        t = QLabel("PRESETS")
        t.setObjectName("Section")
        head.addWidget(t)
        head.addStretch(1)
        add = QPushButton("+ New")
        add.setObjectName("Chip")
        add.clicked.connect(lambda: self.edit_preset(None))
        head.addWidget(add)
        lay.addLayout(head)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter presets…")
        self.search.textChanged.connect(self.rebuild)
        lay.addWidget(self.search)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        lay.addWidget(self.scroll, 1)
        self.rebuild()

    def rebuild(self) -> None:
        host = QWidget()
        v = QVBoxLayout(host)
        v.setContentsMargins(0, 0, 4, 0)
        v.setSpacing(2)
        q = self.search.text().strip().lower()
        presets = [p for p in self.ctx.presets.all()
                   if not q or q in p.name.lower() or q in p.category.lower() or q in p.script.lower()]
        for cat in self.ctx.presets.categories():
            items = [p for p in presets if p.category == cat]
            if not items:
                continue
            lbl = QLabel(cat.upper())
            lbl.setObjectName("Section")
            lbl.setContentsMargins(4, 8, 0, 2)
            v.addWidget(lbl)
            for p in items:
                row = PresetRow(p)
                row.activated.connect(self.activated.emit)
                row.edit.connect(self.edit_preset)
                row.duplicate.connect(lambda pr: self.edit_preset(pr, duplicate=True))
                row.delete.connect(self.delete_preset)
                v.addWidget(row)
        if self.ctx.presets.errors:
            err = QLabel("⚠ " + "\n⚠ ".join(self.ctx.presets.errors))
            err.setWordWrap(True)
            err.setStyleSheet("color: #f0b43c;")
            v.addWidget(err)
        v.addStretch(1)
        self.scroll.setWidget(host)

    def edit_preset(self, preset: Preset | None, duplicate: bool = False) -> None:
        dlg = PresetEditor(self, preset, duplicate, existing={p.id for p in self.ctx.presets.all()})
        if dlg.exec() == QDialog.Accepted and dlg.result_raw:
            self.ctx.put_preset(dlg.result_raw)
            self.rebuild()
            self.changed.emit()

    def delete_preset(self, preset: Preset) -> None:
        what = "your edited copy (the built-in version comes back)" \
            if self.ctx.presets.is_overridden_builtin(preset.id) else f"'{preset.name}'"
        if QMessageBox.question(self, "Delete preset", f"Delete {what}?") == QMessageBox.Yes:
            self.ctx.presets.delete_user(preset.id)
            self.ctx.save_user_presets()
            self.rebuild()
            self.changed.emit()


TEMPLATE = """\
id: my-preset
name: My preset
category: General
roles: [cpe]            # ap / cpe / backbone; empty list = any
script: |
  iw ath0 link
# Choice buttons (use {channel} in the script):
# params:
#   channel:
#     choices: { A: 26660, B: 27000, C: 27340 }
#
# Menu sequence instead of a script:
# kind: sequence
# sequence: { start: QCMAP_CLI, steps: ["1", "2"], prompt: '[:>]\\s*$', delay_ms: 500, step_timeout_s: 15 }
#
# Placeholders: {name} (CPE1)  {n} (1)  {id}  {serial}
"""


class PresetEditor(QDialog):
    """Edits a preset as YAML: everything the file format supports, in one box."""

    def __init__(self, parent, preset: Preset | None, duplicate: bool, existing: set[str]) -> None:
        super().__init__(parent)
        self.setWindowTitle("New preset" if preset is None else ("Duplicate preset" if duplicate else "Edit preset"))
        self.resize(640, 520)
        self.result_raw: dict | None = None
        self.existing = existing
        self.original_id = None if (preset is None or duplicate) else preset.id
        lay = QVBoxLayout(self)
        info = QLabel("Saved to your user file. Editing a built-in preset saves your own copy, "
                      "which replaces the built-in one for you.")
        info.setObjectName("Muted")
        info.setWordWrap(True)
        lay.addWidget(info)
        self.text = QPlainTextEdit()
        self.text.setStyleSheet("font-family: Consolas; font-size: 10pt;")
        if preset is None:
            self.text.setPlainText(TEMPLATE)
        else:
            raw = preset_to_dict(preset)
            if duplicate:
                raw["id"] = self._free_id(raw["id"] + "-copy")
                raw["name"] = raw["name"] + " (copy)"
            self.text.setPlainText(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True, width=200))
        lay.addWidget(self.text, 1)
        self.error = QLabel()
        self.error.setStyleSheet("color: #ff6b7a;")
        self.error.setWordWrap(True)
        lay.addWidget(self.error)
        box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        box.accepted.connect(self._save)
        box.rejected.connect(self.reject)
        lay.addWidget(box)

    def _free_id(self, base: str) -> str:
        pid, i = base, 2
        while pid in self.existing:
            pid, i = f"{base}{i}", i + 1
        return pid

    def _save(self) -> None:
        try:
            raw = yaml.safe_load(self.text.toPlainText())
            if not isinstance(raw, dict):
                raise PresetError("the text must be a YAML mapping (key: value lines)")
            raw["id"] = re.sub(r"\s+", "-", str(raw.get("id", "")).strip())
            if self.original_id is None and raw["id"] in self.existing:
                raise PresetError(f"id '{raw['id']}' is already used; choose another id")
            parse_preset(raw, "user")
        except (yaml.YAMLError, PresetError) as e:
            self.error.setText(str(e))
            return
        self.result_raw = raw
        self.accept()
