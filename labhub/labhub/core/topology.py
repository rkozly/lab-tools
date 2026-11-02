"""
Topology: the network interfaces of every device and backbone, and how they
hang together. Read from /sys/class/net (who is whose father) and
`ifconfig -a` (the config shown when an interface is clicked).

Father of an interface:
  master   bridge / bond it is enslaved to   (br-lan -> eth0, ath0)
  lower    device a VLAN / macvlan sits on   (eth0   -> eth0.960)
  parent   radio of a QCA vap                (wifi0  -> ath0)
The first that exists is the father in the drawing; the others are listed.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field

from ..transports import base as tr
from .bbnet import run
from .events import Event, EventBus

SCRIPT = (
    "for d in /sys/class/net/*; do n=${d##*/}; m=; [ -e $d/master ] && m=$(readlink $d/master); "
    "l=; for x in $d/lower_*; do [ -e \"$x\" ] && l=\"$l ${x##*/lower_}\"; done; "
    "p=; [ -r $d/parent ] && p=$(cat $d/parent 2>/dev/null); "
    "k=; [ -d $d/bridge ] && k=bridge; [ -d $d/bonding ] && k=bond; "
    "{ [ -d $d/wireless ] || [ -d $d/phy80211 ]; } && k=${k:-wireless}; "
    "[ -r $d/type ] && [ \"$(cat $d/type)\" = 772 ] && k=loopback; "
    "o=$(cat $d/operstate 2>/dev/null); echo \"IF|$n|${m##*/}|$l|$p|$k|$o\"; done; "
    "echo ===IFCONFIG===; ifconfig -a 2>/dev/null || ip addr"
)
_HEAD = re.compile(r"^(?:\d+:\s+)?([^\s:@]+)")
_INET = re.compile(r"inet (?:addr:)?(\d+\.\d+\.\d+\.\d+)")
_INET6 = re.compile(r"inet6 (?:addr:\s*)?([0-9a-fA-F:]+(?:/\d+)?)(?:.*?(?:Scope:|scope )(\w+))?")


@dataclass
class Iface:
    name: str
    master: str = ""
    lower: list[str] = field(default_factory=list)
    parent: str = ""
    kind: str = ""               # bridge / bond / wireless / loopback / vlan / "" (ethernet, ...)
    state: str = ""              # operstate: up / down / unknown
    config: str = ""             # its ifconfig block
    ipv4: list[str] = field(default_factory=list)
    ipv6: list[str] = field(default_factory=list)   # global only

    @property
    def father(self) -> str:
        return self.master or (self.lower[0] if self.lower else "") or self.parent

    def relations(self) -> list[str]:
        out = []
        if self.master:
            out.append(f"in {self.master}")
        out += [f"on {l}" for l in self.lower]
        if self.parent and self.parent != self.father:
            out.append(f"radio {self.parent}")
        return out


def _blocks(text: str) -> dict[str, str]:
    """ifconfig -a (or ip addr) -> {name: its lines}. A block starts at an unindented line."""
    out: dict[str, list[str]] = {}
    cur = None
    for line in text.splitlines():
        if line and not line[0].isspace():
            m = _HEAD.match(line)
            cur = m.group(1) if m else None
            if cur:
                out.setdefault(cur, []).append(line)
        elif cur and line.strip():
            out[cur].append(line)
    return {k: "\n".join(v) for k, v in out.items()}


def parse(out: str) -> list[Iface]:
    sys_part, _, cfg_part = out.partition("===IFCONFIG===")
    blocks = _blocks(cfg_part)
    found: dict[str, Iface] = {}
    for line in sys_part.splitlines():
        if not line.startswith("IF|"):
            continue
        f = (line.split("|") + [""] * 8)[1:8]
        f = [x.strip() for x in f]
        name, master, lower, parent, kind, state = f[0], f[1], f[2].split(), f[3], f[4], f[5]
        if not name or name == "*":
            continue
        if not kind and lower:
            kind = "vlan" if "." in name else "virtual"
        found[name] = Iface(name, master, lower, parent, kind, state)
    for name, block in blocks.items():     # no sysfs (old busybox): ifconfig only
        found.setdefault(name, Iface(name))
    for name, ia in found.items():
        ia.config = blocks.get(name, "")
        ia.ipv4 = _INET.findall(ia.config)
        ia.ipv6 = [a for a, scope in _INET6.findall(ia.config)
                   if scope.lower() not in ("link", "host") and not a.lower().startswith(("fe80", "::1"))]
        if ia.father and ia.father not in found:
            ia.master = ia.master if ia.master in found else ""
            ia.lower = [l for l in ia.lower if l in found]
            ia.parent = ia.parent if ia.parent in found else ""
    return sorted(found.values(), key=lambda i: i.name)


# --- service ------------------------------------------------------------------

@dataclass
class TopologyResult(Event):
    node_id: str
    ifaces: list[Iface]
    error: str = ""


@dataclass
class IfaceDetail(Event):
    node_id: str
    name: str
    text: str


class TopologyService:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._busy: set[str] = set()

    def collect(self, node_id: str, spec: tr.ConnectSpec, serial: str = "") -> None:
        if node_id in self._busy:
            return
        self._busy.add(node_id)
        threading.Thread(target=self._collect, args=(node_id, spec, serial), daemon=True).start()

    def _collect(self, node_id: str, spec: tr.ConnectSpec, serial: str) -> None:
        try:
            if serial:
                tr.ensure_root(serial)
            code, out = run(spec, SCRIPT, timeout=40)
            ifaces = parse(out)
            err = "" if ifaces else (out.strip().splitlines()[-1] if out.strip() else f"exit {code}")
            self.bus.publish(TopologyResult(node_id, ifaces, err))
        finally:
            self._busy.discard(node_id)

    def detail(self, node_id: str, spec: tr.ConnectSpec, name: str) -> None:
        def work():
            if not re.fullmatch(r"[\w.@:-]+", name):
                return
            _, out = run(spec, f"ifconfig {name} 2>&1; echo; ip -d addr show dev {name} 2>&1")
            self.bus.publish(IfaceDetail(node_id, name, out))
        threading.Thread(target=work, daemon=True).start()
