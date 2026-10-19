"""
Parallel translation queue.

Runs up to `max_parallel` TranslateTask threads at the same time. Each
thread starts its own pktlog_postproc.py process, so files are translated
in parallel. Extra jobs wait in a FIFO queue and start as soon as a slot
frees up. All methods are called from the GUI thread.
"""

from collections import deque
from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal

from ..config import TRANSLATE_MAX_PARALLEL
from .base_task import State
from .translate_task import TranslateTask


@dataclass
class TranslateJob:
    job_id: int
    crm_path: str
    input_file: str
    output_file: str
    label: str = "-"


class TranslateQueue(QObject):
    # (job_id, state, message) - progress and final result of one job
    job_status = Signal(int, str, str)
    # emitted when nothing is running or waiting any more
    idle = Signal()

    def __init__(self, max_parallel: int = TRANSLATE_MAX_PARALLEL, parent=None):
        super().__init__(parent)
        self.max_parallel = max_parallel
        self._next_id = 1
        self._waiting: deque[TranslateJob] = deque()
        self._running: dict[int, TranslateTask] = {}

    def submit(self, crm_path: str, input_file: str, output_file: str,
               label: str = "-") -> int:
        job = TranslateJob(self._next_id, crm_path, input_file, output_file, label)
        self._next_id += 1
        self._waiting.append(job)
        self.job_status.emit(job.job_id, State.WAITING, "Queued")
        self._fill_slots()
        return job.job_id

    def cancel_all(self) -> None:
        """Drop queued jobs and stop running translations."""
        while self._waiting:
            job = self._waiting.popleft()
            self.job_status.emit(job.job_id, State.STOPPED, "Cancelled")
        for task in list(self._running.values()):
            task.stop()

    def busy(self) -> bool:
        return bool(self._running or self._waiting)

    def counts(self) -> tuple[int, int]:
        """(running, waiting)"""
        return len(self._running), len(self._waiting)

    def _fill_slots(self) -> None:
        while self._waiting and len(self._running) < self.max_parallel:
            job = self._waiting.popleft()
            task = TranslateTask(job.crm_path, job.input_file, job.output_file,
                                 label=job.label)
            jid = job.job_id
            task.status.connect(lambda s, m, j=jid: self.job_status.emit(j, s, m))
            task.done.connect(lambda s, m, j=jid: self._on_done(j, s, m))
            self._running[jid] = task
            task.start()

    def _on_done(self, job_id: int, state: str, message: str) -> None:
        self._running.pop(job_id, None)
        self.job_status.emit(job_id, state, message)
        self._fill_slots()
        if not self.busy():
            self.idle.emit()
