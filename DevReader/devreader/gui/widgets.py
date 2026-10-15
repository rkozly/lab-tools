"""Small reusable widgets: cards, status badges, toast notifications."""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from devreader.gui.theme import COLORS


class Card(QFrame):
    """Rounded panel with a title row and a body layout."""

    def __init__(self, title: str, subtitle: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(10)

        self.header = QHBoxLayout()
        self.header.setSpacing(8)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("CardTitle")
        titles.addWidget(self.title_label)
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setObjectName("CardSubtitle")
        self.subtitle_label.setVisible(bool(subtitle))
        titles.addWidget(self.subtitle_label)
        self.header.addLayout(titles)
        self.header.addStretch(1)
        outer.addLayout(self.header)

        self.body = QVBoxLayout()
        self.body.setSpacing(8)
        outer.addLayout(self.body, 1)

    def add_header_widget(self, widget: QWidget) -> None:
        self.header.addWidget(widget)

    def set_subtitle(self, text: str) -> None:
        self.subtitle_label.setText(text)
        self.subtitle_label.setVisible(bool(text))


def button(text: str, *, primary: bool = False, danger: bool = False, tooltip: str = "") -> QPushButton:
    btn = QPushButton(text)
    btn.setCursor(Qt.PointingHandCursor)
    if primary:
        btn.setProperty("primary", True)
    if danger:
        btn.setProperty("danger", True)
    if tooltip:
        btn.setToolTip(tooltip)
    return btn


class Badge(QLabel):
    def __init__(self, text: str = "", color: str = COLORS["muted"], parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("Badge")
        self.set(text, color)

    def set(self, text: str, color: str) -> None:
        self.setText(text)
        self.setStyleSheet(f"QLabel#Badge {{ color: {color}; border: 1px solid {color}; }}")


_TOAST_ICONS = {"success": "✅", "error": "❌", "warning": "⚠", "info": "ℹ"}


class Toast(QFrame):
    """Transient notification shown in the bottom-right corner of its parent."""

    def __init__(self, parent: QWidget, kind: str, title: str, message: str, msecs: int) -> None:
        super().__init__(parent)
        self.setObjectName("Toast")
        self.setProperty("kind", kind)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setMinimumWidth(300)
        self.setMaximumWidth(440)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 10, 10)
        icon = QLabel(_TOAST_ICONS.get(kind, "ℹ"))
        icon.setStyleSheet("font-size: 14pt;")
        lay.addWidget(icon, 0, Qt.AlignTop)
        text = QVBoxLayout()
        t = QLabel(f"<b>{title}</b>")
        text.addWidget(t)
        if message:
            m = QLabel(message)
            m.setWordWrap(True)
            m.setObjectName("Muted")
            text.addWidget(m)
        lay.addLayout(text, 1)
        close = QPushButton("✕")
        close.setFlat(True)
        close.setFixedSize(22, 22)
        close.setStyleSheet("QPushButton { border: none; background: transparent; padding: 0; }")
        close.clicked.connect(self.close)
        lay.addWidget(close, 0, Qt.AlignTop)

        self._effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._effect)
        self._anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        QTimer.singleShot(msecs, self.fade_out)

    def fade_in(self) -> None:
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.start()

    def fade_out(self) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._effect.opacity())
        self._anim.setEndValue(0.0)
        self._anim.finished.connect(self.close)
        self._anim.start()


class ToastManager:
    """Stacks toasts in the bottom-right corner of a host widget."""

    def __init__(self, host: QWidget) -> None:
        self._host = host
        self._toasts: list[Toast] = []

    def show(self, kind: str, title: str, message: str = "", msecs: int = 4500) -> None:
        toast = Toast(self._host, kind, title, message, msecs)
        toast.destroyed.connect(lambda *_: self._forget(toast))
        self._toasts.append(toast)
        while len(self._toasts) > 4:
            self._toasts.pop(0).close()
        toast.adjustSize()
        toast.show()
        toast.raise_()
        toast.fade_in()
        self.reposition()

    def _forget(self, toast: Toast) -> None:
        if toast in self._toasts:
            self._toasts.remove(toast)
        self.reposition()

    def reposition(self) -> None:
        margin, y = 18, self._host.height() - 18 - 30  # leave room for status bar
        for toast in reversed(self._toasts):
            try:
                toast.adjustSize()
                y -= toast.height()
                toast.move(self._host.width() - toast.width() - margin, y)
                y -= 8
            except RuntimeError:  # already deleted
                pass
