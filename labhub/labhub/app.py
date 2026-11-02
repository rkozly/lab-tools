"""LabHub entry point."""

from __future__ import annotations

import logging
import sys
import traceback

from .core.context import USER_DIR


def main() -> int:
    USER_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=USER_DIR / "labhub.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from PySide6.QtWidgets import QApplication, QMessageBox

    from .core.context import AppContext
    from .ui.common import apply_theme
    from .ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("LabHub")
    from .core.context import UserSettings
    user = UserSettings()
    apply_theme(app, user.get("theme") or "night")

    def excepthook(t, v, tb):
        text = "".join(traceback.format_exception(t, v, tb))
        logging.error(text)
        QMessageBox.critical(None, "LabHub error", f"{v}\n\nDetails are in {USER_DIR / 'labhub.log'}")

    sys.excepthook = excepthook
    win = MainWindow(AppContext(user))
    win.show()
    return app.exec()
