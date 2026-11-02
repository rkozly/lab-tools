"""
Event bus. The core announces everything that happens as an event; the GUI
and features subscribe. Handlers run on the thread that publishes, so the
GUI goes through ui/qt_bridge.py, which moves events onto the Qt thread.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)


class Event:
    """Base class of all events."""


@dataclass
class LabLoaded(Event):
    lab: object  # models.Lab


@dataclass
class NodesChanged(Event):
    nodes: list  # list[models.NodeView], in display order


@dataclass
class NodeStatusChanged(Event):
    node_id: str
    name: str
    old: str
    new: str


@dataclass
class AdbStatus(Event):
    ok: bool
    message: str


@dataclass
class SelectionChanged(Event):
    ids: list[str] = field(default_factory=list)


@dataclass
class LogMessage(Event):
    text: str
    level: str = "info"  # info | warning | error


Handler = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[type, list[Handler]] = defaultdict(list)
        self._lock = threading.Lock()

    def subscribe(self, event_type: type, handler: Handler) -> None:
        """Subscribe to one event type (and its subclasses). Use Event for all."""
        with self._lock:
            self._subs[event_type].append(handler)

    def unsubscribe(self, event_type: type, handler: Handler) -> None:
        with self._lock:
            if handler in self._subs[event_type]:
                self._subs[event_type].remove(handler)

    def publish(self, event: Event) -> None:
        with self._lock:
            handlers = [h for t, hs in self._subs.items() if isinstance(event, t) for h in hs]
        for h in handlers:
            try:
                h(event)
            except Exception:  # one bad subscriber must not break the others
                log.exception("event handler failed for %s", type(event).__name__)

    def log(self, text: str, level: str = "info") -> None:
        self.publish(LogMessage(text, level))
