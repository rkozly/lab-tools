"""
Main tabs, two side by side when wanted (like VS Code): right-click a tab →
"Move to the right side" / "Move to the left side". The right side shows only
when it has tabs; the divider between them can be dragged. Which tabs are on
the right (and the divider) is kept in the user file.

Works like one QTabWidget for the rest of LabHub: addTab, setCurrentWidget,
currentChanged, count / tabText; is_shown(w) tells whether a tab is in view on
either side.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QMenu, QSplitter, QTabWidget, QWidget


class SplitTabs(QWidget):
    currentChanged = Signal(int)

    def __init__(self, user=None) -> None:
        super().__init__()
        self.user = user
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.split = QSplitter(Qt.Horizontal)
        self.split.setChildrenCollapsible(False)
        self.split.splitterMoved.connect(lambda *_: self._save())
        self.panes = [QTabWidget(), QTabWidget()]
        for pane in self.panes:
            pane.setMovable(True)
            bar = pane.tabBar()
            bar.setContextMenuPolicy(Qt.CustomContextMenu)
            bar.customContextMenuRequested.connect(lambda pos, p=pane: self._menu(p, pos))
            pane.currentChanged.connect(lambda i: self.currentChanged.emit(i))
            self.split.addWidget(pane)
        self.panes[1].hide()
        lay.addWidget(self.split)
        self.titles: dict[QWidget, str] = {}

    # --- like a QTabWidget ---------------------------------------------------------

    def addTab(self, w: QWidget, title: str) -> int:
        self.titles[w] = title
        return self.panes[0].addTab(w, title)

    def _pane_of(self, w: QWidget) -> QTabWidget | None:
        return next((p for p in self.panes if p.indexOf(w) >= 0), None)

    def setCurrentWidget(self, w: QWidget) -> None:
        pane = self._pane_of(w)
        if pane is not None:
            pane.setCurrentWidget(w)

    def currentWidget(self) -> QWidget | None:
        return self.panes[0].currentWidget()

    def is_shown(self, w: QWidget) -> bool:
        """The tab is the one in view on its side (left, or right when the right side is open)."""
        pane = self._pane_of(w)
        return pane is not None and not pane.isHidden() and pane.currentWidget() is w

    def _all(self) -> list[QWidget]:
        return [p.widget(i) for p in self.panes for i in range(p.count())]

    def count(self) -> int:
        return sum(p.count() for p in self.panes)

    def tabText(self, i: int) -> str:
        return self.titles.get(self._all()[i], "")

    # --- moving between the sides ------------------------------------------------------

    def move(self, w: QWidget, right: bool, save: bool = True) -> None:
        src, dst = self._pane_of(w), self.panes[1 if right else 0]
        if src is None or src is dst:
            return
        opening = right and self.panes[1].isHidden()
        src.removeTab(src.indexOf(w))
        dst.addTab(w, self.titles.get(w, ""))
        dst.setCurrentWidget(w)
        self._layout()
        if opening:                            # the right side opens: half / half
            half = max(200, self.split.width() // 2)
            self.split.setSizes([half, half])
        if save:
            self._save()
        self.currentChanged.emit(0)            # the tabs in view changed: LabHub loads / polls what is shown

    def _layout(self) -> None:
        right = self.panes[1].count() > 0
        self.panes[1].setVisible(right)
        if right and self.panes[0].count() == 0:     # never leave the left side empty
            self.move(self.panes[1].widget(0), right=False, save=False)

    def _menu(self, pane: QTabWidget, pos) -> None:
        i = pane.tabBar().tabAt(pos)
        if i < 0:
            return
        w = pane.widget(i)
        title = self.titles.get(w, "")
        on_right = pane is self.panes[1]
        m = QMenu(self)
        if on_right:
            m.addAction(f"Move '{title}' to the left side", lambda: self.move(w, right=False))
        elif self.panes[0].count() > 1:
            m.addAction(f"Move '{title}' to the right side (side by side)", lambda: self.move(w, right=True))
        if self.panes[1].count():
            m.addSeparator()
            m.addAction("All tabs on one side", self.join)
        if not m.isEmpty():
            m.exec(pane.tabBar().mapToGlobal(pos))

    def join(self) -> None:
        for w in [self.panes[1].widget(i) for i in range(self.panes[1].count())]:
            self.move(w, right=False, save=False)
        self._save()

    # --- kept in the user file ---------------------------------------------------------

    def _save(self) -> None:
        if self.user is None:
            return
        right = [self.titles.get(self.panes[1].widget(i), "") for i in range(self.panes[1].count())]
        self.user.set("tabs_right", right)
        if right:
            self.user.set("tabs_split", self.split.sizes())

    def restore(self) -> None:
        """Put the tabs saved as 'on the right' back there (after every tab was added)."""
        if self.user is None:
            return
        names = list(self.user.get("tabs_right") or [])
        by_title = {t: w for w, t in self.titles.items()}
        for name in names:
            if name in by_title:
                self.move(by_title[name], right=True, save=False)
        sizes = self.user.get("tabs_split")
        if self.panes[1].count() and isinstance(sizes, list) and len(sizes) == 2:
            self.split.setSizes([int(s) for s in sizes])
