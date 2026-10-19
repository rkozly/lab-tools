"""
Application-wide file logging.

All modules use the standard `logging` module. Every record carries a
`device` field (role or serial, "-" if not device related) so the log file
can be filtered per device.
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from .config import LOG_FILE_NAME

LOGGER_NAME = "logcollector"

_FORMAT = "%(asctime)s | %(levelname)-7s | %(device)-10s | %(message)s"


class _DeviceFilter(logging.Filter):
    """Make sure every record has a `device` attribute."""

    def filter(self, record):
        if not hasattr(record, "device"):
            record.device = "-"
        return True


def setup_logging(log_dir: str) -> str:
    """Configure the root app logger. Returns the log file path.

    Falls back to %LOCALAPPDATA%\\logCollector when log_dir is not writable
    (e.g. a read-only share or the file is locked by another user).
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)

    fallback = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                            "logCollector")
    handler = None
    for folder in (log_dir, fallback):
        path = os.path.join(folder, LOG_FILE_NAME)
        try:
            os.makedirs(folder, exist_ok=True)
            handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024,
                                          backupCount=3, encoding="utf-8")
            break
        except OSError:
            continue
    if handler is None:  # no writable location at all: keep running without a file
        handler, path = logging.NullHandler(), "(no log file)"
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.addFilter(_DeviceFilter())
    logger.addHandler(handler)

    # Log uncaught exceptions instead of losing them (pythonw has no console).
    def _excepthook(exc_type, exc, tb):
        logger.critical("Unhandled exception", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _excepthook
    return path


def get_logger(name: str = "") -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)


def log_command(device: str, cmd: list[str] | str, ok: bool,
                rc: int | None = None, error: str = "") -> None:
    """Write one line per executed command: device, command, result, errors."""
    logger = get_logger("cmd")
    text = cmd if isinstance(cmd, str) else " ".join(cmd)
    extra = {"device": device or "-"}
    if ok:
        logger.info("OK   rc=%s | %s", rc, text, extra=extra)
    else:
        err = (error or "").strip().replace("\n", " / ")
        logger.error("FAIL rc=%s | %s | %s", rc, text, err, extra=extra)
