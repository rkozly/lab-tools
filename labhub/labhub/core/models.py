"""
Data model.

Two layers:
- configuration: what the lab *should* look like (loaded from the lab file)
- runtime:       what is connected *right now* (from discovery)
The registry joins them into NodeView objects, which is what the GUI shows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

ROLES = ("ap", "cpe", "other")
CONNECT_TYPES = ("none", "adb", "ssh", "local")  # none = not configured yet


# --- configuration ----------------------------------------------------------

@dataclass
class ConnectSpec:
    """How a node is reached, e.g. {type: ssh, host: 10.0.0.20, user: root}."""
    type: str = "none"
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"type": self.type, **self.params}


@dataclass
class BackboneLink:
    """A device's link to a backbone host, through the given interfaces."""
    backbone_id: str
    interfaces: list[str] = field(default_factory=list)


@dataclass
class DeviceConfig:
    id: str
    name: str
    role: str = "cpe"
    serial: str = ""
    backbone: BackboneLink | None = None
    tags: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)  # free space for features


@dataclass
class BackboneConfig:
    id: str
    name: str
    connect: ConnectSpec = field(default_factory=ConnectSpec)
    extra: dict = field(default_factory=dict)


@dataclass
class Lab:
    name: str = "Unnamed lab"
    version: int = 1
    devices: dict[str, DeviceConfig] = field(default_factory=dict)
    backbones: dict[str, BackboneConfig] = field(default_factory=dict)
    groups: dict[str, list[str]] = field(default_factory=dict)  # name -> target selectors
    features: dict = field(default_factory=dict)  # per-feature settings
    presets: list = field(default_factory=list)   # raw preset dicts from the lab file
    path: str | None = None  # file it was loaded from


# --- runtime ----------------------------------------------------------------

class Status(str, Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    UNAUTHORIZED = "unauthorized"
    OFFLINE = "offline"
    UNCONFIGURED = "not configured"
    UNKNOWN = "unknown"

    @classmethod
    def from_adb(cls, state: str) -> "Status":
        return {
            "device": cls.CONNECTED,
            "unauthorized": cls.UNAUTHORIZED,
            "offline": cls.OFFLINE,
        }.get(state, cls.UNKNOWN)


@dataclass
class NodeState:
    status: Status = Status.UNKNOWN
    since: datetime | None = None  # when the status last changed
    info: dict = field(default_factory=dict)  # model, product, usb, ...
    error: str | None = None


@dataclass
class AdbDevice:
    """One line of `adb devices -l`."""
    serial: str
    state: str
    info: dict = field(default_factory=dict)


@dataclass
class NodeView:
    """What the GUI shows for one tile."""
    id: str                      # device id, backbone id, or "serial:<serial>"
    kind: str                    # "device" | "backbone" | "unassigned"
    name: str
    state: NodeState
    config: DeviceConfig | BackboneConfig | None = None
    linked: list[str] = field(default_factory=list)  # device <-> backbone ids

    @property
    def serial(self) -> str:
        if isinstance(self.config, DeviceConfig):
            return self.state.info.get("serial", "")
        if self.kind == "unassigned":
            return self.id.split(":", 1)[1]
        return ""

    @property
    def subtitle(self) -> str:
        """Second line on a tile: serial for devices, host for backbones."""
        if isinstance(self.config, BackboneConfig):
            p = self.config.connect.params
            if self.config.connect.type == "none":
                return "not configured"
            host = p.get("host") or p.get("serial") or self.config.connect.type
            return f"{host} {p['netns']}" if p.get("netns") else host
        return self.serial or "no serial"
