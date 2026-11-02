"""
Registry: joins the lab configuration with what is connected right now and
produces one NodeView per tile, in display order:
    AP, AP's backbone, CPE1, CPE1's backbone, ..., unused backbones, unassigned
"""

from __future__ import annotations

import threading
from datetime import datetime

from . import config as cfg
from .events import EventBus, LabLoaded, NodesChanged, NodeStatusChanged
from .models import (AdbDevice, BackboneConfig, DeviceConfig, Lab, NodeState,
                     NodeView, Status)


def _sort_key(dev: DeviceConfig):
    digits = "".join(ch for ch in dev.id if ch.isdigit())
    return ({"ap": 0, "cpe": 1}.get(dev.role, 2), int(digits) if digits else 0, dev.id)


class Registry:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.lab = Lab()
        self.env: dict[str, str] = {}
        self._adb: dict[str, AdbDevice] = {}
        self._bb_status: dict[str, Status] = {}   # backbone id -> probe result
        self._learned: dict[str, dict] = {}       # node id -> facts learned by plugins (macs, ...)
        self._nodes: list[NodeView] = []
        self._lock = threading.RLock()

    # --- inputs -------------------------------------------------------------

    def set_lab(self, lab: Lab) -> None:
        with self._lock:
            self.lab = lab
            self.env = cfg.load_env()
            self._bb_status = {}
        self.bus.publish(LabLoaded(lab))
        self._rebuild()

    def set_adb_devices(self, devices: list[AdbDevice]) -> None:
        with self._lock:
            new = {d.serial.lower(): d for d in devices}
            if {k: (v.state, v.info) for k, v in new.items()} == \
               {k: (v.state, v.info) for k, v in self._adb.items()}:
                return
            self._adb = new
        self._rebuild()

    def set_backbone_status(self, bb_id: str, status: Status) -> None:
        with self._lock:
            if self._bb_status.get(bb_id) == status:
                return
            self._bb_status[bb_id] = status
        self._rebuild()

    def learn(self, node_id: str, **facts) -> None:
        """Plugins record facts about a node (e.g. mac='00:03:7f:..')."""
        with self._lock:
            self._learned.setdefault(node_id, {}).update(facts)

    def learned(self, node_id: str) -> dict:
        with self._lock:
            return dict(self._learned.get(node_id, {}))

    # --- outputs ------------------------------------------------------------

    def nodes(self) -> list[NodeView]:
        with self._lock:
            return list(self._nodes)

    def get(self, node_id: str) -> NodeView | None:
        with self._lock:
            return next((n for n in self._nodes if n.id == node_id), None)

    def serial_of(self, node: NodeView) -> str:
        if isinstance(node.config, DeviceConfig):
            return cfg.resolved_serial(node.config, self.env)
        return node.serial

    def device_by_mac(self, mac: str) -> NodeView | None:
        mac = mac.lower()
        with self._lock:
            for n in self._nodes:
                if not isinstance(n.config, DeviceConfig):
                    continue
                macs = [str(m).lower() for m in n.config.extra.get("macs", [])]
                macs += [str(m).lower() for m in self._learned.get(n.id, {}).get("macs", [])]
                if mac in macs:
                    return n
        return None

    # --- build --------------------------------------------------------------

    def _rebuild(self) -> None:
        with self._lock:
            old = {n.id: n for n in self._nodes}
            lab, env, now = self.lab, self.env, datetime.now()
            nodes: list[NodeView] = []
            used_serials: set[str] = set()
            placed_bbs: set[str] = set()

            def state_for(node_id: str, status: Status, info: dict | None = None, error=None) -> NodeState:
                prev = old.get(node_id)
                since = prev.state.since if prev and prev.state.status == status else now
                return NodeState(status, since, info or {}, error)

            def add_backbone(bb: BackboneConfig) -> None:
                if bb.id in placed_bbs:
                    return
                placed_bbs.add(bb.id)
                if bb.connect.type == "none":
                    status = Status.UNCONFIGURED
                elif bb.connect.type == "local":
                    status = Status.CONNECTED
                else:
                    status = self._bb_status.get(bb.id, Status.UNKNOWN)
                linked = [d.id for d in lab.devices.values() if d.backbone and d.backbone.backbone_id == bb.id]
                nodes.append(NodeView(bb.id, "backbone", bb.name, state_for(bb.id, status), bb, linked))

            for dev in sorted(lab.devices.values(), key=_sort_key):
                serial = cfg.resolved_serial(dev, env)
                adb = self._adb.get(serial.lower()) if serial else None
                if serial:
                    used_serials.add(serial.lower())
                if not serial:
                    st = state_for(dev.id, Status.UNCONFIGURED, error="no serial (env variable empty?)")
                elif adb is None:
                    st = state_for(dev.id, Status.DISCONNECTED)
                else:
                    st = state_for(dev.id, Status.from_adb(adb.state), adb.info)
                st.info["serial"] = serial
                linked = [dev.backbone.backbone_id] if dev.backbone else []
                nodes.append(NodeView(dev.id, "device", dev.name, st, dev, linked))
                if dev.backbone and dev.backbone.backbone_id in lab.backbones:
                    add_backbone(lab.backbones[dev.backbone.backbone_id])

            for bb in lab.backbones.values():
                add_backbone(bb)

            for serial, adb in sorted(self._adb.items()):
                if serial not in used_serials:
                    serial = adb.serial
                    nid = f"serial:{serial}"
                    nodes.append(NodeView(nid, "unassigned", serial,
                                          state_for(nid, Status.from_adb(adb.state), adb.info)))
            self._nodes = nodes

        changes = [(n, old[n.id].state.status) for n in nodes
                   if n.id in old and old[n.id].state.status != n.state.status]
        for n, was in changes:
            self.bus.publish(NodeStatusChanged(n.id, n.name, was.value, n.state.status.value))
        self.bus.publish(NodesChanged(nodes))
