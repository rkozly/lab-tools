"""Small reusable widgets used by the panels."""

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QVBoxLayout,
                               QWidget)

from ..tasks.base_task import State
from .theme import COLOR_GRAY, STATE_COLORS

STATE_TEXT = {
    State.IDLE: "Idle",
    State.WAITING: "Waiting",
    State.COLLECTING: "Collecting",
    State.STOPPING: "Stopping",
    State.STOPPED: "Stopped",
    State.TRANSLATING: "Translating",
    State.COMPLETED: "Completed",
    State.ERROR: "Error",
}


class StatusLabel(QLabel):
    """Label that shows a state in its color. Long messages go to the tooltip."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.set_state(State.IDLE)

    def set_state(self, state: str, message: str = "") -> None:
        text = message or STATE_TEXT.get(state, state)
        first_line = text.splitlines()[0] if text else ""
        self.setText(first_line)
        self.setToolTip(text)
        color = STATE_COLORS.get(state, STATE_COLORS[State.IDLE])
        self.setStyleSheet(f"color: {color};")


def _dialog_dir(path: str) -> str:
    """Start folder for file dialogs. Avoid probing network paths here."""
    folder = os.path.dirname(path.strip()) if path.strip() else ""
    return folder or os.path.expanduser("~")


class PathEdit(QWidget):
    """Line edit + Browse button. mode is 'save', 'open' or 'dir'."""

    changed = Signal(str)

    def __init__(self, path: str = "", mode: str = "save",
                 file_filter: str = "All files (*)", parent=None):
        super().__init__(parent)
        self.mode = mode
        self.file_filter = file_filter

        self.edit = QLineEdit(path)
        self.edit.textChanged.connect(self.changed)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.edit, 1)
        lay.addWidget(browse)

    def path(self) -> str:
        return self.edit.text().strip()

    def set_path(self, path: str) -> None:
        self.edit.setText(path)

    def _browse(self):
        current = self.path()
        if self.mode == "dir":
            chosen = QFileDialog.getExistingDirectory(self, "Select folder",
                                                      current or os.path.expanduser("~"))
        elif self.mode == "open":
            chosen, _ = QFileDialog.getOpenFileName(self, "Select file",
                                                    _dialog_dir(current), self.file_filter)
        else:
            chosen, _ = QFileDialog.getSaveFileName(self, "Save as",
                                                    current or _dialog_dir(current),
                                                    self.file_filter)
        if chosen:
            self.set_path(os.path.normpath(chosen))


class FolderFileEdit(QWidget):
    """Destination split into folder and file name, plus Browse.

    The full path is folder + file name. Both parts can be edited.
    """

    changed = Signal(str)

    def __init__(self, folder: str, filename: str,
                 file_filter: str = "All files (*)", parent=None):
        super().__init__(parent)
        self.file_filter = file_filter
        self.folder = QLineEdit(folder)
        self.name = QLineEdit(filename)
        self.name.setMinimumWidth(190)
        self.name.setMaximumWidth(260)
        for e in (self.folder, self.name):
            e.textChanged.connect(lambda _=None: self.changed.emit(self.path()))
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel("Folder:"))
        lay.addWidget(self.folder, 1)
        lay.addWidget(QLabel("File:"))
        lay.addWidget(self.name)
        lay.addWidget(browse)

    def path(self) -> str:
        folder, name = self.folder.text().strip(), self.name.text().strip()
        return os.path.join(folder, name) if folder else name

    def set_path(self, path: str) -> None:
        folder, name = os.path.split(path)
        self.folder.setText(folder)
        self.name.setText(name)

    def _browse(self):
        chosen, _ = QFileDialog.getSaveFileName(self, "Save as", self.path(),
                                                self.file_filter)
        if chosen:
            self.set_path(os.path.normpath(chosen))


class CheckGrid(QWidget):
    """Check boxes laid out in a grid (wraps after COLUMNS), plus Select All.

    Items can change at runtime (e.g. after a device refresh), so a setup
    with 1, 2, 8 or 16 CPEs only shows the boxes it needs.
    """

    toggled = Signal(str, bool)  # (item name, checked)
    COLUMNS = 8

    def __init__(self, select_all: bool = True, parent=None):
        super().__init__(parent)
        self._boxes: dict[str, QCheckBox] = {}
        self._grid = QGridLayout()
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(14)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(self._grid)
        self.select_all = None
        if select_all:
            self.select_all = QCheckBox("Select All")
            self.select_all.toggled.connect(self._toggle_all)
            lay.addSpacing(14)
            lay.addWidget(self.select_all, 0, Qt.AlignTop)
        lay.addStretch(1)

    def set_items(self, names: list[str], labels: dict[str, str] | None = None):
        """Show one box per name (in this order). Returns the newly added names."""
        labels = labels or {}
        for name in list(self._boxes):
            if name not in names:
                box = self._boxes.pop(name)
                box.setChecked(False)  # lets the owner hide the matching row
                self._grid.removeWidget(box)
                box.deleteLater()
        added = []
        for name in names:
            if name not in self._boxes:
                box = QCheckBox(labels.get(name, name))
                box.toggled.connect(lambda on, n=name: self._on_toggled(n, on))
                self._boxes[name] = box
                added.append(name)
        # Re-place every box so the order always matches `names`.
        for i, name in enumerate(names):
            self._grid.addWidget(self._boxes[name], i // self.COLUMNS, i % self.COLUMNS)
        if self.select_all:
            self.select_all.setVisible(len(names) > 1)
        self._sync_select_all()
        return added

    def checked(self) -> list[str]:
        return [n for n, b in self._boxes.items() if b.isChecked()]

    def is_checked(self, name: str) -> bool:
        return name in self._boxes and self._boxes[name].isChecked()

    def set_checked(self, name: str, on: bool) -> None:
        if name in self._boxes:
            self._boxes[name].setChecked(on)

    def _on_toggled(self, name: str, on: bool):
        self._sync_select_all()
        self.toggled.emit(name, on)

    def _toggle_all(self, on: bool):
        for box in self._boxes.values():
            box.setChecked(on)

    def _sync_select_all(self):
        if self.select_all:
            all_on = bool(self._boxes) and all(b.isChecked() for b in self._boxes.values())
            self.select_all.blockSignals(True)
            self.select_all.setChecked(all_on)
            self.select_all.blockSignals(False)


class CheckedRows(QWidget):
    """A CheckGrid plus one detail row per item, shown while its box is checked.

    row_factory(name) creates the row widget. Rows must provide
    is_running(); a row that is still running is kept even if its item
    disappears, so a refresh never interrupts a collection.
    """

    def __init__(self, row_factory, select_all: bool = True,
                 empty_text: str = "", parent=None):
        super().__init__(parent)
        self._factory = row_factory
        self.rows: dict[str, QWidget] = {}

        self.grid = CheckGrid(select_all)
        self.grid.toggled.connect(self._on_toggled)
        self.empty = QLabel(empty_text)
        self.empty.setStyleSheet(f"color: {COLOR_GRAY};")
        self.empty.setWordWrap(True)
        self._rows_box = QVBoxLayout()
        self._rows_box.setContentsMargins(0, 4, 0, 0)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.grid)
        lay.addWidget(self.empty)
        lay.addLayout(self._rows_box)

    def set_items(self, names: list[str], labels: dict[str, str] | None = None):
        """Update the item list. Returns the newly added names."""
        busy = [n for n, r in self.rows.items() if r.is_running() and n not in names]
        keep = list(names) + busy
        added = self.grid.set_items(keep, labels)

        for name in list(self.rows):
            if name not in keep:
                self.rows.pop(name).deleteLater()
        for name in keep:
            if name not in self.rows:
                row = self._factory(name)
                row.setVisible(False)
                self.rows[name] = row
        for name in keep:  # keep rows in the same order as the boxes
            self._rows_box.removeWidget(self.rows[name])
            self._rows_box.addWidget(self.rows[name])
        self.rows = {name: self.rows[name] for name in keep}

        self.empty.setVisible(not keep)
        return added

    def names(self) -> list[str]:
        return list(self.rows)

    def checked(self) -> list[str]:
        return [n for n in self.rows if self.grid.is_checked(n)]

    def _on_toggled(self, name: str, on: bool):
        row = self.rows.get(name)
        if row is not None:
            row.setVisible(on)
