"""
Interactive shells: one long-lived shell per node (adb shell -tt, or ssh
with a pty). The Console shows each as a pane you can type into; Run and
presets are sent into open shells instead of starting new jobs.
"""

from __future__ import annotations

import codecs
import os
import re
import threading
from dataclasses import dataclass

from ..transports import base as tr
from .events import Event, EventBus
from .models import NodeView, Status
from .runner import OutputBuffer, _kill_tree, clean

# A numbered menu waiting for input, e.g. QCMAP_CLI's "Option >".
MENU_PROMPT = re.compile(r"(?i)(option\s*>|select an option[^\n]*[:>]|enter (your )?choice[^\n]*[:>])\s*$")
MENU_PROGRAMS = ("QCMAP_CLI",)

# Screen clears (watch, clear, top) -> the pane is cleared instead of growing.
_CLEAR = re.compile(r"\x1b\[H\x1b\[2J|\x1b\[2J|\x1b\[H\x1b\[J|\x1b\[3J|\x1bc")
_ESC_DONE = re.compile(r"\x1b(\[[0-9;?]*[ -/]*[@-~]|[()][0-9A-Za-z]|[=>c78DEHM])")


@dataclass
class ShellOutput(Event):
    session: "ShellSession"
    text: str
    clear: bool = False


@dataclass
class ShellState(Event):
    session: "ShellSession"


class ShellSession:
    def __init__(self, node: NodeView, argv: list[str], bus: EventBus) -> None:
        self.node_id, self.name, self.argv, self.bus = node.id, node.name, argv, bus
        self.status = "starting"  # starting | open | closed
        self.message = ""
        self.output = OutputBuffer()
        self.proc = None
        self._carry = ""
        self._lock = threading.Lock()
        self.listeners: list = []  # fn(text) called with every piece of device output
        self.device_id = node.id   # node_id may be a second-shell key such as "stats:ap"
        self.adb: list[str] = []   # adb -s <serial>: used to find this shell's tty
        self.tty = ""              # e.g. /dev/pts/0 (found by comparing /dev/pts before and after)

    @property
    def alive(self) -> bool:
        return self.status == "open" and self.proc is not None and self.proc.poll() is None

    def start(self, prepare: list[str] | None = None) -> None:
        """Start the shell; `prepare` (e.g. adb wait-for-device root) runs first, in the background."""
        if prepare:
            threading.Thread(target=self._prepare_then_start, args=(prepare,), daemon=True).start()
            return
        self._start_now()

    def _prepare_then_start(self, prepare: list[str]) -> None:
        import subprocess
        self.output.append(f"$ {' '.join(prepare[1:])}\n")
        self.bus.publish(ShellOutput(self, f"$ adb {' '.join(prepare[1:])}\n"))
        try:
            res = subprocess.run(prepare, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                                 creationflags=tr.CREATE_NO_WINDOW, cwd=os.path.expanduser("~"))
            msg = (res.stdout + res.stderr).strip()
            if msg:
                self.bus.publish(ShellOutput(self, msg + "\n"))
            # adbd restarts after `root`; wait until it is back
            subprocess.run(prepare[:3] + ["wait-for-device"], timeout=30, stdin=subprocess.DEVNULL,
                           capture_output=True, creationflags=tr.CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired:
            self.bus.publish(ShellOutput(self, "[LabHub] adb root timed out, opening shell anyway\n"))
        except OSError as e:
            self.bus.publish(ShellOutput(self, f"[LabHub] adb root failed: {e}\n"))
        before = self._pts() if self.adb else None
        if self.status == "starting":
            self._start_now()
        if before is None:
            return
        import time
        for _ in range(5):  # the new /dev/pts entry is this shell's terminal
            time.sleep(0.8)
            new = (self._pts() or set()) - before
            if len(new) == 1:
                self.tty = f"/dev/pts/{new.pop()}"
                return
            if len(new) > 1:
                return  # another adb shell opened at the same moment: cannot tell

    def _pts(self) -> set[str] | None:
        import subprocess
        try:
            res = subprocess.run(self.adb + ["shell", "ls /dev/pts"], capture_output=True, text=True, timeout=10,
                                 stdin=subprocess.DEVNULL, creationflags=tr.CREATE_NO_WINDOW,
                                 cwd=os.path.expanduser("~"))
        except (OSError, subprocess.TimeoutExpired):
            return None
        return {x for x in res.stdout.split() if x.isdigit()}

    def _start_now(self) -> None:
        try:
            self.proc = tr.popen(self.argv, stdin=True)
        except OSError as e:
            self._set("closed", str(e))
            return
        self._set("open", "")
        threading.Thread(target=self._read, name=f"shell-{self.node_id}", daemon=True).start()
        queued, self._queued = getattr(self, "_queued", ""), ""
        if queued:
            self.write(queued)

    def write(self, text: str) -> bool:
        with self._lock:
            if self.status == "starting":
                self._queued = getattr(self, "_queued", "") + text  # sent once the shell is up
                return True
            if not self.alive:
                return False
            try:
                self.proc.stdin.write(text.encode("utf-8"))
                self.proc.stdin.flush()
                return True
            except OSError as e:
                self._set("closed", f"write failed: {e}")
                return False

    def interrupt(self) -> bool:
        return self.write("\x03")

    def send_key(self, key: str) -> bool:
        """Raw control key: \x03 Ctrl+C, \x1a Ctrl+Z, \x04 Ctrl+D, \x1c Ctrl+\\."""
        return self.write(key)

    def tail(self, n: int = 400) -> str:
        return self.output.text()[-n:]

    def in_menu(self) -> bool:
        return bool(MENU_PROMPT.search(self.tail().rstrip("\n") + " ")) or bool(
            MENU_PROMPT.search(self.tail().rstrip()))

    def close(self) -> None:
        if self.proc is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            _kill_tree(self.proc)
        if self.status != "closed":
            self._set("closed", "closed")

    def _set(self, status: str, message: str) -> None:
        self.status, self.message = status, message
        self.bus.publish(ShellState(self))

    def _emit(self, raw: str) -> None:
        buf = self._carry + raw.replace("\r\n", "\n")
        i = buf.rfind("\x1b")
        if i != -1 and not _ESC_DONE.match(buf, i):
            self._carry, buf = buf[i:], buf[:i]   # escape sequence split across reads
        else:
            self._carry = ""
        parts = _CLEAR.split(buf)
        clear = len(parts) > 1
        text = clean(parts[-1])
        if clear:
            self.output.clear()
        if text or clear:
            self.output.append(text)
            self.bus.publish(ShellOutput(self, text, clear))
        for fn in list(self.listeners):
            try:
                fn(text)
            except Exception:
                pass

    def note(self, text: str) -> None:
        """A LabHub message in this shell's pane (not sent to the device)."""
        self.bus.publish(ShellOutput(self, f"\n[LabHub] {text}\n"))

    def _read(self) -> None:
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        fd = self.proc.stdout
        while True:
            chunk = fd.read1(4096) if hasattr(fd, "read1") else os.read(fd.fileno(), 4096)
            if not chunk:
                break
            self._emit(dec.decode(chunk))
        code = self.proc.wait()
        if self.status != "closed":
            self._set("closed", f"shell ended (exit {code})")


class ShellManager:
    def __init__(self, bus: EventBus, registry) -> None:
        self.bus, self.registry = bus, registry
        self.sessions: dict[str, ShellSession] = {}

    def open(self, node: NodeView, key: str | None = None, name: str | None = None) -> str | None:
        """Open (or keep) a shell for node. Returns an error message or None.
        key: session id (default node.id), e.g. "stats:ap" for a second shell."""
        key = key or node.id
        s = self.sessions.get(key)
        if s and s.alive:
            return None
        if node.kind != "backbone" and node.state.status != Status.CONNECTED:
            return f"{node.name}: {node.state.status.value}"
        try:
            spec = tr.connect_spec(node.config, self.registry.serial_of(node))
            argv = tr.session_argv(spec, tty=True)
        except tr.TransportError as e:
            return f"{node.name}: {e}"
        s = ShellSession(node, argv, self.bus)
        s.node_id, s.device_id = key, node.id
        if name:
            s.name = name
        self.sessions[key] = s
        prepare = None
        if spec.type == "adb":
            prepare = argv[:3] + ["wait-for-device", "root"]  # adb -s <serial> wait-for-device root
            s.adb = argv[:3]
        s.start(prepare)
        return None if s.status != "closed" else f"{node.name}: {s.message}"

    def is_open(self, node_id: str) -> bool:
        s = self.sessions.get(node_id)
        return bool(s and (s.alive or s.status == "starting"))

    def write(self, node_id: str, text: str) -> bool:
        s = self.sessions.get(node_id)
        return bool(s and s.write(text))

    def interrupt(self, node_id: str) -> bool:
        """Ctrl+C. Over adb the byte alone often does nothing (adb on Windows
        may not pass it through), so the foreground job on the shell's tty is
        also sent SIGINT from a second adb connection."""
        s = self.sessions.get(node_id)
        if not (s and s.interrupt()):
            return False
        self._signal_later(node_id, "INT")
        return True

    def interrupt_all(self) -> int:
        return sum(1 for nid in list(self.sessions) if self.interrupt(nid))

    def send_key(self, node_id: str, key: str) -> bool:
        s = self.sessions.get(node_id)
        if not (s and s.send_key(key)):
            return False
        if key == "\x1a":
            threading.Thread(target=self.suspend, args=(node_id,), daemon=True).start()
            return True
        sig = {"\x03": "INT", "\x1c": "QUIT"}.get(key)
        if sig:
            self._signal_later(node_id, sig)
        return True

    def _signal_later(self, node_id: str, sig: str) -> None:
        threading.Thread(target=self.signal_foreground, args=(node_id, sig), daemon=True).start()

    def _pick_script(self, s: ShellSession) -> str:
        """Device shell code that sets $PIDS to the job(s) running in this shell:
        everything on the shell's tty except the shell (the session leader), or,
        when the tty is not known and a menu is showing, the menu program."""
        if s.tty:
            return (f"PIDS=; for d in /proc/[0-9]*; do p=${{d#/proc/}}; "
                    f"[ \"$(readlink $d/fd/0 2>/dev/null)\" = {s.tty} ] || continue; "
                    f"[ \"$(cut -d' ' -f6 $d/stat 2>/dev/null)\" = \"$p\" ] && continue; "
                    f"PIDS=\"$PIDS $p\"; done; ")
        if s.in_menu():
            return f"PIDS=$(pidof {' '.join(MENU_PROGRAMS)}); "
        return ""

    def _on_device(self, node_id: str, script: str) -> str | None:
        """Run script over a second adb connection; stdout, or None on error."""
        import subprocess
        s = self.sessions.get(node_id)
        node = self.registry.get(s.device_id if s else node_id)
        if s is None or node is None or not s.adb:
            return None
        try:
            res = subprocess.run(tr.exec_argv(tr.ConnectSpec("adb", {"serial": self.registry.serial_of(node)}),
                                              script),
                                 capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL,
                                 creationflags=tr.CREATE_NO_WINDOW, cwd=os.path.expanduser("~"))
        except (OSError, subprocess.TimeoutExpired, tr.TransportError) as e:
            s.note(f"second adb connection failed: {e}")
            return None
        return res.stdout

    def _learn_tty(self, s: ShellSession, out: str) -> None:
        m = re.search(r"TTY=(/dev/pts/\d+)", out or "")
        if m and not s.tty:
            s.tty = m.group(1)

    def signal_foreground(self, node_id: str, sig: str = "INT") -> str:
        """kill -<sig> the job running in the shell. Returns the pids."""
        import time
        s = self.sessions.get(node_id)
        if s is None or not s.alive:
            return ""
        time.sleep(0.6)  # give the plain Ctrl+C byte a chance first
        if not s.in_menu() and s.tail(4).rstrip().endswith(("#", "$")):
            return ""     # already back at a shell prompt
        pick = self._pick_script(s)
        if not pick:
            return ""
        out = self._on_device(node_id, pick + f"for p in $PIDS; do kill -{sig} $p 2>/dev/null && echo $p; done; "
                                              f"set -- $PIDS; [ -n \"$1\" ] && echo TTY=$(readlink /proc/$1/fd/0)")
        self._learn_tty(s, out)
        pids = [x for x in (out or "").split() if x.isdigit()]
        if pids:
            s.note(f"SIG{sig} sent to {', '.join(pids)}")
            if sig == "INT":
                s.write("\n")  # fresh prompt
        return " ".join(pids)

    def suspend(self, node_id: str) -> bool:
        """Ctrl+Z that works over adb: the running job (e.g. QCMAP_CLI) is
        stopped and the shell gets its prompt back; `fg` resumes it.
        SIGTSTP first; a menu that ignores it gets SIGSTOP (cannot be ignored).
        Blocking (a few seconds) - call from a worker thread."""
        import time
        s = self.sessions.get(node_id)
        if s is None or not s.alive:
            return False
        was_menu = s.in_menu()
        time.sleep(0.8)  # the Ctrl+Z byte may have worked already
        if (was_menu and not s.in_menu()) or (not was_menu and s.tail(4).rstrip().endswith(("#", "$"))):
            return True
        pick = self._pick_script(s)
        if not pick:
            s.note("Ctrl+Z: nothing to suspend found")
            return False
        out = self._on_device(node_id, pick +
                              "for p in $PIDS; do kill -TSTP $p 2>/dev/null; done; sleep 0.5; "
                              "for p in $PIDS; do [ \"$(cut -d' ' -f3 /proc/$p/stat 2>/dev/null)\" = T ] "
                              "|| kill -STOP $p 2>/dev/null; echo $p; done; "
                              "set -- $PIDS; [ -n \"$1\" ] && echo TTY=$(readlink /proc/$1/fd/0)")
        self._learn_tty(s, out)
        pids = [x for x in (out or "").split() if x.isdigit()]
        if not pids:
            s.note("Ctrl+Z: no running job found to suspend")
            return False
        for _ in range(10):   # the shell prints "[1]+ Stopped ..." and its prompt
            time.sleep(0.3)
            if not s.in_menu():
                break
        if s.in_menu():
            s.write("\n")
            time.sleep(0.8)
        if not s.in_menu():
            s.note(f"suspended {', '.join(pids)} – stopped in the background; type  fg  to go back to it")
            return True
        # the shell did not take the terminal back (no job control): do not leave the menu frozen
        self._on_device(node_id, "".join(f"kill -CONT {p} 2>/dev/null; " for p in pids))
        s.note("Ctrl+Z: this shell has no job control, so QCMAP_CLI cannot go to the background – "
               "it was resumed. Use ^C or Exit to close it.")
        return False

    def leave_menu(self, node_id: str, kill: bool = True) -> str:
        """Get the shell out of a menu program (QCMAP_CLI).

        kill=False: suspend it (Ctrl+Z, enforced) - it stays in the background, `fg` resumes it.
        kill=True:  Ctrl+C, then Ctrl+Z, then killall over a second adb connection.
        Returns what worked. Blocking (a few seconds) - call from a worker thread.
        """
        import subprocess
        import time
        s = self.sessions.get(node_id)
        if s is None or not s.alive:
            return "no shell"
        if not s.in_menu():
            return "not in a menu"
        if not kill:
            s.note("sending QCMAP_CLI to the background (Ctrl+Z)")
            s.send_key("\x1a")
            return "Ctrl+Z" if self.suspend(node_id) else "still in menu"
        for key, name in (("\x03", "Ctrl+C"), ("\x1a", "Ctrl+Z")):
            s.note(f"leaving menu: {name}")
            s.send_key(key)
            if key == "\x1a":
                self.suspend(node_id)
            else:
                time.sleep(1.5)
            if not s.in_menu():
                return name
        node = self.registry.get(s.device_id if s else node_id)
        serial = self.registry.serial_of(node) if node else ""
        if not serial:
            return "still in menu"
        kills = "; ".join(f"killall {p} 2>/dev/null || pkill -f {p}" for p in MENU_PROGRAMS)
        s.note(f"menu ignored Ctrl+C / Ctrl+Z: closing it from a second adb connection ({kills})")
        try:
            subprocess.run(tr.exec_argv(tr.ConnectSpec("adb", {"serial": serial}), kills),
                           capture_output=True, timeout=15, stdin=subprocess.DEVNULL,
                           creationflags=tr.CREATE_NO_WINDOW, cwd=os.path.expanduser("~"))
        except (OSError, subprocess.TimeoutExpired, tr.TransportError) as e:
            s.note(f"killall failed: {e}")
            return "still in menu"
        time.sleep(1.0)
        s.write("\n")
        time.sleep(0.8)
        return "killall" if not s.in_menu() else "still in menu"

    def close(self, node_id: str) -> None:
        s = self.sessions.pop(node_id, None)
        if s:
            s.close()

    def close_all(self) -> None:
        for nid in list(self.sessions):
            self.close(nid)
