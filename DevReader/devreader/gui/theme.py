"""Dark theme: palette + Qt stylesheet."""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

COLORS = {
    "bg": "#15171c",
    "surface": "#1d2027",
    "surface_alt": "#242833",
    "border": "#2f3442",
    "text": "#e6e8ee",
    "muted": "#9097a8",
    "accent": "#4f8cff",
    "accent_hover": "#6b9fff",
    "success": "#3fb950",
    "warning": "#d29922",
    "error": "#f85149",
    "info": "#58a6ff",
    "debug": "#6e7681",
}

STYLESHEET = """
* {{ font-family: "Segoe UI", "Inter", sans-serif; font-size: 10pt; }}
QMainWindow, QWidget#Root {{ background: {bg}; }}
QWidget {{ color: {text}; }}
QToolTip {{ background: {surface_alt}; color: {text}; border: 1px solid {border}; padding: 4px; }}

QFrame#Card {{ background: {surface}; border: 1px solid {border}; border-radius: 10px; }}
QLabel#CardTitle {{ font-size: 12pt; font-weight: 600; }}
QLabel#CardSubtitle, QLabel#Muted {{ color: {muted}; }}
QLabel#AppTitle {{ font-size: 18pt; font-weight: 700; }}
QLabel#AppSubtitle {{ color: {muted}; }}
QLabel#ParamLabel {{ font-weight: 600; }}
QLabel#ParamHint {{ color: {muted}; font-size: 9pt; }}
QLabel#Badge {{ border-radius: 9px; padding: 2px 10px; font-size: 9pt; font-weight: 600; }}

QPushButton {{
    background: {surface_alt}; border: 1px solid {border}; border-radius: 6px;
    padding: 7px 14px; min-height: 18px;
}}
QPushButton:hover {{ border-color: {accent}; }}
QPushButton:pressed {{ background: {border}; }}
QPushButton:disabled {{ color: {debug}; border-color: {surface_alt}; }}
QPushButton[primary="true"] {{ background: {accent}; border-color: {accent}; color: white; font-weight: 600; }}
QPushButton[primary="true"]:hover {{ background: {accent_hover}; }}
QPushButton[primary="true"]:disabled {{ background: #2b3d63; border-color: #2b3d63; color: #8ea3c9; }}
QPushButton[danger="true"]:hover {{ border-color: {error}; color: {error}; }}

QLineEdit, QSpinBox, QComboBox, QPlainTextEdit {{
    background: {bg}; border: 1px solid {border}; border-radius: 6px; padding: 6px 8px;
    selection-background-color: {accent};
}}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {{ border-color: {accent}; }}
QLineEdit[modified="true"] {{ border-color: {warning}; }}
QLineEdit[invalid="true"] {{ border-color: {error}; }}
QLineEdit:read-only {{ color: {muted}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{ background: {surface_alt}; border: 1px solid {border}; selection-background-color: {accent}; }}

QTableView, QTreeView, QListView {{
    background: {surface}; alternate-background-color: #20232b; border: none;
    gridline-color: {border}; selection-background-color: #2a3b5e; selection-color: {text};
}}
QHeaderView::section {{
    background: {surface_alt}; color: {muted}; border: none; border-bottom: 1px solid {border};
    padding: 6px 8px; font-weight: 600;
}}
QTableView::indicator, QCheckBox::indicator {{ width: 16px; height: 16px; }}

QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: transparent; color: {muted}; padding: 8px 16px; border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {text}; border-bottom-color: {accent}; }}
QTabBar::tab:hover {{ color: {text}; }}

QProgressBar {{ background: {bg}; border: 1px solid {border}; border-radius: 5px; height: 10px; text-align: center; color: {muted}; font-size: 8pt; }}
QProgressBar::chunk {{ background: {accent}; border-radius: 4px; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {border}; border-radius: 5px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {border}; border-radius: 5px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}

QSplitter::handle {{ background: {bg}; }}
QSplitter::handle:vertical {{ height: 8px; }}
QSplitter::handle:horizontal {{ width: 8px; }}
QStatusBar {{ background: {surface}; color: {muted}; border-top: 1px solid {border}; }}
QMenu {{ background: {surface_alt}; border: 1px solid {border}; padding: 4px; }}
QMenu::item {{ padding: 6px 20px; border-radius: 4px; }}
QMenu::item:selected {{ background: {accent}; }}
QMenuBar {{ background: {surface}; }}
QMenuBar::item:selected {{ background: {surface_alt}; }}
QDialog {{ background: {surface}; }}
QMessageBox {{ background: {surface}; }}

QFrame#DeviceTile {{ background: {bg}; border: 1px solid {border}; border-radius: 8px; }}
QFrame#DeviceTile:hover {{ border-color: {accent_hover}; }}
QFrame#DeviceTile[checked="true"] {{ border-color: {accent}; background: #1a2333; }}
QLabel#TileName {{ font-weight: 700; }}
QFrame#DeviceTile[muted="true"] QLabel#TileName {{ color: {muted}; }}

QFrame#Toast {{ border-radius: 8px; border: 1px solid {border}; background: {surface_alt}; }}
QFrame#Toast[kind="success"] {{ border-left: 4px solid {success}; }}
QFrame#Toast[kind="error"] {{ border-left: 4px solid {error}; }}
QFrame#Toast[kind="warning"] {{ border-left: 4px solid {warning}; }}
QFrame#Toast[kind="info"] {{ border-left: 4px solid {info}; }}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    c = {k: QColor(v) for k, v in COLORS.items()}
    palette.setColor(QPalette.Window, c["bg"])
    palette.setColor(QPalette.WindowText, c["text"])
    palette.setColor(QPalette.Base, c["surface"])
    palette.setColor(QPalette.AlternateBase, c["surface_alt"])
    palette.setColor(QPalette.Text, c["text"])
    palette.setColor(QPalette.Button, c["surface_alt"])
    palette.setColor(QPalette.ButtonText, c["text"])
    palette.setColor(QPalette.Highlight, c["accent"])
    palette.setColor(QPalette.HighlightedText, QColor("white"))
    palette.setColor(QPalette.ToolTipBase, c["surface_alt"])
    palette.setColor(QPalette.ToolTipText, c["text"])
    palette.setColor(QPalette.PlaceholderText, c["debug"])
    palette.setColor(QPalette.Disabled, QPalette.Text, c["debug"])
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, c["debug"])
    app.setPalette(palette)
    app.setStyleSheet(STYLESHEET.format(**COLORS))


def repolish(widget) -> None:
    """Re-apply the stylesheet after a dynamic property change."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
