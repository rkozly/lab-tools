"""
Steps typed into a device's live shell, with a pass/fail result.

MenuSequence (QCMAP_CLI): for every key wait the configured delay, send it,
then wait until the menu has printed something new and gone quiet. After
each group (e.g. 1-2-1) the output must match the success pattern
(success / succeeds / pass), otherwise the sequence stops.

CommandCheck: send one command, wait until the output is quiet, and report
FAIL if an error line was printed (error, failed, invalid, ...), else PASS.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass

from .events import Event, EventBus

DEFAULT_SUCCESS_WORDS = "succe*, pass, passed, enabled"
MENU_LINE = re.compile(r"^\s*\d+\s*[.)]\s")   # "13. QoS Configuration" is a menu entry, not a result


def words_regex(words: str) -> str:
    """'succe*, pass, enabled' -> regex. * = any ending, otherwise a whole word."""
    parts = []
    for w in re.split(r"[,\s]+", (words or "").strip()):
        if w:
            parts.append(re.escape(w[:-1]) + r"\w*" if w.endswith("*") else re.escape(w) + r"\b")
    return r"(?i)\b(" + "|".join(parts) + ")" if parts else words_regex(DEFAULT_SUCCESS_WORDS)


DEFAULT_SUCCESS = words_regex(DEFAULT_SUCCESS_WORDS)
FAIL_LINE = re.compile(r"(?i)(\berror\b|fail(ed|ure)?\b|invalid|not found|no such|unknown|denied|usage:)")
_PROMPT_ONLY = re.compile(r"^\s*\S*\s*[#$>]\s*$")


@dataclass
class SequenceProgress(Event):
    node_id: str
    name: str
    state: str          # running | pass | done | failed | stopped
    text: str
    group: int = 0      # 1-based group number, 0 = the sequence as a whole
    detail: str = ""


@dataclass
class CheckResult(Event):
    node_id: str
    name: str
    label: str
    ok: bool
    detail: str


def _line_at(text: str, pos: int) -> str:
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    return text[start:end if end != -1 else len(text)].strip()


class _ShellStep:
    """Shared plumbing: collect a session's output while a step runs."""

    def __init__(self, session, bus: EventBus, settle_s: float) -> None:
        self.session, self.bus, self.settle = session, bus, settle_s
        self._buf = ""
        self._last = 0.0
        self._cond = threading.Condition()
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()

    def _on_output(self, text: str) -> None:
        with self._cond:
            self._buf += text
            self._last = time.monotonic()
            self._cond.notify_all()

    def _wait_shell(self) -> bool:
        s = self.session
        end = time.monotonic() + 90  # adb root + shell start
        while s.status == "starting" and time.monotonic() < end and not self._stop.is_set():
            time.sleep(0.2)
        return s.alive

    def _send(self, text: str) -> int:
        with self._cond:
            mark = len(self._buf)
        self.session.write(text + "\n")
        return mark

    def _wait_response(self, start: int, sent: str, timeout: float) -> bool:
        """New output (not just the echo of `sent`), then `settle` seconds of quiet."""
        end = time.monotonic() + timeout
        with self._cond:
            while True:
                meaningful = re.sub(r"\s+", "", self._buf[start:])
                if meaningful and meaningful != re.sub(r"\s+", "", sent):
                    break
                left = end - time.monotonic()
                if left <= 0 or self._stop.is_set() or not self.session.alive:
                    return False
                self._cond.wait(min(left, 0.2))
            while True:
                quiet = time.monotonic() - self._last
                if quiet >= self.settle or self._stop.is_set():
                    return not self._stop.is_set()
                self._cond.wait(self.settle - quiet)


class MenuSequence(_ShellStep):
    def __init__(self, session, spec, bus: EventBus, label: str, delay_ms: int | None = None,
                 settle_ms: int | None = None, timeout_s: float | None = None, success: str | None = None) -> None:
        super().__init__(session, bus, (spec.settle_ms if settle_ms is None else settle_ms) / 1000)
        self.spec, self.label = spec, label
        self.delay = (spec.delay_ms if delay_ms is None else delay_ms) / 1000
        self.timeout = spec.step_timeout_s if timeout_s is None else timeout_s
        pattern = spec.success if success is None else success
        self.success = re.compile(pattern or DEFAULT_SUCCESS)
        self.state = "running"
        self.thread = threading.Thread(target=self._run, name=f"seq-{session.node_id}", daemon=True)

    def start(self) -> None:
        self.thread.start()

    @property
    def active(self) -> bool:
        return self.thread.is_alive()

    def _progress(self, state: str, text: str, group: int = 0, detail: str = "", note: bool = True) -> None:
        self.state = state
        self.bus.publish(SequenceProgress(self.session.node_id, self.session.name, state, text, group, detail))
        if note:
            self.session.note(f"{self.label}: {text}" + (f"  ({detail})" if detail else ""))

    def _wait_success(self, start: int, timeout: float) -> str | None:
        end = time.monotonic() + timeout
        with self._cond:
            while True:
                for m in self.success.finditer(self._buf, start):
                    line = _line_at(self._buf, m.start())
                    if not MENU_LINE.match(line):
                        return line
                left = end - time.monotonic()
                if left <= 0 or self._stop.is_set() or not self.session.alive:
                    return None
                self._cond.wait(min(left, 0.2))

    def _last_lines(self, start: int, n: int = 2) -> str:
        with self._cond:
            lines = [l.strip() for l in self._buf[start:].splitlines() if l.strip()]
        return " | ".join(lines[-n:]) or "no output"

    def _run(self) -> None:
        s = self.session
        if not self._wait_shell():
            self._progress("failed", f"shell not available ({s.message or s.status})", note=False)
            return
        s.listeners.append(self._on_output)
        groups = self.spec.groups or [[k] for k in self.spec.steps]
        try:
            self._progress("running", f"starting {self.spec.start}")
            mark = self._send(self.spec.start)
            if not self._wait_response(mark, self.spec.start, max(self.timeout, 30)):
                return self._fail(f"{self.spec.start} printed nothing", 0, self._last_lines(mark))
            for gi, group in enumerate(groups, 1):
                name = "-".join(group)
                with self._cond:
                    group_start = len(self._buf)
                for key in group:
                    if self._stop.wait(self.delay):
                        return self._fail("stopped", gi)
                    self._progress("running", f"{name}: sending {key}", gi, note=False)
                    mark = self._send(key)
                    if not self._wait_response(mark, key, self.timeout):
                        return self._fail(f"{name}: no response after '{key}'", gi, self._last_lines(group_start))
                line = self._wait_success(group_start, self.timeout)
                if line is None:
                    return self._fail(f"{name}: no success message", gi, self._last_lines(group_start))
                self._progress("pass", f"{name} ✔", gi, line)
            if self.spec.exit:
                self._send(self.spec.exit)
            self._progress("done", f"PASS – all {len(groups)} step(s) succeeded")
        finally:
            if self._on_output in s.listeners:
                s.listeners.remove(self._on_output)

    def _fail(self, text: str, group: int, detail: str = "") -> None:
        if self._stop.is_set():
            self._progress("stopped", "stopped", group)
        else:
            self._progress("failed", f"FAIL – {text}", group, detail)


class CommandCheck(_ShellStep):
    def __init__(self, session, command: str, bus: EventBus, label: str,
                 settle_s: float = 1.0, timeout_s: float = 15.0) -> None:
        super().__init__(session, bus, settle_s)
        self.command, self.label, self.timeout = command, label, timeout_s
        threading.Thread(target=self._run, name=f"check-{session.node_id}", daemon=True).start()

    def _result(self, ok: bool, detail: str) -> None:
        self.bus.publish(CheckResult(self.session.node_id, self.session.name, self.label, ok, detail))
        self.session.note(f"{self.label}: {'PASS' if ok else 'FAIL'} – {detail}")

    def _run(self) -> None:
        s = self.session
        if not self._wait_shell():
            self.bus.publish(CheckResult(s.node_id, s.name, self.label, False,
                                         f"shell not available ({s.message or s.status})"))
            return
        s.listeners.append(self._on_output)
        try:
            lines_sent = [l for l in self.command.splitlines() if l.strip()]
            mark = self._send("\n".join(lines_sent))
            answered = self._wait_response(mark, self.command, self.timeout)
            with self._cond:
                out = self._buf[mark:]
            lines = [l.strip() for l in out.splitlines() if l.strip()]
            lines = [l for l in lines if not _PROMPT_ONLY.match(l) and not any(c.strip() in l for c in lines_sent)]
            bad = [l for l in lines if FAIL_LINE.search(l)]
            if bad:
                self._result(False, bad[0])
            elif not answered and not lines:
                self._result(False, "no answer from the device")
            else:
                self._result(True, lines[-1] if lines else "done, no errors printed")
        finally:
            if self._on_output in s.listeners:
                s.listeners.remove(self._on_output)
