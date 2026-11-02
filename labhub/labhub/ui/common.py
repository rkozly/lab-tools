"""
Shared UI pieces: theme, Qt bridge (core events -> GUI thread), colours,
flow layout.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QLayout, QStyleFactory

from ..core.events import Event, EventBus
from ..core.models import Status

STATUS_COLOR = {
    Status.CONNECTED: "#3ccf7a",
    Status.DISCONNECTED: "#5b6170",
    Status.UNAUTHORIZED: "#f0b43c",
    Status.OFFLINE: "#f0b43c",
    Status.UNCONFIGURED: "#46506a",
    Status.UNKNOWN: "#46506a",
}
JOB_COLOR = {"queued": "#8a90a0", "running": "#2f6fed", "done": "#3ccf7a",
             "failed": "#ff6b7a", "stopped": "#f0b43c", "skipped": "#5b6170"}
JOB_ICON = {"queued": "…", "running": "●", "done": "✔", "failed": "✖", "stopped": "■", "skipped": "–"}


# Night (dark) colours are the source; DAY maps every one of them to a light
# colour. theme.qss is written with the night colours and translated.
DAY = {
    "#16181d": "#f4f5f8", "#1c1f26": "#ffffff", "#101216": "#ffffff", "#1a1d23": "#eef0f4",
    "#14161b": "#eef0f4", "#1f232b": "#ffffff", "#1d2740": "#e3ecff", "#1e2128": "#eceef2",
    "#262a33": "#eceef2", "#2e3340": "#e2e5eb", "#232731": "#e6e8ee", "#23262e": "#e3e6ec",
    "#2c3039": "#d4d8e0", "#333844": "#c9ced8", "#3a3f4b": "#c4c9d3", "#46506a": "#9aa3b5",
    "#253354": "#cfdcff", "#2a2e38": "#ffffff", "#5b6170": "#9aa0ad", "#8a90a0": "#5f6675",
    "#cfd6e2": "#1f2430", "#d8dce4": "#1f2430", "#e8eaf0": "#1f2430", "#ffffff": "#ffffff",
    "#3a1f24": "#fde8ea", "#4a242b": "#fbd5da", "#6b2b35": "#e7a3ad", "#ff9aa5": "#c02d3f",
    "#2f6fed": "#2f6fed", "#3d7cf5": "#3d7cf5",
}
THEMES = ("night", "day")
_theme = "night"


def theme() -> str:
    return _theme


def c(color: str) -> str:
    """A night colour translated to the current theme."""
    return DAY.get(color.lower(), color) if _theme == "day" else color


def apply_theme(app: QApplication, name: str = "night") -> None:
    global _theme
    _theme = name if name in THEMES else "night"
    app.setStyle(QStyleFactory.create("Fusion"))
    pal = QPalette()
    for role, color in [
        (QPalette.Window, "#16181d"), (QPalette.WindowText, "#d8dce4"), (QPalette.Base, "#101216"),
        (QPalette.AlternateBase, "#1a1d23"), (QPalette.Text, "#d8dce4"), (QPalette.Button, "#262a33"),
        (QPalette.ButtonText, "#d8dce4"), (QPalette.Highlight, "#2f6fed"), (QPalette.HighlightedText, "#ffffff"),
        (QPalette.ToolTipBase, "#2a2e38"), (QPalette.ToolTipText, "#e8eaf0"), (QPalette.PlaceholderText, "#5b6170"),
    ]:
        pal.setColor(role, QColor(c(color)))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor(c("#5b6170")))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(c("#5b6170")))
    app.setPalette(pal)
    qss = (Path(__file__).with_name("theme.qss")).read_text(encoding="utf-8")
    if _theme == "day":
        import re
        qss = re.sub(r"#[0-9a-fA-F]{6}", lambda m: DAY.get(m.group(0).lower(), m.group(0)), qss)
        qss = qss.replace("color: white", "color: #ffffff")
    app.setStyleSheet(qss)


class QtBridge(QObject):
    """Re-emits every core event on the GUI thread as `event(object)`."""
    event = Signal(object)

    def __init__(self, bus: EventBus) -> None:
        super().__init__()
        bus.subscribe(Event, self.event.emit)  # queued across threads automatically


def repolish(widget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class FlowLayout(QLayout):
    """Lays out widgets left to right, wrapping to new lines."""

    def __init__(self, parent=None, spacing: int = 8) -> None:
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do_layout(self, rect: QRect, test: bool) -> int:
        m = self.contentsMargins()
        r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = r.x(), r.y(), 0
        for item in self._items:
            if item.widget() and not item.widget().isVisible() and not test:
                pass
            hint = item.sizeHint()
            nx = x + hint.width() + self._spacing
            if nx - self._spacing > r.right() + 1 and line_h > 0:
                x, y = r.x(), y + line_h + self._spacing
                nx, line_h = x + hint.width() + self._spacing, 0
            if not test:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x, line_h = nx, max(line_h, hint.height())
        return y + line_h - rect.y() + m.bottom()
