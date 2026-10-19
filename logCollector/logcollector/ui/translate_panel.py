"""
PKTLOG Translation group (shown inside the PKTLOG Logging block).

  * CRM path is auto-detected (highest build number) and can be overridden.
  * Any number of .dat files can be added (Add Files..., or automatically
    when a capture finishes). Each file gets its own output name, which can
    be edited by double-clicking the cell.
  * "Translate" runs all checked files in parallel (see TranslateQueue).
"""

import os

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QGroupBox,
                               QHBoxLayout, QHeaderView, QLabel, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from ..config import CRM_ROOT, TRANSLATE_MAX_PARALLEL, translated_name_for
from ..core.crm import translator_dir
from ..tasks.base_task import State
from ..tasks.crm_scan_task import CrmScanTask
from ..tasks.translate_queue import TranslateQueue
from .theme import COLOR_GRAY, STATE_COLORS
from .widgets import STATE_TEXT, PathEdit, StatusLabel

COL_INPUT, COL_OUTPUT, COL_STATUS = range(3)

# States in which a file row may not be edited / removed / restarted.
_BUSY = (State.WAITING, State.TRANSLATING, State.STOPPING)


class TranslatePanel(QGroupBox):
    status_message = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__("PKTLOG Translation", parent)
        self._scan: CrmScanTask | None = None
        self.queue = TranslateQueue()
        self.queue.job_status.connect(self._on_job_status)
        self.queue.idle.connect(self._on_queue_idle)
        self._job_rows: dict[int, int] = {}  # job id -> table row

        # --- CRM path (auto detected, can be overridden) ---
        self.crm = PathEdit("", mode="dir")
        self.crm.edit.setPlaceholderText(f"Detecting latest CRM in {CRM_ROOT} ...")
        self.crm.changed.connect(self._update_tool_label)
        self.detect_btn = QPushButton("Re-detect")
        self.detect_btn.clicked.connect(self.detect_crm)
        self.tool_label = QLabel("Translator: -")
        self.tool_label.setStyleSheet(f"color: {COLOR_GRAY};")
        self.tool_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        crm_row = QHBoxLayout()
        crm_row.addWidget(QLabel("CRM path:"))
        crm_row.addWidget(self.crm, 1)
        crm_row.addWidget(self.detect_btn)

        # --- file list ---
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(
            ["Input file (.dat)", "Output file (.txt)", "Status"])
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked
                                   | QAbstractItemView.EditKeyPressed)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(COL_INPUT, QHeaderView.Stretch)
        hdr.setSectionResizeMode(COL_OUTPUT, QHeaderView.Stretch)
        hdr.setSectionResizeMode(COL_STATUS, QHeaderView.Interactive)
        self.table.setColumnWidth(COL_STATUS, 190)
        self.table.setMinimumHeight(130)

        add_btn = QPushButton("Add Files...")
        add_btn.clicked.connect(self._browse_inputs)
        out_btn = QPushButton("Output Folder...")
        out_btn.setToolTip("Put the output of the selected files (or all) in one folder")
        out_btn.clicked.connect(self._browse_output_folder)
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.clicked.connect(self._remove_selected)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._clear)

        self.translate_btn = QPushButton("Translate")
        self.translate_btn.clicked.connect(self.translate)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.queue.cancel_all)
        self.status = StatusLabel()

        file_btns = QHBoxLayout()
        for b in (add_btn, out_btn, self.remove_btn, clear_btn):
            file_btns.addWidget(b)
        file_btns.addStretch(1)
        file_btns.addWidget(QLabel(f"up to {TRANSLATE_MAX_PARALLEL} in parallel"))

        run_row = QHBoxLayout()
        run_row.addWidget(self.translate_btn)
        run_row.addWidget(self.cancel_btn)
        run_row.addWidget(self.status, 1)

        lay = QVBoxLayout(self)
        lay.addLayout(crm_row)
        lay.addWidget(self.tool_label)
        lay.addLayout(file_btns)
        lay.addWidget(self.table)
        lay.addLayout(run_row)

    # ----- CRM detection ----------------------------------------------------

    def detect_crm(self):
        if self._scan and self._scan.isRunning():
            return
        self.detect_btn.setEnabled(False)
        self.status.set_state(State.WAITING, "Detecting latest CRM...")
        self._scan = CrmScanTask()
        self._scan.result.connect(self.crm.set_path)
        self._scan.done.connect(self._detect_done)
        self._scan.start()

    def _detect_done(self, state, message):
        self.detect_btn.setEnabled(True)
        if state == State.ERROR:
            self.status.set_state(State.ERROR, message)
            self.status_message.emit(state, f"CRM detection: {message}")
        else:
            self.status.set_state(State.IDLE,
                                  f"Detected {os.path.basename(self.crm.path())}")

    def _update_tool_label(self, crm: str):
        crm = crm.strip()
        self.tool_label.setText(f"Translator: {translator_dir(crm)}" if crm
                                else "Translator: -")

    # ----- file list --------------------------------------------------------

    def add_file(self, input_file: str, output_file: str = "",
                 checked: bool = True) -> int:
        """Add a file (or update the row that already has this input)."""
        input_file = os.path.normpath(input_file)
        if not output_file:
            output_file = os.path.join(os.path.dirname(input_file),
                                       translated_name_for(os.path.basename(input_file)))
        row = self._find_row(input_file)
        if row is None:
            row = self.table.rowCount()
            self.table.insertRow(row)
            item = QTableWidgetItem(input_file)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            self.table.setItem(row, COL_INPUT, item)
            self.table.setItem(row, COL_OUTPUT, QTableWidgetItem(""))
            status = QTableWidgetItem("")
            status.setFlags(status.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, COL_STATUS, status)
        elif self._row_busy(row):
            return row
        self.table.item(row, COL_INPUT).setCheckState(
            Qt.Checked if checked else Qt.Unchecked)
        self.table.item(row, COL_OUTPUT).setText(output_file)
        self._set_row_state(row, State.IDLE, "Ready")
        return row

    def on_capture_completed(self, source: str, path: str):
        """A PKTLOG capture finished: queue its file for translation."""
        self.add_file(path)

    def _find_row(self, input_file: str) -> int | None:
        key = os.path.normcase(input_file)
        for r in range(self.table.rowCount()):
            if os.path.normcase(self.table.item(r, COL_INPUT).text()) == key:
                return r
        return None

    def _row_busy(self, row: int) -> bool:
        return self.table.item(row, COL_STATUS).data(Qt.UserRole) in _BUSY

    def _browse_inputs(self):
        start = ""
        if self.table.rowCount():
            start = os.path.dirname(self.table.item(self.table.rowCount() - 1,
                                                    COL_INPUT).text())
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select PKTLOG files", start or os.path.expanduser("~"),
            "PKTLOG files (*.dat);;All files (*)")
        for f in files:
            self.add_file(f)

    def _target_rows(self) -> list[int]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return rows or list(range(self.table.rowCount()))

    def _browse_output_folder(self):
        rows = [r for r in self._target_rows() if not self._row_busy(r)]
        if not rows:
            return
        current = os.path.dirname(self.table.item(rows[0], COL_OUTPUT).text())
        folder = QFileDialog.getExistingDirectory(self, "Output folder",
                                                  current or os.path.expanduser("~"))
        if not folder:
            return
        for r in rows:
            item = self.table.item(r, COL_OUTPUT)
            item.setText(os.path.join(os.path.normpath(folder),
                                      os.path.basename(item.text())))

    def _remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            if not self._row_busy(r):
                self._remove_row(r)

    def _clear(self):
        for r in reversed(range(self.table.rowCount())):
            if not self._row_busy(r):
                self._remove_row(r)

    def _remove_row(self, row: int):
        self.table.removeRow(row)
        # Job ids point at row numbers; shift the ones below the removed row.
        self._job_rows = {j: (r - 1 if r > row else r)
                          for j, r in self._job_rows.items() if r != row}

    def _set_row_state(self, row: int, state: str, message: str = ""):
        item = self.table.item(row, COL_STATUS)
        text = message or STATE_TEXT.get(state, state)
        item.setText(text.splitlines()[0])
        item.setToolTip(text)
        item.setData(Qt.UserRole, state)
        item.setForeground(QColor(STATE_COLORS.get(state, COLOR_GRAY)))
        editable = state not in _BUSY
        for col in (COL_INPUT, COL_OUTPUT):
            cell = self.table.item(row, col)
            flags = cell.flags()
            if col == COL_OUTPUT:
                flags = flags | Qt.ItemIsEditable if editable else flags & ~Qt.ItemIsEditable
            else:
                flags &= ~Qt.ItemIsEditable  # input is chosen via Add Files
            cell.setFlags(flags)

    # ----- translation ------------------------------------------------------

    def translate(self):
        crm = self.crm.path()
        if not crm:
            self.status.set_state(State.ERROR, "CRM path is empty")
            return
        rows = [r for r in range(self.table.rowCount())
                if self.table.item(r, COL_INPUT).checkState() == Qt.Checked
                and not self._row_busy(r)]
        if not rows:
            self.status.set_state(State.ERROR, "No files checked. Use Add Files...")
            return

        outputs = {}
        for r in rows:
            out = self.table.item(r, COL_OUTPUT).text().strip()
            if not out:
                self._set_row_state(r, State.ERROR, "Output file is empty")
                continue
            key = os.path.normcase(out)
            if key in outputs:
                self._set_row_state(r, State.ERROR, "Same output file as another row")
                continue
            outputs[key] = r

        for r in outputs.values():
            inp = self.table.item(r, COL_INPUT).text()
            out = self.table.item(r, COL_OUTPUT).text().strip()
            job = self.queue.submit(crm, inp, out, label=os.path.basename(inp))
            self._job_rows[job] = r
        self._update_summary()

    def _on_job_status(self, job_id: int, state: str, message: str):
        row = self._job_rows.get(job_id)
        if row is not None and row < self.table.rowCount():
            self._set_row_state(row, state, message)
            if state in (State.COMPLETED, State.ERROR, State.STOPPED):
                self._job_rows.pop(job_id, None)
                if state == State.ERROR:
                    name = os.path.basename(self.table.item(row, COL_INPUT).text())
                    self.status_message.emit(state, f"Translation {name}: {message}")
        self._update_summary()

    def _on_queue_idle(self):
        self._update_summary()

    def _update_summary(self):
        running, waiting = self.queue.counts()
        self.cancel_btn.setEnabled(running + waiting > 0)
        if running or waiting:
            msg = f"Translating {running} file(s)" + (f", {waiting} queued" if waiting else "")
            self.status.set_state(State.TRANSLATING, msg)
            self.status_message.emit(State.TRANSLATING, msg)
            return
        states = [self.table.item(r, COL_STATUS).data(Qt.UserRole)
                  for r in range(self.table.rowCount())]
        done, failed = states.count(State.COMPLETED), states.count(State.ERROR)
        if not (done or failed):
            self.status.set_state(State.IDLE)
            return
        if failed:
            msg = f"{done} translated, {failed} failed (hover a status for details)"
            self.status.set_state(State.ERROR, msg)
        else:
            msg = f"{done} file(s) translated"
            self.status.set_state(State.COMPLETED, msg)
        self.status_message.emit(State.ERROR if failed else State.COMPLETED,
                                 f"Translation: {msg}")

    # ----- settings ---------------------------------------------------------

    def save_settings(self, s: QSettings):
        # The CRM path is not saved: it is re-detected at every start so a
        # newer build is picked up automatically.
        files = []
        for r in range(self.table.rowCount()):
            files.append([self.table.item(r, COL_INPUT).text(),
                          self.table.item(r, COL_OUTPUT).text(),
                          self.table.item(r, COL_INPUT).checkState() == Qt.Checked])
        s.setValue("translate/files", files)

    def load_settings(self, s: QSettings):
        for entry in s.value("translate/files", []) or []:
            try:
                inp, out, checked = entry[0], entry[1], str(entry[2]).lower() == "true"
            except (IndexError, TypeError):
                continue
            if inp:
                self.add_file(inp, out, checked)
