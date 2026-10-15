"""Background task infrastructure.

:class:`TaskRunner` executes callables on a ``QThreadPool`` and delivers
results via Qt signals on the GUI thread. Each task gets a :class:`TaskContext`
with a cancel event and progress/output callbacks that are safe to call from
the worker thread.
"""

from __future__ import annotations

import threading
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


@dataclass
class TaskContext:
    task_id: str
    cancel: threading.Event = field(default_factory=threading.Event)
    _signals: "TaskSignals | None" = None

    def progress(self, key: str, value: int, maximum: int, text: str = "") -> None:
        if self._signals is not None:
            self._signals.progress.emit(self.task_id, key, value, maximum, text)

    def output(self, key: str, text: str) -> None:
        if self._signals is not None:
            self._signals.output.emit(self.task_id, key, text)

    @property
    def cancelled(self) -> bool:
        return self.cancel.is_set()


class TaskSignals(QObject):
    started = Signal(str)                       # task_id
    progress = Signal(str, str, int, int, str)  # task_id, key, value, max, text
    output = Signal(str, str, str)              # task_id, key, text
    result = Signal(str, object)                # task_id, result
    error = Signal(str, object, str)            # task_id, exception, traceback
    finished = Signal(str)                      # task_id


class _Runnable(QRunnable):
    def __init__(self, fn: Callable[[TaskContext], Any], ctx: TaskContext, signals: TaskSignals) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._fn, self._ctx, self._signals = fn, ctx, signals

    def run(self) -> None:  # executes on a worker thread
        sig, tid = self._signals, self._ctx.task_id
        sig.started.emit(tid)
        try:
            res = self._fn(self._ctx)
        except BaseException as exc:  # noqa: BLE001 - report everything to the UI
            sig.error.emit(tid, exc, traceback.format_exc())
        else:
            sig.result.emit(tid, res)
        finally:
            sig.finished.emit(tid)


class TaskRunner(QObject):
    """Owns a thread pool; one instance per main window."""

    busy_changed = Signal(bool)

    def __init__(self, parent: QObject | None = None, max_threads: int = 16) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max_threads)
        self._active: dict[str, tuple[TaskContext, TaskSignals]] = {}

    @property
    def busy(self) -> bool:
        return bool(self._active)

    def submit(
        self,
        fn: Callable[[TaskContext], Any],
        *,
        on_result: Callable[[Any], None] | None = None,
        on_error: Callable[[BaseException, str], None] | None = None,
        on_progress: Callable[[str, int, int, str], None] | None = None,
        on_output: Callable[[str, str], None] | None = None,
        on_finished: Callable[[], None] | None = None,
    ) -> TaskContext:
        tid = uuid.uuid4().hex
        signals = TaskSignals()
        ctx = TaskContext(tid, _signals=signals)
        if on_result:
            signals.result.connect(lambda _t, r: on_result(r))
        if on_error:
            signals.error.connect(lambda _t, e, tb: on_error(e, tb))
        if on_progress:
            signals.progress.connect(lambda _t, k, v, m, txt: on_progress(k, v, m, txt))
        if on_output:
            signals.output.connect(lambda _t, k, txt: on_output(k, txt))
        signals.finished.connect(lambda _t: self._done(tid, on_finished))

        was_busy = self.busy
        self._active[tid] = (ctx, signals)
        if not was_busy:
            self.busy_changed.emit(True)
        self._pool.start(_Runnable(fn, ctx, signals))
        return ctx

    def cancel_all(self) -> None:
        for ctx, _ in self._active.values():
            ctx.cancel.set()

    def wait(self, msecs: int = 5000) -> bool:
        return self._pool.waitForDone(msecs)

    def _done(self, tid: str, on_finished: Callable[[], None] | None) -> None:
        self._active.pop(tid, None)
        if on_finished:
            on_finished()
        if not self.busy:
            self.busy_changed.emit(False)
