"""
Pollers and stores for live data.

A Poller runs one command on one node every interval and hands the parsed
result to a callback. Stores keep a bounded history that views read from.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from ..transports import base as tr
from .events import Event, EventBus
from .models import NodeView, Status
from .runner import clean


@dataclass
class SampleAdded(Event):
    node_id: str
    sample: "Sample"


@dataclass
class PollerState(Event):
    key: str
    node_id: str
    running: bool
    error: str = ""


@dataclass
class Sample:
    node_id: str
    time: datetime
    metrics: dict
    raw: str = ""


class SampleStore:
    def __init__(self, bus: EventBus, max_per_node: int = 3600) -> None:
        self.bus = bus
        self._data: dict[str, deque[Sample]] = {}
        self._max = max_per_node
        self._lock = threading.Lock()

    def add(self, sample: Sample) -> None:
        with self._lock:
            self._data.setdefault(sample.node_id, deque(maxlen=self._max)).append(sample)
        self.bus.publish(SampleAdded(sample.node_id, sample))

    def previous(self, node_id: str) -> Sample | None:
        with self._lock:
            d = self._data.get(node_id)
            return d[-2] if d and len(d) > 1 else None

    def latest(self, node_id: str) -> Sample | None:
        with self._lock:
            d = self._data.get(node_id)
            return d[-1] if d else None

    def history(self, node_id: str, key: str, n: int = 60) -> list[float]:
        with self._lock:
            d = list(self._data.get(node_id, ()))[-n:]
        return [s.metrics[key] for s in d if isinstance(s.metrics.get(key), (int, float))]

    def export_csv(self, node_id: str, path: str) -> int:
        with self._lock:
            rows = list(self._data.get(node_id, ()))
        keys = sorted({k for s in rows for k, v in s.metrics.items() if isinstance(v, (int, float, str, bool))})
        with open(path, "w", encoding="utf-8") as f:
            f.write(",".join(["time"] + keys) + "\n")
            for s in rows:
                f.write(",".join([s.time.isoformat(timespec="seconds")] +
                                 [str(s.metrics.get(k, "")) for k in keys]) + "\n")
        return len(rows)


class Poller:
    def __init__(self, key: str, node: NodeView, spec, command: str, interval_s: float,
                 on_output, bus: EventBus) -> None:
        self.key, self.node, self.spec, self.command = key, node, spec, command
        self.interval_s, self.on_output, self.bus = interval_s, on_output, bus
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._thread = threading.Thread(target=self._loop, name=f"poll-{key}-{node.id}", daemon=True)

    def start(self) -> None:
        self.bus.publish(PollerState(self.key, self.node.id, True))
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        p = self._proc
        if p and p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass

    @property
    def running(self) -> bool:
        return self._thread.is_alive() and not self._stop.is_set()

    def _loop(self) -> None:
        error = ""
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                argv = tr.exec_argv(self.spec, self.command)
                self._proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                              stderr=subprocess.STDOUT, creationflags=tr.CREATE_NO_WINDOW,
                                              cwd=os.path.expanduser("~"), env=tr.child_env())
                try:
                    out, _ = self._proc.communicate(timeout=max(10.0, self.interval_s * 5))
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    out, _ = self._proc.communicate()
                if self._stop.is_set():
                    break
                text = clean(out.decode("utf-8", errors="replace").replace("\r\n", "\n"))
                self.on_output(self.node, text)
                if error:
                    error = ""
                    self.bus.publish(PollerState(self.key, self.node.id, True))
            except tr.TransportError as e:
                error = str(e)
                self.bus.publish(PollerState(self.key, self.node.id, False, error))
                break
            except Exception as e:
                if str(e) != error:
                    error = str(e)
                    self.bus.publish(PollerState(self.key, self.node.id, True, error))
            self._stop.wait(max(0.1, self.interval_s - (time.monotonic() - t0)))
        self.bus.publish(PollerState(self.key, self.node.id, False, error))


class PollerManager:
    """Start/stop pollers keyed by (feature key, node id)."""

    def __init__(self, bus: EventBus, registry) -> None:
        self.bus, self.registry = bus, registry
        self._pollers: dict[tuple[str, str], Poller] = {}
        self._lock = threading.Lock()

    def start(self, key: str, node: NodeView, command: str, interval_s: float, on_output) -> str | None:
        """Returns an error message, or None when started."""
        if node.kind == "device" and node.state.status != Status.CONNECTED:
            return f"{node.name}: {node.state.status.value}"
        try:
            spec = tr.connect_spec(node.config, self.registry.serial_of(node))
            tr.exec_argv(spec, "true")  # validate early (adb present, configured)
        except tr.TransportError as e:
            return f"{node.name}: {e}"
        self.stop(key, node.id)
        p = Poller(key, node, spec, command, interval_s, on_output, self.bus)
        with self._lock:
            self._pollers[(key, node.id)] = p
        p.start()
        return None

    def stop(self, key: str, node_id: str | None = None) -> None:
        with self._lock:
            items = [(k, p) for k, p in self._pollers.items()
                     if k[0] == key and (node_id is None or k[1] == node_id)]
            for k, _ in items:
                del self._pollers[k]
        for _, p in items:
            p.stop()

    def running(self, key: str) -> list[str]:
        with self._lock:
            return [k[1] for k, p in self._pollers.items() if k[0] == key and p.running]

    def stop_all(self) -> None:
        with self._lock:
            items = list(self._pollers.values())
            self._pollers.clear()
        for p in items:
            p.stop()


# --- beacons ------------------------------------------------------------------

@dataclass
class BeaconObservation:
    observer_id: str
    time: datetime
    bssid: str
    ssid: str = ""
    channel: str = ""
    freq_mhz: float | None = None
    rssi_dbm: float | None = None
    matched_node_id: str | None = None
    device_type: str = "?"
    associated: bool = False


@dataclass
class BeaconsUpdated(Event):
    observer_id: str


class BeaconStore:
    """Latest observation per (observer, bssid)."""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._data: dict[tuple[str, str], BeaconObservation] = {}
        self._lock = threading.Lock()

    def replace(self, observer_id: str, items: list[BeaconObservation]) -> None:
        with self._lock:
            for o in items:
                self._data[(observer_id, o.bssid)] = o
        self.bus.publish(BeaconsUpdated(observer_id))

    def all(self) -> list[BeaconObservation]:
        with self._lock:
            return list(self._data.values())

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
        self.bus.publish(BeaconsUpdated(""))
