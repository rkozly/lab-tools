"""
Command bar (target chooser + command input) and the Console tab
(one live output pane per device).
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QKeyEvent, QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
                               QPushButton, QScrollArea, QToolButton,
                               QVBoxLayout, QWidget)

from ..core.runner import Job, Run
from ..core.targets import QUICK_TARGETS
from .common import JOB_COLOR, JOB_ICON


SHELL_MIN_H = 420     # px; shells grow taller still when the window has room
COLUMN_GAP = 36       # px between the two shell columns: the mouse wheel there scrolls the page


class CommandBar(QFrame):
    run_requested = Signal(str, list)  # command, targets
    stop_all = Signal()
    open_shells = Signal(list)         # targets
    reboot_requested = Signal(list)    # targets

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("CommandBar")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 10)
        lay.setSpacing(6)

        row1 = QHBoxLayout()
        lbl = QLabel("TARGET")
        lbl.setObjectName("Section")
        row1.addWidget(lbl)
        self.target = QComboBox()
        self.target.setMinimumWidth(170)
        self.target.currentIndexChanged.connect(self._update_hint)
        row1.addWidget(self.target)
        self.hint = QLabel()
        self.hint.setObjectName("Muted")
        row1.addWidget(self.hint, 1)
        lay.addLayout(row1)

        row2 = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setObjectName("CommandInput")
        self.input.setPlaceholderText("Command for ALL target shells, Enter to send   (↑/↓ history)  –  "
                                      "or type in a device's own shell below")
        self.input.returnPressed.connect(self._run)
        row2.addWidget(self.input, 1)
        run = QPushButton("▶  Run")
        run.setObjectName("Primary")
        run.clicked.connect(self._run)
        row2.addWidget(run)
        self.preset_btn = QPushButton("Presets ▾")
        self.preset_btn.setToolTip("Run a preset on the target devices")
        self.preset_btn.clicked.connect(self._show_presets)
        row2.insertWidget(1, self.preset_btn)
        self.auto_shells = QCheckBox("Shell per device")
        self.auto_shells.setChecked(True)
        self.auto_shells.setToolTip("Open a live shell for every device you select (adb root + adb shell).\n"
                                    "Run and presets are typed into those shells.")
        row1.addWidget(self.auto_shells)
        shells = QPushButton("⌨  Open shells")
        shells.setToolTip("Open a live shell per target device in the Console.\n"
                          "While a shell is open, Run and presets are typed into it.")
        shells.clicked.connect(lambda: self.open_shells.emit(self.targets()))
        row2.addWidget(shells)
        reboot = QPushButton("⏻  Reboot all")
        reboot.setToolTip("Reboot every AP and CPE of the lab (never the backbones), whatever is selected.\n"
                          "A running menu (QCMAP_CLI) is left with Ctrl+C / Ctrl+Z first, then 'reboot' is typed.")
        reboot.clicked.connect(lambda: self.reboot_requested.emit(["kind:device"]))
        row2.addWidget(reboot)
        stop = QPushButton("■  Stop all")
        stop.setObjectName("Danger")
        stop.clicked.connect(self.stop_all.emit)
        row2.addWidget(stop)
        lay.addLayout(row2)

        self.history: list[str] = []
        self._hist_pos = 0
        for key, step in ((Qt.Key_Up, -1), (Qt.Key_Down, 1)):
            sc = QShortcut(QKeySequence(key), self.input)
            sc.setContext(Qt.WidgetShortcut)
            sc.activated.connect(lambda s=step: self._history(s))
        self._resolve = lambda targets: []
        self.set_groups([])

    def set_preset_menu(self, build) -> None:
        self._build_menu = build

    def _show_presets(self) -> None:
        if getattr(self, "_build_menu", None):
            menu = self._build_menu()
            menu.exec(self.preset_btn.mapToGlobal(self.preset_btn.rect().bottomLeft()))

    def set_resolver(self, fn) -> None:
        """fn(targets) -> list of node names (for the hint)."""
        self._resolve = fn
        self._update_hint()

    def set_groups(self, groups: list[str]) -> None:
        cur = self.target.currentData()
        self.target.blockSignals(True)
        self.target.clear()
        for label, t in QUICK_TARGETS:
            self.target.addItem(label, t)
        for g in groups:
            self.target.addItem(f"Group: {g}", f"group:{g}")
        i = self.target.findData(cur) if cur else 0
        self.target.setCurrentIndex(max(0, i))
        self.target.blockSignals(False)
        self._update_hint()

    def targets(self) -> list[str]:
        return [self.target.currentData() or "selected"]

    def target_label(self) -> str:
        return self.target.currentText()

    def _update_hint(self) -> None:
        try:
            names = self._resolve(self.targets())
        except Exception as e:
            self.hint.setText(f"  ⚠ {e}")
            return
        if not names:
            self.hint.setText("  → nobody (select tiles above, or pick a target)")
        else:
            shown = ", ".join(names[:12]) + (f" +{len(names) - 12}" if len(names) > 12 else "")
            self.hint.setText(f"  → {shown}")

    def refresh_hint(self) -> None:
        self._update_hint()

    def _run(self) -> None:
        cmd = self.input.text().strip()
        if not cmd:
            return
        if not self.history or self.history[-1] != cmd:
            self.history.append(cmd)
        self._hist_pos = len(self.history)
        self.run_requested.emit(cmd, self.targets())
        self.input.selectAll()

    def _history(self, step: int) -> None:
        if not self.history:
            return
        self._hist_pos = max(0, min(len(self.history), self._hist_pos + step))
        self.input.setText(self.history[self._hist_pos] if self._hist_pos < len(self.history) else "")


class OutputPane(QFrame):
    stop = Signal(object)  # Job

    def __init__(self, job: Job, run: Run) -> None:
        super().__init__()
        self.setObjectName("Pane")
        self.job, self.run = job, run
        self.setMinimumHeight(180)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        head = QFrame()
        head.setObjectName("PaneHeader")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(10, 4, 4, 4)
        self.title = QLabel()
        self.title.setStyleSheet("background: transparent; font-weight: 600;")
        hl.addWidget(self.title)
        self.state = QLabel()
        self.state.setStyleSheet("background: transparent;")
        hl.addWidget(self.state, 1)
        for text, tip, fn in (("■", "Stop", lambda: self.stop.emit(self.job)),
                              ("⌫", "Clear", self.clear),
                              ("⇩", "Export this output", self.export)):
            b = QToolButton()
            b.setObjectName("Mini")
            b.setText(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            hl.addWidget(b)
            if text == "■":
                self.stop_btn = b
        lay.addWidget(head)
        self.text = QPlainTextEdit()
        self.text.setObjectName("PaneText")
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.text.setMaximumBlockCount(20000)
        lay.addWidget(self.text, 1)
        self._pending: list[str] = []
        self.set_job(job, run)

    def set_job(self, job: Job, run: Run) -> None:
        self.job, self.run = job, run
        self.title.setText(job.node_name)
        self.text.appendPlainText(f"$ {run.label}")
        self.text.appendPlainText("")
        self.update_state()

    def update_state(self) -> None:
        j = self.job
        color = JOB_COLOR.get(j.status, "#8a90a0")
        extra = f"  {j.message}" if j.message and j.status != "running" else ""
        self.state.setText(f"  <span style='color:{color}'>{JOB_ICON.get(j.status, '')} {j.status}</span>"
                           f"<span style='color:#8a90a0'>{extra}</span>")
        self.stop_btn.setEnabled(j.active)

    def append(self, text: str) -> None:
        self._pending.append(text)

    def flush(self) -> None:
        if not self._pending:
            return
        text = "".join(self._pending)
        self._pending.clear()
        bar = self.text.verticalScrollBar()
        at_end = bar.value() >= bar.maximum() - 4
        cur = self.text.textCursor()
        cur.movePosition(QTextCursor.End)
        cur.insertText(text)
        if at_end:
            bar.setValue(bar.maximum())

    def clear(self) -> None:
        self.text.clear()

    def export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export output", f"{self.job.node_name}.log",
                                              "Log files (*.log *.txt);;All files (*)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.text.toPlainText() + "\n")


class ShellPane(QFrame):
    """A live interactive shell for one node: output on top, input line below.
    AP / CPE panes also get a row of quick-command buttons."""
    send = Signal(str, str)        # node id, text
    interrupt = Signal(str)
    key = Signal(str, str)         # node id, control character
    close_req = Signal(str)
    reconnect = Signal(str)
    quick = Signal(str, str)       # node id, quick command key
    quick_edit = Signal(str)       # quick command key
    addr_refresh = Signal(str)     # node id

    def __init__(self, session, title: str = "", quick: list | None = None, addr_bar: bool = False) -> None:
        super().__init__()
        self.setObjectName("Pane")
        self.setMinimumHeight(SHELL_MIN_H)
        self.node_id = session.node_id
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        head = QFrame()
        head.setObjectName("PaneHeader")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(10, 4, 4, 4)
        self.title = QLabel(f"⌨ {title or session.name}")
        self.title.setStyleSheet("background: transparent; font-weight: 600;")
        hl.addWidget(self.title)
        self.state = QLabel()
        self.state.setStyleSheet("background: transparent;")
        hl.addWidget(self.state, 1)
        for text, tip, fn in (("^C", "Send Ctrl+C (stop the running command)", lambda: self.interrupt.emit(self.node_id)),
                              ("^Z", "Send Ctrl+Z (suspend / leave the menu)", lambda: self.key.emit(self.node_id, "\x1a")),
                              ("⌫", "Clear", lambda: self.text.clear()),
                              ("⇩", "Export this output", self.export),
                              ("⟳", "Reconnect", lambda: self.reconnect.emit(self.node_id)),
                              ("✕", "Close shell", lambda: self.close_req.emit(self.node_id))):
            b = QToolButton()
            b.setObjectName("Mini")
            b.setText(text)
            b.setToolTip(tip)
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(fn)
            hl.addWidget(b)
        lay.addWidget(head)
        if addr_bar:
            ab = QFrame()
            ab.setObjectName("AddrBar")
            al = QHBoxLayout(ab)
            al.setContentsMargins(10, 3, 4, 3)
            self.addr = QLabel("IPv6: checking…")
            self.addr.setObjectName("Muted")
            self.addr.setTextFormat(Qt.RichText)
            self.addr.setStyleSheet("background: transparent;")
            self.addr.setTextInteractionFlags(Qt.TextSelectableByMouse)
            al.addWidget(self.addr, 1)
            b = QToolButton()
            b.setObjectName("Mini")
            b.setText("⟳")
            b.setToolTip("Check the IPv6 addresses of the control (.960) / data (AP v10371015, CPE .1015) interfaces again")
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(lambda: self.addr_refresh.emit(self.node_id))
            al.addWidget(b)
            lay.addWidget(ab)
        if quick:
            bar = QFrame()
            bar.setObjectName("QuickBar")
            ql = QHBoxLayout(bar)
            ql.setContentsMargins(8, 4, 8, 4)
            ql.setSpacing(4)
            self.quick_buttons: dict[str, QPushButton] = {}
            for qc in quick:
                b = QPushButton(qc.label)
                b.setObjectName("Quick")
                b.setFocusPolicy(Qt.NoFocus)
                b.setContextMenuPolicy(Qt.CustomContextMenu)
                b.clicked.connect(lambda _=False, k=qc.key: self.quick.emit(self.node_id, k))
                b.customContextMenuRequested.connect(lambda _p, k=qc.key: self.quick_edit.emit(k))
                ql.addWidget(b)
                self.quick_buttons[qc.key] = b
            ql.addStretch(1)
            lay.addWidget(bar)
        self.text = QPlainTextEdit()
        self.text.setObjectName("PaneText")
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.text.setMaximumBlockCount(20000)
        lay.addWidget(self.text, 1)
        self.input = QLineEdit()
        self.input.setObjectName("CommandInput")
        self.input.setPlaceholderText(f"{session.name} ›  type here, Enter sends  ·  Ctrl+C / Ctrl+Z / Ctrl+D "
                                      "go to the device  ·  ↑/↓ history")
        self.input.setMinimumHeight(30)
        self.input.returnPressed.connect(self._send)
        lay.addWidget(self.input)
        # Keys are handled per pane (window-wide shortcuts would clash between panes).
        self.input.installEventFilter(self)
        self.text.installEventFilter(self)
        self.text.viewport().installEventFilter(self)
        self._last_output = 0.0
        self.history: list[str] = []
        self._pos = 0
        self._pending: list[tuple[str, bool]] = []
        self.set_session(session)

    def set_session(self, session) -> None:
        self.session = session
        self.update_state()

    def _ctrl_c(self) -> None:
        if self.input.hasSelectedText():
            self.input.copy()
        else:
            self.interrupt.emit(self.node_id)

    def _send(self) -> None:
        cmd = self.input.text()
        if cmd.strip() and (not self.history or self.history[-1] != cmd):
            self.history.append(cmd)
        self._pos = len(self.history)
        self.input.clear()
        self.send.emit(self.node_id, cmd)
        import time
        sent_at = time.monotonic()
        QTimer.singleShot(4000, lambda: self._check_answer(sent_at))

    def _check_answer(self, sent_at: float) -> None:
        if self.session.status == "open" and self._last_output < sent_at:
            self.note("no answer from the device yet. If nothing appears, press ⟳ to reconnect.")

    def eventFilter(self, obj, e) -> bool:
        t = e.type()
        if t == QEvent.KeyPress:
            ctrl = bool(e.modifiers() & Qt.ControlModifier)
            if ctrl and e.key() == Qt.Key_C:
                sel = self.input.hasSelectedText() if obj is self.input else self.text.textCursor().hasSelection()
                if sel:
                    return False  # normal copy
                self.interrupt.emit(self.node_id)
                return True
            ctrl_keys = {Qt.Key_Z: "\x1a", Qt.Key_D: "\x04", Qt.Key_Backslash: "\x1c"}
            if ctrl and e.key() in ctrl_keys:
                self.key.emit(self.node_id, ctrl_keys[e.key()])   # not "undo": goes to the device
                return True
            if obj is self.input:
                if e.key() == Qt.Key_Up:
                    self._history(-1)
                    return True
                if e.key() == Qt.Key_Down:
                    self._history(1)
                    return True
                return False
            # typing in the output area goes to the input line
            if ctrl and e.key() in (Qt.Key_A, Qt.Key_Insert):
                return False
            if e.text() or e.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Backspace, Qt.Key_Up, Qt.Key_Down):
                self.input.setFocus()
                if e.key() in (Qt.Key_Up, Qt.Key_Down):
                    self._history(-1 if e.key() == Qt.Key_Up else 1)
                else:
                    QApplication.sendEvent(self.input, QKeyEvent(QEvent.KeyPress, e.key(), e.modifiers(), e.text()))
                return True
        elif t == QEvent.MouseButtonRelease and obj is self.text.viewport():
            if not self.text.textCursor().hasSelection():
                self.input.setFocus()
        return False

    def _history(self, step: int) -> None:
        if self.history:
            self._pos = max(0, min(len(self.history), self._pos + step))
            self.input.setText(self.history[self._pos] if self._pos < len(self.history) else "")

    def update_state(self) -> None:
        s = self.session
        color = {"open": "#3ccf7a", "starting": "#2f6fed"}.get(s.status, "#ff6b7a")
        label = {"open": "● connected", "starting": "… connecting"}.get(s.status, f"✖ {s.message or 'closed'}")
        self.state.setText(f"  <span style='color:{color}'>{label}</span>")
        self.input.setEnabled(s.status in ("open", "starting"))

    def append(self, text: str, clear: bool) -> None:
        import time
        self._last_output = time.monotonic()
        self._pending.append((text, clear))

    def flush(self) -> None:
        if not self._pending:
            return
        items, self._pending = self._pending, []
        last_clear = max((i for i, (_, c) in enumerate(items) if c), default=-1)
        if last_clear >= 0:
            self.text.clear()
            items = items[last_clear:]
        bar = self.text.verticalScrollBar()
        at_end = last_clear >= 0 or bar.value() >= bar.maximum() - 4
        cur = self.text.textCursor()
        cur.movePosition(QTextCursor.End)
        cur.insertText("".join(t for t, _ in items))
        if last_clear >= 0:
            bar.setValue(0)
        elif at_end:
            bar.setValue(bar.maximum())

    def note(self, text: str) -> None:
        self._pending.append((f"\n[LabHub] {text}\n", False))

    def set_addrs(self, html: str, tip: str = "") -> None:
        """IPv6 line of a BB pane: green = interface has an address, red = none."""
        if hasattr(self, "addr"):
            self.addr.setText(html)
            self.addr.setToolTip(tip)

    QUICK_STYLE = {None: "border-color: #2f6fed; color: #9fc0ff;",
                   True: "border-color: #2f8f55; color: #3ccf7a;",
                   False: "border-color: #6b2b35; color: #ff6b7a;"}

    def set_quick_state(self, key: str, ok: bool | None, tip: str = "") -> None:
        """Colour a quick button: None = running, True = pass, False = fail."""
        b = getattr(self, "quick_buttons", {}).get(key)
        if b is None:
            return
        b.setStyleSheet(self.QUICK_STYLE.get(ok, ""))
        if tip:
            b.setToolTip(tip)

    def set_quick_tips(self, tips: dict[str, str]) -> None:
        for key, tip in tips.items():
            b = getattr(self, "quick_buttons", {}).get(key)
            if b is not None:
                b.setToolTip(tip + "\n\nRight-click to edit the command.")

    def export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export shell output", f"{self.session.name}_shell.log",
                                              "Log files (*.log *.txt);;All files (*)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.text.toPlainText() + "\n")


class StatsView(QWidget):
    """Stats tab: one live shell per AP / CPE that is open in the Console,
    running that role's watch command. Two columns, AP first."""
    restart = Signal(str)          # device id
    interrupt = Signal(str)        # stats session key
    restart_all = Signal()
    stop_all = Signal()

    def __init__(self) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        bar = QHBoxLayout()
        bar.addStretch(1)
        for text, fn in (("⟳ Restart all", self.restart_all.emit), ("■ Stop all", self.stop_all.emit)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            bar.addWidget(b)
        lay.addLayout(bar)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        host = QWidget()
        self.grid = QGridLayout(host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(8)
        self.grid.setHorizontalSpacing(COLUMN_GAP)   # room to scroll the page between the columns
        scroll.setWidget(host)
        lay.addWidget(scroll, 1)
        self.empty = QLabel("Open AP / CPE shells in the Console: their stats show up here.")
        self.empty.setObjectName("Muted")
        self.empty.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.empty)
        self.panes: dict[str, ShellPane] = {}   # device id -> pane
        self._timer = QTimer(self, interval=80, timeout=lambda: [p.flush() for p in self.panes.values()])
        self._timer.start()

    def add(self, device_id: str, session, title: str) -> ShellPane:
        pane = self.panes.get(device_id)
        if pane is None:
            pane = ShellPane(session, title)
            pane.input.hide()                      # read-only: the watch runs by itself
            for b in pane.findChildren(QToolButton):
                if b.text() in ("^Z", "✕"):         # no menu here; the pane follows the Console shell
                    b.hide()
            pane.reconnect.connect(lambda _k, d=device_id: self.restart.emit(d))
            pane.interrupt.connect(self.interrupt.emit)
            self.panes[device_id] = pane
        elif pane.session is not session:
            pane.set_session(session)
        return pane

    def remove(self, device_id: str) -> None:
        pane = self.panes.pop(device_id, None)
        if pane:
            pane.deleteLater()

    def relayout(self, order: list[str]) -> None:
        while self.grid.count():
            self.grid.takeAt(0)
        ids = [i for i in order if i in self.panes] + [i for i in self.panes if i not in order]
        for i, nid in enumerate(ids):
            self.grid.addWidget(self.panes[nid], i // 2, i % 2)
        rows = (len(ids) + 1) // 2
        for r in range(rows + 1):
            self.grid.setRowStretch(r, 1 if r < rows else 0)
        for c in range(2):
            self.grid.setColumnStretch(c, 1)
        self.empty.setVisible(not ids)

    def output(self, session, text: str, clear: bool) -> bool:
        pane = self.panes.get(session.device_id)
        if pane and pane.session is session:
            pane.append(text, clear)
            return True
        return False

    def state(self, session) -> bool:
        pane = self.panes.get(session.device_id)
        if pane and pane.session is session:
            pane.update_state()
            return True
        return False


class ConsoleView(QWidget):
    """Shells and output panes. Each device's shell sits in one row with the
    shell of its backbone next to it:  device | corresponding BB."""
    stop_job = Signal(object)
    export_run = Signal()
    shell_send = Signal(str, str)
    shell_interrupt = Signal(str)
    shell_key = Signal(str, str)
    shell_close = Signal(str)
    shell_reconnect = Signal(str)
    close_shells = Signal()
    quick = Signal(str, str)        # node id, quick command key
    quick_edit = Signal(str)
    addr_refresh = Signal(str)      # node id of a BB shell

    def __init__(self) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        bar = QHBoxLayout()
        bar.addStretch(1)
        for text, fn in (("Clear all", self.clear_all), ("Close output panes", self.close_all),
                         ("Close shells", self.close_shells.emit),
                         ("Export run…", self.export_run.emit)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            bar.addWidget(b)
        lay.addLayout(bar)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.host = QWidget()
        self.grid = QGridLayout(self.host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(8)
        self.grid.setHorizontalSpacing(COLUMN_GAP)   # room to scroll the page between the columns
        self.scroll.setWidget(self.host)
        lay.addWidget(self.scroll, 1)
        self.panes: dict[str, OutputPane] = {}   # node id -> pane
        self.shells: dict[str, ShellPane] = {}   # node id -> live shell
        self.by_job: dict[int, OutputPane] = {}
        self.last_run: Run | None = None
        self._layout_fn = lambda ids: [[i] for i in ids]   # shell ids -> rows
        self._timer = QTimer(self, interval=80, timeout=self._flush)
        self._timer.start()

    def set_row_layout(self, fn) -> None:
        """fn(list of shell node ids) -> rows, e.g. [["ap", "ap-bb"], ["cpe1"]]."""
        self._layout_fn = fn
        self._relayout()

    def start_run(self, run: Run) -> None:
        self.last_run = run
        for job in run.jobs:
            pane = self.panes.get(job.node_id)
            if pane is None:
                pane = OutputPane(job, run)
                pane.stop.connect(self.stop_job.emit)
                self.panes[job.node_id] = pane
            else:
                pane.text.appendPlainText("")
                pane.set_job(job, run)
            self.by_job[job.id] = pane
        self._relayout()

    def output(self, job: Job, text: str) -> None:
        pane = self.by_job.get(job.id)
        if pane:
            pane.append(text)

    def job_changed(self, job: Job) -> None:
        pane = self.by_job.get(job.id)
        if pane and pane.job is job:
            pane.update_state()
            if job.status == "skipped" or (job.status == "failed" and not job.output.text()):
                pane.append(f"[LabHub] {job.message}\n")

    def _flush(self) -> None:
        for p in self.panes.values():
            p.flush()
        for p in self.shells.values():
            p.flush()

    # --- shells -------------------------------------------------------------

    def add_shell(self, session, title: str = "", quick: list | None = None, addr_bar: bool = False) -> ShellPane:
        pane = self.shells.get(session.node_id)
        if pane is None:
            pane = ShellPane(session, title, quick, addr_bar)
            pane.addr_refresh.connect(self.addr_refresh.emit)
            pane.send.connect(self.shell_send.emit)
            pane.interrupt.connect(self.shell_interrupt.emit)
            pane.key.connect(self.shell_key.emit)
            pane.close_req.connect(self.shell_close.emit)
            pane.reconnect.connect(self.shell_reconnect.emit)
            pane.quick.connect(self.quick.emit)
            pane.quick_edit.connect(self.quick_edit.emit)
            self.shells[session.node_id] = pane
            self._relayout()
        elif pane.session is not session:
            pane.set_session(session)
            pane.note("reconnected")
        return pane

    def shell_output(self, session, text: str, clear: bool) -> None:
        pane = self.shells.get(session.node_id)
        if pane and pane.session is session:
            pane.append(text, clear)

    def shell_state(self, session) -> None:
        pane = self.shells.get(session.node_id)
        if pane and pane.session is session:
            pane.update_state()
            if session.status == "closed" and session.message not in ("", "closed"):
                pane.note(session.message)

    def remove_shell(self, node_id: str) -> None:
        pane = self.shells.pop(node_id, None)
        if pane:
            pane.deleteLater()
            self._relayout()

    def _relayout(self) -> None:
        while self.grid.count():
            self.grid.takeAt(0)
        rows = [[i for i in r if i in self.shells] for r in self._layout_fn(list(self.shells))]
        rows = [r for r in rows if r]
        placed = {i for r in rows for i in r}
        rows += [[i] for i in self.shells if i not in placed]
        # device | BB: two columns; a device without a BB in its row leaves the right side empty
        for r, ids in enumerate(rows):
            for c, nid in enumerate(ids[:2]):
                self.grid.addWidget(self.shells[nid], r, c)
        panes = list(self.panes.values())
        for i, pane in enumerate(panes):
            self.grid.addWidget(pane, len(rows) + i // 2, i % 2)
        total = len(rows) + (len(panes) + 1) // 2
        for r in range(total + 1):
            self.grid.setRowStretch(r, 1 if r < total else 0)
        for c in range(4):
            self.grid.setColumnStretch(c, 1 if c < 2 else 0)

    def clear_all(self) -> None:
        for p in self.panes.values():
            p.clear()

    def close_all(self) -> None:
        for p in self.panes.values():
            p.deleteLater()
        self.panes.clear()
        self.by_job.clear()
        self._relayout()
