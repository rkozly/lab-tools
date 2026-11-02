"""
Crash dumps (ramdumps) on devices: list, watch for new files, pull to the
PC, and clear. Works for any adb device; the default folder is
/data/vendor/ramdump.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..transports import base as tr
from .events import Event, EventBus
from .models import NodeView, Status

DEFAULT_PATH = "/data/vendor/ramdump"


@dataclass
class DumpFile:
    name: str
    size: int
    date: str


@dataclass
class DumpListing(Event):
    node_id: str
    name: str
    path: str
    files: list = field(default_factory=list)
    error: str = ""
    new: list = field(default_factory=list)   # names that appeared since the last check
    missing_dir: bool = False


@dataclass
class DumpAction(Event):
    node_id: str
    name: str
    action: str      # pull | clear
    ok: bool
    message: str
    dest: str = ""


def parse_listing(text: str) -> list[DumpFile]:
    """`ls -l` lines (from find -exec ls -l) -> files."""
    files = []
    for line in text.splitlines():
        parts = line.split(None, 8)
        if len(parts) < 9 or not parts[0].startswith("-"):
            continue
        try:
            size = int(parts[4])
        except ValueError:
            continue
        name = parts[8].strip()
        if name.startswith("./"):
            name = name[2:]
        files.append(DumpFile(name, size, " ".join(parts[5:8])))
    return sorted(files, key=lambda f: f.name)


class RamdumpService:
    def __init__(self, bus: EventBus, registry) -> None:
        self.bus, self.registry = bus, registry
        self._known: dict[tuple[str, str], set[str]] = {}
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None

    # --- helpers ------------------------------------------------------------

    def _adb(self, node: NodeView) -> list[str]:
        serial = self.registry.serial_of(node)
        tr.ensure_root(serial)
        return tr.exec_argv(tr.connect_spec(node.config, serial), "true")[:3]  # adb -s <serial>

    def _sh(self, node: NodeView, cmd: str, timeout: float = 60) -> tuple[int, str]:
        argv = self._adb(node) + ["shell", cmd]
        res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                             creationflags=tr.CREATE_NO_WINDOW, cwd=os.path.expanduser("~"),
                             encoding="utf-8", errors="replace")
        return res.returncode, (res.stdout or "") + (res.stderr or "")

    @staticmethod
    def usable(node: NodeView) -> bool:
        return node.kind == "device" and node.state.status == Status.CONNECTED

    def _bg(self, fn, *args) -> None:
        threading.Thread(target=fn, args=args, daemon=True).start()

    # --- list / watch -------------------------------------------------------

    def list(self, nodes: list[NodeView], path: str) -> None:
        for n in nodes:
            self._bg(self._list_one, n, path)

    def _list_one(self, node: NodeView, path: str) -> None:
        q = shlex.quote(path)
        cmd = (f"if [ -d {q} ]; then cd {q} && find . -type f -exec ls -l {{}} \\; 2>/dev/null; "
               f"echo __END__; else echo __NODIR__; fi")
        try:
            code, out = self._sh(node, cmd)
        except (OSError, subprocess.TimeoutExpired, tr.TransportError) as e:
            self.bus.publish(DumpListing(node.id, node.name, path, error=str(e)))
            return
        if "__NODIR__" in out:
            self._known[(node.id, path)] = set()
            self.bus.publish(DumpListing(node.id, node.name, path, missing_dir=True))
            return
        if "__END__" not in out:
            self.bus.publish(DumpListing(node.id, node.name, path, error=out.strip()[-200:] or f"exit {code}"))
            return
        files = parse_listing(out)
        key = (node.id, path)
        names = {f.name for f in files}
        new = sorted(names - self._known[key]) if key in self._known else []
        self._known[key] = names
        self.bus.publish(DumpListing(node.id, node.name, path, files, new=new))

    def watch(self, get_nodes, get_path, interval_s: float) -> None:
        """Re-list every interval; get_nodes()/get_path() are read each round."""
        self.stop_watch()
        self._watch_stop = threading.Event()
        stop = self._watch_stop

        def loop() -> None:
            while not stop.is_set():
                path = get_path()
                for n in get_nodes():
                    if stop.is_set():
                        break
                    self._list_one(n, path)
                stop.wait(interval_s)

        self._watch_thread = threading.Thread(target=loop, name="ramdump-watch", daemon=True)
        self._watch_thread.start()

    def stop_watch(self) -> None:
        self._watch_stop.set()

    # --- pull / clear -------------------------------------------------------

    def pull(self, nodes: list[NodeView], path: str, dest_root: Path,
             files: dict[str, list[str]] | None = None) -> None:
        for n in nodes:
            names = None if files is None else files.get(n.id)
            if files is not None and not names:
                continue
            self._bg(self._pull_one, n, path, dest_root, names)

    def _pull_one(self, node: NodeView, path: str, dest_root: Path, names: list[str] | None) -> None:
        dest = Path(dest_root) / f"{datetime.now():%Y%m%d_%H%M%S}_{node.name}_ramdump"
        try:
            dest.mkdir(parents=True, exist_ok=True)
            adb = self._adb(node)
            sources = [f"{path.rstrip('/')}/{n}" for n in names] if names else [path]
            log = []
            ok = True
            for src in sources:
                res = subprocess.run(adb + ["pull", src, str(dest)], capture_output=True, text=True, timeout=1800,
                                     stdin=subprocess.DEVNULL, creationflags=tr.CREATE_NO_WINDOW,
                                     encoding="utf-8", errors="replace")
                log.append((res.stdout + res.stderr).strip().splitlines()[-1:] or [""])
                ok = ok and res.returncode == 0
            msg = "; ".join(l[0] for l in log if l and l[0]) or ("pulled" if ok else "pull failed")
        except (OSError, subprocess.TimeoutExpired, tr.TransportError) as e:
            ok, msg = False, str(e)
        self.bus.publish(DumpAction(node.id, node.name, "pull", ok, msg, str(dest)))

    def clear(self, nodes: list[NodeView], path: str) -> None:
        for n in nodes:
            self._bg(self._clear_one, n, path)

    def _clear_one(self, node: NodeView, path: str) -> None:
        q = path.rstrip("/")
        if q in ("", "/", "/data", "/data/vendor") or not q.startswith("/"):
            self.bus.publish(DumpAction(node.id, node.name, "clear", False, f"refusing to clear '{path}'"))
            return
        sq = shlex.quote(q)
        cmd = f"[ -d {sq} ] && rm -rf {sq}/* {sq}/.[!.]* 2>/dev/null; sync; echo __DONE__"
        try:
            code, out = self._sh(node, cmd)
            ok = "__DONE__" in out
            msg = "cleared" if ok else (out.strip()[-200:] or f"exit {code}")
        except (OSError, subprocess.TimeoutExpired, tr.TransportError) as e:
            ok, msg = False, str(e)
        self.bus.publish(DumpAction(node.id, node.name, "clear", ok, msg))
        self._list_one(node, path)
