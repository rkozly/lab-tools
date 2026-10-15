"""Central, thread-safe application log.

Every subsystem reports through :class:`LogService`. Entries are fanned out to
registered listeners (e.g. the GUI log panel) and persisted to a rotating log
file. Listeners may be invoked from worker threads; GUI listeners must marshal
to the UI thread themselves (see ``gui.log_panel.LogBridge``).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable


class Level(IntEnum):
    DEBUG = 10
    INFO = 20
    SUCCESS = 25
    WARNING = 30
    ERROR = 40

    @property
    def label(self) -> str:
        return self.name.capitalize()


logging.addLevelName(Level.SUCCESS, "SUCCESS")


@dataclass(frozen=True)
class LogEntry:
    level: Level
    message: str
    device: str = ""
    command: str = ""
    result: str = ""
    error: str = ""
    timestamp: datetime = field(default_factory=datetime.now)

    def as_text(self) -> str:
        """Single-line plain text representation (used for copy/export)."""
        parts = [self.timestamp.strftime("%Y-%m-%d %H:%M:%S"), self.level.name]
        if self.device:
            parts.append(f"[{self.device}]")
        parts.append(self.message)
        if self.command:
            parts.append(f"| cmd: {self.command}")
        if self.result:
            parts.append(f"| result: {_one_line(self.result)}")
        if self.error:
            parts.append(f"| error: {_one_line(self.error)}")
        return " ".join(parts)


Listener = Callable[[LogEntry], None]


def _one_line(text: str) -> str:
    return " ⏎ ".join(line.strip() for line in text.strip().splitlines() if line.strip())


class LogService:
    """Thread-safe log hub with device alias resolution and file persistence."""

    def __init__(self, log_file: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._listeners: list[Listener] = []
        self._aliases: dict[str, str] = {}
        self._file_logger = logging.getLogger("devreader")
        self._file_logger.setLevel(logging.DEBUG)
        self._file_logger.propagate = False
        self.log_file = log_file
        if log_file is not None and not self._file_logger.handlers:
            handler = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._file_logger.addHandler(handler)

    # -- listeners ---------------------------------------------------------
    def add_listener(self, listener: Listener) -> None:
        with self._lock:
            self._listeners.append(listener)

    def remove_listener(self, listener: Listener) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    # -- device aliases ----------------------------------------------------
    def set_alias(self, serial: str, name: str) -> None:
        with self._lock:
            self._aliases[serial] = name

    def device_label(self, serial_or_name: str | None) -> str:
        if not serial_or_name:
            return ""
        with self._lock:
            name = self._aliases.get(serial_or_name)
        return f"{name} ({serial_or_name})" if name else serial_or_name

    # -- logging -----------------------------------------------------------
    def log(
        self,
        level: Level,
        message: str,
        *,
        device: str | None = None,
        command: str = "",
        result: str = "",
        error: str = "",
    ) -> LogEntry:
        entry = LogEntry(
            level=level,
            message=message,
            device=self.device_label(device),
            command=command,
            result=result.strip(),
            error=error.strip(),
        )
        try:
            self._file_logger.log(int(level), entry.as_text())
        except Exception:  # never let file logging break the app
            pass
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(entry)
            except Exception:
                pass
        return entry

    def debug(self, message: str, **kw) -> LogEntry:
        return self.log(Level.DEBUG, message, **kw)

    def info(self, message: str, **kw) -> LogEntry:
        return self.log(Level.INFO, message, **kw)

    def success(self, message: str, **kw) -> LogEntry:
        return self.log(Level.SUCCESS, message, **kw)

    def warning(self, message: str, **kw) -> LogEntry:
        return self.log(Level.WARNING, message, **kw)

    def error(self, message: str, **kw) -> LogEntry:
        return self.log(Level.ERROR, message, **kw)
