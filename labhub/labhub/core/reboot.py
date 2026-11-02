"""
Reboot devices.

If the device has a live shell (it may be inside a menu such as QCMAP_CLI),
LabHub first sends Ctrl+C and Ctrl+Z to leave the menu and then types the
reboot command. Without a shell it runs `adb -s <serial> reboot`. If the
typed command has no effect within a short time, `adb reboot` is used.
Afterwards the device is followed: offline -> back online, or a timeout.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass

from ..transports import base as tr
from .events import Event, EventBus, NodeStatusChanged
from .models import NodeView

REBOOT_COMMAND = "reboot"
BACK_TIMEOUT_S = 300
SHELL_GRACE_S = 20


@dataclass
class RebootProgress(Event):
    node_id: str
    name: str
    state: str   # sent | offline | back | failed
    text: str


class RebootService:
    def __init__(self, bus: EventBus, registry, shells) -> None:
        self.bus, self.registry, self.shells = bus, registry, shells
        self._pending: dict[str, dict] = {}
        self._lock = threading.Lock()
        bus.subscribe(NodeStatusChanged, self._on_status)

    def is_rebooting(self, node_id: str) -> bool:
        with self._lock:
            return node_id in self._pending

    def reboot(self, node: NodeView, command: str = REBOOT_COMMAND) -> None:
        with self._lock:
            if node.id in self._pending:
                return
            self._pending[node.id] = {"t0": time.monotonic(), "offline": False, "name": node.name}
        threading.Thread(target=self._run, args=(node, command), name=f"reboot-{node.id}", daemon=True).start()

    # --- internals ----------------------------------------------------------

    def _publish(self, node_id: str, name: str, state: str, text: str) -> None:
        self.bus.publish(RebootProgress(node_id, name, state, text))

    def _offline(self, node_id: str) -> bool:
        with self._lock:
            p = self._pending.get(node_id)
            return bool(p and p["offline"])

    def _adb_reboot(self, serial: str) -> str | None:
        try:
            res = subprocess.run(tr.exec_argv(tr.ConnectSpec("adb", {"serial": serial}), "true")[:3] + ["reboot"],
                                 capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
                                 creationflags=tr.CREATE_NO_WINDOW, cwd=os.path.expanduser("~"))
            if res.returncode != 0:
                return (res.stdout + res.stderr).strip() or f"adb reboot exit {res.returncode}"
        except (OSError, subprocess.TimeoutExpired, tr.TransportError) as e:
            return str(e)
        return None

    def _fail(self, node: NodeView, text: str) -> None:
        with self._lock:
            self._pending.pop(node.id, None)
        self._publish(node.id, node.name, "failed", text)

    def _run(self, node: NodeView, command: str) -> None:
        serial = self.registry.serial_of(node)
        tr.forget_root(serial)  # adbd starts unprivileged again after a reboot
        s = self.shells.sessions.get(node.id)
        if s is not None and s.alive:
            how = self.shells.leave_menu(node.id)
            if how == "still in menu":
                s.note("could not leave the menu, using adb reboot")
                err = self._adb_reboot(serial)
                if err:
                    return self._fail(node, f"menu could not be closed and adb reboot failed: {err}")
                self._publish(node.id, node.name, "sent", "menu would not close, used adb reboot")
                return self._watch(node)
            left = {"not in a menu": "", "Ctrl+C": " (left QCMAP with Ctrl+C)", "Ctrl+Z": " (left QCMAP with Ctrl+Z)",
                    "killall": " (QCMAP closed with killall)"}.get(how, "")
            s.write(command + "\n")
            self._publish(node.id, node.name, "sent", f"'{command}' typed in the shell{left}")
            end = time.monotonic() + SHELL_GRACE_S
            while time.monotonic() < end and s.alive and not self._offline(node.id):
                time.sleep(0.3)
            if s.alive and not self._offline(node.id):
                err = self._adb_reboot(serial)
                if err:
                    return self._fail(node, f"shell reboot had no effect and adb reboot failed: {err}")
                self._publish(node.id, node.name, "sent", "shell reboot had no effect, used adb reboot")
        else:
            err = self._adb_reboot(serial)
            if err:
                return self._fail(node, f"adb reboot failed: {err}")
            self._publish(node.id, node.name, "sent", "adb reboot sent")
        self._watch(node)

    def _watch(self, node: NodeView) -> None:
        deadline = time.monotonic() + BACK_TIMEOUT_S
        while time.monotonic() < deadline:
            if not self.is_rebooting(node.id):
                return
            time.sleep(1)
        if self.is_rebooting(node.id):
            self._fail(node, f"not back online after {BACK_TIMEOUT_S} s")

    def _on_status(self, e: NodeStatusChanged) -> None:
        event = None
        with self._lock:
            p = self._pending.get(e.node_id)
            if p is None:
                return
            if e.new != "connected":
                if not p["offline"]:
                    p["offline"] = True
                    event = ("offline", "went offline, rebooting…")
            elif p["offline"]:
                secs = time.monotonic() - p["t0"]
                del self._pending[e.node_id]
                event = ("back", f"back online after {secs:.0f} s")
        if event:
            self._publish(e.node_id, e.name, *event)
