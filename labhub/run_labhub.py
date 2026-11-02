"""Launcher script (used by LabHub.bat and for running without installing)."""

import os
import sys
import traceback


def _startup_error(text: str) -> None:
    """pythonw has no console: write the error to a file and show it in a box."""
    folder = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "LabHub")
    path = os.path.join(folder, "startup_error.txt")
    try:
        os.makedirs(folder, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"Python {sys.version}\n{sys.executable}\n\n{text}")
    except OSError:
        path = "(could not write the error file)"
    if sys.stderr:
        print(text, file=sys.stderr)
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, f"{text[-1500:]}\n\nSaved to {path}", "LabHub failed to start", 0x10)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from labhub.app import main
        code = main()
    except SystemExit:
        raise
    except BaseException:
        _startup_error(traceback.format_exc())
        code = 1
    sys.exit(code)
