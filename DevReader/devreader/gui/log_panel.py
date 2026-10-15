"""Integrated log panel (model, filtering, export, copy)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMenu,
    QTableView,
    QWidget,
)

from devreader.core.logging_service import Level, LogEntry, LogService
from devreader.gui.theme import COLORS
from devreader.gui.widgets import Card, button

LEVEL_COLORS = {
    Level.DEBUG: COLORS["debug"],
    Level.INFO: COLORS["text"],
    Level.SUCCESS: COLORS["success"],
    Level.WARNING: COLORS["warning"],
    Level.ERROR: COLORS["error"],
}
HEADERS = ["Time", "Level", "Device", "Message", "Command", "Result / Error"]
MAX_ENTRIES = 50_000


class LogBridge(QObject):
    """Marshals log entries from any thread onto the GUI thread."""

    entry = Signal(object)

    def __init__(self, service: LogService, parent=None) -> None:
        super().__init__(parent)
        service.add_listener(self.entry.emit)  # Qt queues cross-thread emits


class LogModel(QAbstractTableModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.entries: list[LogEntry] = []

    def append(self, entry: LogEntry) -> None:
        if len(self.entries) >= MAX_ENTRIES:
            drop = MAX_ENTRIES // 10
            self.beginRemoveRows(QModelIndex(), 0, drop - 1)
            del self.entries[:drop]
            self.endRemoveRows()
        row = len(self.entries)
        self.beginInsertRows(QModelIndex(), row, row)
        self.entries.append(entry)
        self.endInsertRows()

    def clear(self) -> None:
        self.beginResetModel()
        self.entries.clear()
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.entries)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return HEADERS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        e = self.entries[index.row()]
        col = index.column()
        if role == Qt.UserRole:
            return e
        if role == Qt.DisplayRole:
            return [
                e.timestamp.strftime("%H:%M:%S"),
                e.level.label,
                e.device,
                e.message,
                e.command,
                _one_line(e.error or e.result),
            ][col]
        if role == Qt.ToolTipRole:
            if col == 0:
                return e.timestamp.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            if col == 5:
                return "\n\n".join(x for x in (e.result, e.error) if x) or None
            return self.data(index, Qt.DisplayRole) or None
        if role == Qt.ForegroundRole:
            if col == 5 and e.error:
                return QColor(COLORS["error"] if e.level >= Level.WARNING else COLORS["muted"])
            if col in (4, 5):
                return QColor(COLORS["muted"])
            return QColor(LEVEL_COLORS[e.level])
        return None


def _one_line(text: str, limit: int = 300) -> str:
    flat = " ⏎ ".join(line.strip() for line in text.splitlines() if line.strip())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


class LogFilterProxy(QSortFilterProxyModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.min_level = Level.INFO
        self.text = ""

    def set_min_level(self, level: Level) -> None:
        self.min_level = level
        self.invalidateFilter()

    def set_text(self, text: str) -> None:
        self.text = text.strip().lower()
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent) -> bool:
        e: LogEntry = self.sourceModel().index(row, 0, parent).data(Qt.UserRole)
        if e.level < self.min_level:
            return False
        if self.text:
            hay = f"{e.device} {e.message} {e.command} {e.result} {e.error}".lower()
            return self.text in hay
        return True


class LogPanel(Card):
    def __init__(self, service: LogService, parent: QWidget | None = None) -> None:
        super().__init__("Logs", parent=parent)
        self.service = service
        self.model = LogModel(self)
        self.proxy = LogFilterProxy(self)
        self.proxy.setSourceModel(self.model)

        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter log…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.proxy.set_text)
        bar.addWidget(self.search, 1)
        self.level = QComboBox()
        for lvl in (Level.DEBUG, Level.INFO, Level.SUCCESS, Level.WARNING, Level.ERROR):
            self.level.addItem(f"{lvl.label}+" if lvl != Level.ERROR else "Error", lvl)
        self.level.setCurrentIndex(1)
        self.level.setToolTip("Minimum level to show (Debug shows polling commands)")
        self.level.currentIndexChanged.connect(lambda _: self.proxy.set_min_level(self.level.currentData()))
        bar.addWidget(self.level)
        self.autoscroll = QCheckBox("Auto-scroll")
        self.autoscroll.setChecked(True)
        bar.addWidget(self.autoscroll)
        self.copy_btn = button("Copy", tooltip="Copy selected entries (Ctrl+C)")
        self.export_btn = button("Export…", tooltip="Export the full log to a file")
        self.clear_btn = button("Clear", danger=True)
        for b in (self.copy_btn, self.export_btn, self.clear_btn):
            bar.addWidget(b)
        self.body.addLayout(bar)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(24)
        mono = QFont("Cascadia Mono, Consolas, monospace")
        mono.setStyleHint(QFont.Monospace)
        mono.setPointSize(9)
        self.table.setFont(mono)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        for col, width in enumerate((70, 70, 170, 360, 330)):
            hh.resizeSection(col, width)
        hh.setStretchLastSection(True)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.body.addWidget(self.table, 1)

        QShortcut(QKeySequence.Copy, self.table, activated=self.copy_selected)
        self.copy_btn.clicked.connect(self.copy_selected)
        self.export_btn.clicked.connect(self.export)
        self.clear_btn.clicked.connect(self.model.clear)

        self.bridge = LogBridge(service, self)
        self.bridge.entry.connect(self._append)

    def _append(self, entry: LogEntry) -> None:
        self.model.append(entry)
        if self.autoscroll.isChecked():
            self.table.scrollToBottom()

    def selected_entries(self) -> list[LogEntry]:
        rows = sorted({self.proxy.mapToSource(i).row() for i in self.table.selectionModel().selectedRows()})
        return [self.model.entries[r] for r in rows]

    def copy_selected(self) -> None:
        entries = self.selected_entries()
        if entries:
            QGuiApplication.clipboard().setText("\n".join(e.as_text() for e in entries))

    def export(self) -> None:
        default = f"devreader_{datetime.now():%Y%m%d_%H%M%S}.log"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export log", str(Path.home() / default), "Log files (*.log *.txt);;CSV (*.csv);;All files (*)"
        )
        if not path:
            return
        self.export_to(Path(path))

    def export_to(self, path: Path) -> None:
        try:
            if path.suffix.lower() == ".csv":
                import csv

                with path.open("w", newline="", encoding="utf-8") as fh:
                    w = csv.writer(fh)
                    w.writerow(["timestamp", "level", "device", "message", "command", "result", "error"])
                    for e in self.model.entries:
                        w.writerow([e.timestamp.isoformat(), e.level.name, e.device, e.message,
                                    e.command, e.result, e.error])
            else:
                path.write_text("\n".join(e.as_text() for e in self.model.entries) + "\n", encoding="utf-8")
            self.service.success(f"Log exported to {path}")
        except OSError as exc:
            self.service.error(f"Failed to export log: {exc}")

    def _context_menu(self, pos) -> None:
        menu = QMenu(self)
        menu.addAction("Copy selected", self.copy_selected)
        menu.addAction("Select all", self.table.selectAll)
        menu.addSeparator()
        menu.addAction("Export…", self.export)
        menu.addAction("Clear", self.model.clear)
        menu.exec(self.table.viewport().mapToGlobal(pos))
