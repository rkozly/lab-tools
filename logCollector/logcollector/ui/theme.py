"""Dark, minimal theme (charcoal surfaces, hairline borders, one blue accent)."""

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory

from ..tasks.base_task import State

# Status colors (also used in the device table). Soft tones that read well
# on a dark background without looking neon.
COLOR_BLUE = "#0A84FF"
COLOR_ORANGE = "#E5A445"
COLOR_GREEN = "#3DBE7A"
COLOR_RED = "#F07178"
COLOR_GRAY = "#94A3B8"

STATE_COLORS = {
    State.IDLE: COLOR_GRAY,
    State.WAITING: COLOR_BLUE,
    State.COLLECTING: COLOR_BLUE,
    State.STOPPING: COLOR_BLUE,
    State.STOPPED: COLOR_GRAY,
    State.TRANSLATING: COLOR_ORANGE,
    State.COMPLETED: COLOR_GREEN,
    State.ERROR: COLOR_RED,
}

# Palette
_WINDOW = "#121214"    # window background
_SURFACE = "#18181B"   # cards (group boxes)
_BASE = "#1C1C20"      # inputs / tables
_ALT = "#202024"       # zebra rows
_BUTTON = "#26262B"
_BORDER = "rgba(255, 255, 255, 0.08)"
_BORDER_STRONG = "rgba(255, 255, 255, 0.14)"
_TEXT = "#FAFAFA"
_MUTED = "#94A3B8"
_DISABLED = "#5B6472"

_FONT = "Segoe UI Variable Text"
_MONO = "Cascadia Mono"

_STYLESHEET = f"""
QWidget {{ font-family: "{_FONT}", "Segoe UI", sans-serif; font-size: 10pt; }}
QMainWindow, QScrollArea, QScrollArea > QWidget > QWidget {{ background: {_WINDOW}; }}

QGroupBox {{
    background: {_SURFACE};
    border: 1px solid {_BORDER};
    border-radius: 10px;
    margin-top: 22px;
    padding: 10px 8px 8px 8px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 4px;
    padding: 0 4px 4px 4px;
    color: {_TEXT};
}}
QGroupBox QGroupBox {{
    background: transparent;
    border: none;
    border-top: 1px solid {_BORDER};
    border-radius: 0;
}}
QGroupBox QGroupBox::title {{ color: {_MUTED}; }}
QGroupBox QWidget {{ font-weight: normal; }}
QLabel {{ background: transparent; }}

QPushButton {{
    background: {_BUTTON};
    color: {_TEXT};
    border: 1px solid {_BORDER};
    border-radius: 6px;
    padding: 5px 14px;
    min-width: 64px;
}}
QPushButton:hover {{ background: #2E2E34; border-color: {_BORDER_STRONG}; }}
QPushButton:pressed {{ background: #222227; }}
QPushButton:disabled {{ color: {_DISABLED}; background: #1E1E22; }}
QPushButton:focus {{ border-color: {COLOR_BLUE}; }}

QLineEdit, QSpinBox, QComboBox {{
    background: {_BASE};
    color: {_TEXT};
    border: 1px solid {_BORDER};
    border-radius: 6px;
    padding: 4px 6px;
    selection-background-color: {COLOR_BLUE};
}}
QLineEdit:hover, QSpinBox:hover {{ border-color: {_BORDER_STRONG}; }}
QLineEdit:focus, QSpinBox:focus {{ border-color: {COLOR_BLUE}; }}
QLineEdit:disabled, QSpinBox:disabled {{ color: {_DISABLED}; }}
QLineEdit {{ font-family: "{_MONO}", Consolas, monospace; font-size: 9pt; }}

QCheckBox {{ spacing: 6px; color: {_TEXT}; }}
QCheckBox::indicator {{
    width: 14px; height: 14px;
    border: 1px solid {_BORDER_STRONG};
    border-radius: 4px;
    background: {_BASE};
}}
QCheckBox::indicator:hover {{ border-color: rgba(255, 255, 255, 0.28); }}
QCheckBox::indicator:checked {{ background: {COLOR_BLUE}; border-color: {COLOR_BLUE}; }}

QTableWidget {{
    background: {_BASE};
    alternate-background-color: {_ALT};
    border: 1px solid {_BORDER};
    border-radius: 8px;
    gridline-color: transparent;
    font-family: "{_MONO}", Consolas, monospace;
    font-size: 9pt;
    selection-background-color: rgba(10, 132, 255, 0.22);
    selection-color: {_TEXT};
}}
QTableWidget::item {{ padding: 3px 6px; border-bottom: 1px solid {_BORDER}; }}
QTableWidget::indicator {{
    width: 13px; height: 13px;
    border: 1px solid {_BORDER_STRONG}; border-radius: 4px; background: {_BASE};
}}
QTableWidget::indicator:checked {{ background: {COLOR_BLUE}; border-color: {COLOR_BLUE}; }}
QHeaderView::section {{
    background: {_SURFACE};
    color: {_MUTED};
    padding: 5px 6px;
    border: none;
    border-bottom: 1px solid {_BORDER};
    font-weight: 600;
}}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: rgba(255, 255, 255, 0.12); border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: rgba(255, 255, 255, 0.22); }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: rgba(255, 255, 255, 0.12); border-radius: 3px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
    background: none; width: 0; height: 0;
}}

QStatusBar {{
    background: {_SURFACE};
    border-top: 1px solid {_BORDER};
    color: {_MUTED};
    font-size: 9pt;
}}
QStatusBar::item {{ border: none; }}

QToolTip {{
    background: #232328; color: {_TEXT};
    border: 1px solid {_BORDER_STRONG}; border-radius: 6px; padding: 4px 6px;
}}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle(QStyleFactory.create("Fusion"))
    app.setFont(QFont(_FONT, 10))
    p = QPalette()
    text = QColor(_TEXT)
    disabled = QColor(_DISABLED)

    p.setColor(QPalette.Window, QColor(_WINDOW))
    p.setColor(QPalette.WindowText, text)
    p.setColor(QPalette.Base, QColor(_BASE))
    p.setColor(QPalette.AlternateBase, QColor(_ALT))
    p.setColor(QPalette.ToolTipBase, QColor("#232328"))
    p.setColor(QPalette.ToolTipText, text)
    p.setColor(QPalette.Text, text)
    p.setColor(QPalette.Button, QColor(_BUTTON))
    p.setColor(QPalette.ButtonText, text)
    p.setColor(QPalette.BrightText, QColor(COLOR_RED))
    p.setColor(QPalette.Link, QColor(COLOR_BLUE))
    p.setColor(QPalette.Highlight, QColor(COLOR_BLUE))
    p.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
    p.setColor(QPalette.PlaceholderText, QColor(_MUTED))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, disabled)
    app.setPalette(p)
    app.setStyleSheet(_STYLESHEET)


def dark_title_bar(window) -> None:
    """Ask Windows 10/11 to draw the title bar dark, to match the theme."""
    import sys
    if sys.platform != "win32":
        return
    try:
        import ctypes
        value = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(int(window.winId())), 20,  # DWMWA_USE_IMMERSIVE_DARK_MODE
            ctypes.byref(value), ctypes.sizeof(value))
    except (OSError, AttributeError):
        pass
