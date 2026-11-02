"""
Job runner. A Run sends one command (or sequence) to several nodes; each
node gets a Job with its own output buffer. Output is streamed as JobOutput
events; jobs can be stopped one by one or all together.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..transports import base as tr
from .events import Event, EventBus
from .models import NodeView, Status

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|\x1b[=>]|\r(?!\n)")
MAX_LINES = 20000
_ids = itertools.count(1)


def clean(text: str) -> str:
    return _ANSI.sub("", text)


# --- events -----------------------------------------------------------------

@dataclass
class RunStarted(Event):
    run: "Run"


@dataclass
class JobOutput(Event):
    job: "Job"
    text: str


@dataclass
class JobStateChanged(Event):
    job: "Job"


@dataclass
class RunFinished(Event):
    run: "Run"


# --- model ------------------------------------------------------------------

class OutputBuffer:
    """Line buffer with a cap, so a long `watch` cannot fill memory."""

    def __init__(self, max_lines: int = MAX_LINES) -> None:
        self._lines: deque[str] = deque(maxlen=max_lines)
        self._partial = ""
        self._lock = threading.Lock()
        self.dropped = 0

    def append(self, text: str) -> None:
        with self._lock:
            parts = (self._partial + text).split("\n")
            self._partial = parts.pop()
            for line in parts:
                if len(self._lines) == self._lines.maxlen:
                    self.dropped += 1
                self._lines.append(line)

    def text(self) -> str:
        with self._lock:
            body = "\n".join(self._lines)
            if self._partial:
                body = f"{body}\n{self._partial}" if body else self._partial
            return body

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()
            self._partial = ""
            self.dropped = 0


@dataclass
class Job:
    id: int
    run_id: int
    node_id: str
    node_name: str
    status: str = "queued"   # queued | running | done | failed | stopped | skipped
    exit_code: int | None = None
    started: datetime | None = None
    ended: datetime | None = None
    message: str = ""
    output: OutputBuffer = field(default_factory=OutputBuffer)
    _proc: subprocess.Popen | None = field(default=None, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def active(self) -> bool:
        return self.status in ("queued", "running")


@dataclass
class Run:
    id: int
    label: str
    command: str
    kind: str                          # exec | sequence
    targets: list[str]
    params: dict = field(default_factory=dict)
    preset_id: str | None = None
    started: datetime = field(default_factory=datetime.now)
    ended: datetime | None = None
    jobs: list[Job] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return any(j.active for j in self.jobs)


# --- runner -----------------------------------------------------------------

def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=tr.CREATE_NO_WINDOW)
    else:
        proc.kill()


class JobRunner:
    def __init__(self, bus: EventBus, registry, max_parallel: int = 16) -> None:
        self.bus = bus
        self.registry = registry
        self._pool = ThreadPoolExecutor(max_workers=max_parallel, thread_name_prefix="job")
        self.runs: list[Run] = []
        self._lock = threading.Lock()

    # --- public -------------------------------------------------------------

    def run(self, label: str, command: str, nodes: list[NodeView], targets: list[str],
            per_node: dict[str, str] | None = None, preset_id: str | None = None,
            params: dict | None = None) -> Run:
        """Run a shell command on each node. per_node overrides the command per node id."""
        run = Run(next(_ids), label, command, "exec", targets, params or {}, preset_id)
        self._start(run, nodes, lambda job, node: self._exec(job, node, (per_node or {}).get(node.id, command)))
        return run

    def run_sequence(self, label: str, spec, nodes: list[NodeView], targets: list[str],
                     preset_id: str | None = None, delay_ms: int | None = None) -> Run:
        cmd = f"{spec.start}  ->  {' '.join(spec.steps)}"
        run = Run(next(_ids), label, cmd, "sequence", targets, {"delay_ms": delay_ms or spec.delay_ms}, preset_id)
        self._start(run, nodes, lambda job, node: self._sequence(job, node, spec, delay_ms))
        return run

    def stop_job(self, job: Job) -> None:
        job._stop.set()
        if job._proc is not None:
            threading.Thread(target=self._terminate, args=(job,), daemon=True).start()
        elif job.status == "queued":
            self._finish(job, "stopped", None, "stopped before start")

    def stop_all(self) -> None:
        with self._lock:
            jobs = [j for r in self.runs for j in r.jobs if j.active]
        for j in jobs:
            self.stop_job(j)

    def active_jobs(self) -> list[Job]:
        with self._lock:
            return [j for r in self.runs for j in r.jobs if j.active]

    def shutdown(self) -> None:
        self.stop_all()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def export(self, run: Run, folder: str | Path) -> Path:
        stamp = run.started.strftime("%Y%m%d_%H%M%S")
        safe = re.sub(r"[^\w.-]+", "_", run.label).strip("_")[:40] or "run"
        out = Path(folder) / f"{stamp}_{safe}"
        out.mkdir(parents=True, exist_ok=True)
        for j in run.jobs:
            name = re.sub(r"[^\w.-]+", "_", j.node_name)
            (out / f"{name}.log").write_text(j.output.text() + "\n", encoding="utf-8")
        meta = {
            "label": run.label, "command": run.command, "kind": run.kind, "preset": run.preset_id,
            "params": run.params, "targets": run.targets,
            "started": run.started.isoformat(timespec="seconds"),
            "ended": run.ended.isoformat(timespec="seconds") if run.ended else None,
            "jobs": [{"node": j.node_name, "status": j.status, "exit_code": j.exit_code, "message": j.message,
                      "started": j.started.isoformat(timespec="seconds") if j.started else None,
                      "ended": j.ended.isoformat(timespec="seconds") if j.ended else None}
                     for j in run.jobs],
        }
        (out / "run.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return out

    # --- internals ----------------------------------------------------------

    def _start(self, run: Run, nodes: list[NodeView], work) -> None:
        for n in nodes:
            run.jobs.append(Job(next(_ids), run.id, n.id, n.name))
        with self._lock:
            self.runs.append(run)
            del self.runs[:-200]  # keep recent history only
        self.bus.publish(RunStarted(run))
        for job, node in zip(run.jobs, nodes):
            if node.state.status != Status.CONNECTED and node.kind != "backbone":
                self._finish(job, "skipped", None, f"not run: {node.state.status.value}")
            elif node.kind == "backbone" and node.state.status == Status.UNCONFIGURED:
                self._finish(job, "skipped", None, "not run: backbone connection not configured")
            else:
                self._pool.submit(self._guard, job, node, work)
        self._check_run(run)

    def _guard(self, job: Job, node: NodeView, work) -> None:
        if job._stop.is_set():
            return
        job.status, job.started = "running", datetime.now()
        self.bus.publish(JobStateChanged(job))
        try:
            work(job, node)
        except tr.TransportError as e:
            self._finish(job, "failed", None, str(e))
        except Exception as e:  # keep the pool alive whatever happens
            self._finish(job, "failed", None, f"{type(e).__name__}: {e}")

    def _spec(self, node: NodeView):
        return tr.connect_spec(node.config, self.registry.serial_of(node))

    def _emit(self, job: Job, text: str) -> None:
        text = clean(text)
        if text:
            job.output.append(text)
            self.bus.publish(JobOutput(job, text))

    def _read(self, job: Job, proc: subprocess.Popen, on_text=None) -> None:
        fd = proc.stdout
        while True:
            chunk = fd.read1(4096) if hasattr(fd, "read1") else os.read(fd.fileno(), 4096)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace").replace("\r\n", "\n")
            self._emit(job, text)
            if on_text:
                on_text(text)

    def _exec(self, job: Job, node: NodeView, command: str) -> None:
        spec = self._spec(node)
        if spec.type == "adb":
            tr.ensure_root(spec.params.get("serial", ""))
        proc = tr.popen(tr.exec_argv(spec, command))
        job._proc = proc
        self._read(job, proc)
        code = proc.wait()
        if job._stop.is_set():
            self._finish(job, "stopped", code, "stopped")
        else:
            self._finish(job, "done" if code == 0 else "failed", code, f"exit {code}")

    def _sequence(self, job: Job, node: NodeView, spec, delay_ms: int | None) -> None:
        delay = (spec.delay_ms if delay_ms is None else delay_ms) / 1000
        prompt = re.compile(spec.prompt)
        proc = tr.popen(tr.session_argv(self._spec(node), spec.tty), stdin=True)
        job._proc = proc
        seen = {"buf": ""}
        cond = threading.Condition()

        def on_text(text: str) -> None:
            with cond:
                seen["buf"] = (seen["buf"] + clean(text))[-4000:]
                cond.notify_all()

        reader = threading.Thread(target=self._read, args=(job, proc, on_text), daemon=True)
        reader.start()

        def send(text: str) -> None:
            proc.stdin.write((text + "\n").encode())
            proc.stdin.flush()

        def wait_prompt(timeout: float) -> bool:
            end = time.monotonic() + timeout
            with cond:
                while not prompt.search(seen["buf"]):
                    left = end - time.monotonic()
                    if left <= 0 or job._stop.is_set() or proc.poll() is not None:
                        return False
                    cond.wait(min(left, 0.2))
                seen["buf"] = ""
                return True

        try:
            with cond:
                seen["buf"] = ""
            send(spec.start)
            for i, step in enumerate(spec.steps, 1):
                if not wait_prompt(spec.step_timeout_s):
                    why = "stopped" if job._stop.is_set() else \
                        ("session ended" if proc.poll() is not None else f"no prompt within {spec.step_timeout_s:g} s")
                    self._emit(job, f"\n[LabHub] step {i}/{len(spec.steps)}: {why}\n")
                    self._finish(job, "stopped" if job._stop.is_set() else "failed", None,
                                 f"step {i}: {why}")
                    return
                time.sleep(delay)
                self._emit(job, f"\n[LabHub] step {i}/{len(spec.steps)}: sending \"{step}\"\n")
                send(step)
            wait_prompt(spec.step_timeout_s)
            if spec.exit:
                send(spec.exit)
            self._emit(job, f"\n[LabHub] sequence finished ({len(spec.steps)} steps)\n")
            self._finish(job, "done", 0, "sequence finished")
        except OSError as e:
            self._finish(job, "failed", None, f"session closed: {e}")
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
            _kill_tree(proc)
            reader.join(timeout=2)

    def _terminate(self, job: Job) -> None:
        proc = job._proc
        if proc is None:
            return
        # adb shell has no pty, so the device process gets SIGHUP when adb dies.
        _kill_tree(proc)

    def _finish(self, job: Job, status: str, code, message: str) -> None:
        if not job.active:
            return
        job.status, job.exit_code, job.message, job.ended = status, code, message, datetime.now()
        job._proc = None
        self.bus.publish(JobStateChanged(job))
        with self._lock:
            run = next((r for r in self.runs if r.id == job.run_id), None)
        if run:
            self._check_run(run)

    def _check_run(self, run: Run) -> None:
        with self._lock:
            done = run.ended is None and not run.active
            if done:
                run.ended = datetime.now()
        if done:
            self.bus.publish(RunFinished(run))
