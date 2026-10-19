"""
Log Collector - entry point.

    python logCollector.py      (or pythonw logCollector.py for no console)

Double-click "Log Collector.bat" to start it without a terminal.
"""

import os
import sys
import traceback

APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _show_startup_error(details: str) -> None:
    """Report a startup failure even when there is no console (pythonw).

    Uses the plain Windows message box via ctypes, so it works even when
    PySide6 itself is missing. The full traceback is also saved to a file.
    """
    hint = ""
    if "No module named 'PySide6'" in details:
        hint = ("PySide6 is not installed for this Python:\n"
                f"{sys.executable}\n\n"
                "Fix: open a terminal and run\n"
                f'"{sys.executable}" -m pip install PySide6\n\n')
    local = os.path.join(os.environ.get("LOCALAPPDATA", APP_DIR), "logCollector")
    report = os.path.join(local, "startup_error.txt")
    try:
        os.makedirs(local, exist_ok=True)
        with open(report, "w", encoding="utf-8") as f:
            f.write(f"Python: {sys.executable} {sys.version}\n\n{details}")
    except OSError:
        report = "(could not be saved)"

    last = details.strip().splitlines()[-1] if details.strip() else ""
    text = (f"Log Collector could not start.\n\n{hint}Error: {last}\n\n"
            f"Full details: {report}")
    if sys.stderr:
        print(text, file=sys.stderr)
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "Log Collector", 0x10)


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from logcollector.app_logging import get_logger, setup_logging
    from logcollector.config import APP_NAME, ORG_NAME
    from logcollector.core.process import init_job_object
    from logcollector.ui.main_window import MainWindow
    from logcollector.ui.theme import apply_theme

    log_path = setup_logging(APP_DIR)
    log = get_logger()
    log.info("=" * 60)
    log.info("%s starting (Python %s, %s)", APP_NAME, sys.version.split()[0],
             sys.executable)

    # Make sure no child process (adb, translator) can outlive the app.
    init_job_object()

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    apply_theme(app)

    window = MainWindow(log_path)
    window.show()
    rc = app.exec()
    log.info("%s exited (%s)", APP_NAME, rc)
    return rc


if __name__ == "__main__":
    # Makes "import logcollector" work no matter which folder we start from.
    sys.path.insert(0, APP_DIR)
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        _show_startup_error(traceback.format_exc())
        sys.exit(1)
