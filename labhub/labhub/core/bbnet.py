"""
Backbone networking helpers: IPv6 addresses of the BB interfaces and the
routing table. Commands run over a second connection (exec), so they work
while the BB's Console shell is busy (e.g. dhcpd -d in the foreground).
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import threading
from dataclasses import dataclass, field

from ..transports import base as tr
from .events import Event, EventBus

ADDR_CMD = "ip -6 -o addr show 2>&1; echo ===LINKS===; ip -o link show 2>&1"
_ADDR = re.compile(r"^\d+:\s+([^@\s]+)\S*\s+inet6\s+(\S+)\s+scope\s+(\S+)")
_LINK = re.compile(r"^\d+:\s+([^:@\s]+)")
_ROUTE_TYPES = ("unicast", "local", "broadcast", "multicast", "throw", "unreachable", "prohibit", "blackhole", "nat")
_DEST = re.compile(r"^(default|[0-9a-fA-F:.]+(/\d{1,3})?)$")
_IFACE = re.compile(r"^[\w.@:-]+$")
_GW = re.compile(r"^[0-9a-fA-F:.]+$")


def run(spec: tr.ConnectSpec, command: str, timeout: float = 30) -> tuple[int, str]:
    """Run one command on the node; (exit code, output). Blocking."""
    try:
        res = subprocess.run(tr.exec_argv(spec, command), capture_output=True, text=True, timeout=timeout,
                             stdin=subprocess.DEVNULL, creationflags=tr.CREATE_NO_WINDOW,
                             cwd=os.path.expanduser("~"), env=tr.child_env(), encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f} s"
    except (OSError, tr.TransportError) as e:
        return 255, str(e)
    return res.returncode, (res.stdout + res.stderr).strip()


# --- IPv6 addresses ----------------------------------------------------------

@dataclass
class IfaceAddr:
    name: str                    # as on the BB (case may differ from Mappings)
    present: bool
    global_addrs: list[str] = field(default_factory=list)
    link_local: list[str] = field(default_factory=list)


def parse_addrs(out: str) -> tuple[dict[str, IfaceAddr], list[str]]:
    """Output of ADDR_CMD -> ({lower-case name: IfaceAddr}, all interface names)."""
    addrs_part, _, links_part = out.partition("===LINKS===")
    links = [m.group(1) for m in map(_LINK.match, links_part.splitlines()) if m]
    found = {n.lower(): IfaceAddr(n, True) for n in links}
    for line in addrs_part.splitlines():
        m = _ADDR.match(line.strip())
        if not m:
            continue
        name, addr, scope = m.groups()
        ia = found.setdefault(name.lower(), IfaceAddr(name, True))
        (ia.link_local if scope == "link" else ia.global_addrs).append(addr)
    return found, links


_INET6 = re.compile(r"inet6\s+(?:addr:\s*)?([0-9a-fA-F:]+)(?:/\d+)?")


def ifconfig_cmd(iface: str) -> str:
    return f"ifconfig {shlex.quote(iface)} 2>&1"


def parse_inet6(out: str) -> tuple[list[str], list[str]]:
    """`ifconfig <iface>` (old 'inet6 addr: x/64' or new 'inet6 x prefixlen 64') -> (global, link-local)."""
    glob, link = [], []
    for addr in _INET6.findall(out):
        (link if addr.lower().startswith("fe80") else glob).append(addr)
    return glob, link


_IFC_HEAD = re.compile(r"^([^\s:]+(?::\d+)?):?\s+(?:flags=|Link encap)")
_GLOBAL_NEW = re.compile(r"^\s*inet6\s+([0-9a-fA-F:]+)\s+prefixlen\s+(\d+).*<global>\s*$")
_GLOBAL_OLD = re.compile(r"^\s*inet6 addr:\s*([0-9a-fA-F:]+)/(\d+)\s+Scope:Global", re.I)


def parse_globals(out: str) -> list[tuple[str, str]]:
    """`ifconfig` -> [(interface, "addr/prefixlen")] of the global IPv6 addresses
    ('inet6 2008:db9::20  prefixlen 64  scopeid 0x0<global>'; old style 'Scope:Global' too)."""
    found, iface = [], ""
    for line in out.splitlines():
        h = _IFC_HEAD.match(line)
        if h:
            iface = h.group(1)
            continue
        m = _GLOBAL_NEW.match(line) or _GLOBAL_OLD.match(line)
        if m:
            found.append((iface, f"{m.group(1)}/{m.group(2)}"))
    return found


def lookup(found: dict[str, IfaceAddr], name: str) -> IfaceAddr:
    """Interface by name, case-insensitive (CPE1.960 = cpe1.960)."""
    return found.get(name.lower()) or IfaceAddr(name, False)


# --- routes ------------------------------------------------------------------

@dataclass
class Route:
    line: str
    dest: str
    via: str = ""
    dev: str = ""
    metric: str = ""
    proto: str = ""
    table: str = ""
    kind: str = ""               # unreachable / local / ... (empty = unicast)

    def del_args(self) -> str:
        """Selectors for `ip route del` that match exactly this route."""
        parts = ([self.kind] if self.kind else []) + [self.dest]
        for key in ("via", "dev", "metric", "table", "proto"):
            val = getattr(self, key)
            if val:
                parts += [key, val]
        return " ".join(shlex.quote(p) for p in parts)


def parse_routes(out: str) -> list[Route]:
    routes = []
    for line in out.splitlines():
        tok = line.split()
        if not tok or tok[0].startswith(("RTNETLINK", "Error", "[LabHub]")):
            continue
        kind = tok.pop(0) if tok[0] in _ROUTE_TYPES and len(tok) > 1 else ""
        r = Route(line.strip(), tok[0], kind=kind)
        for i, t in enumerate(tok[1:-1], 1):
            if t in ("via", "dev", "metric", "proto", "table"):
                setattr(r, t, tok[i + 1])
        routes.append(r)
    return routes


def route_cmd(family: str, action: str, args: str = "") -> str:
    """family '6' / '4'; action 'show' / 'add' / 'del'."""
    return f"ip -{family} route {action} {args}".strip()


def add_args(dest: str, iface: str, via: str = "") -> str:
    """`<dest> dev <iface> [via <gw>]`; raises ValueError on a bad value."""
    dest, iface, via = dest.strip(), iface.strip(), via.strip()
    if not _DEST.match(dest):
        raise ValueError(f"destination '{dest}' is not like 2001:db8::/64, 10.0.0.0/24 or default")
    if not _IFACE.match(iface):
        raise ValueError(f"interface '{iface}' is not a valid name")
    if via and not _GW.match(via):
        raise ValueError(f"gateway '{via}' is not an address")
    return f"{dest} dev {iface}" + (f" via {via}" if via else "")


# --- service: runs the above in worker threads, answers with events ------------

@dataclass
class BbAddrs(Event):
    node_id: str
    ifaces: list[str]
    found: dict                  # lower-case name -> IfaceAddr
    error: str = ""


@dataclass
class BbRoutes(Event):
    node_id: str
    family: str
    routes: list[Route]
    error: str = ""
    message: str = ""            # result of an add / del, if one ran first


@dataclass
class BbExecDone(Event):
    node_id: str
    key: str                     # quick command key
    code: int
    output: str


@dataclass
class BbGlobals(Event):
    node_id: str                 # backbone
    addrs: list                  # [(interface, "addr/prefixlen")]
    error: str = ""


@dataclass
class BbPeerAddr(Event):
    node_id: str                 # BB whose shell gets the command
    key: str                     # quick command key
    peer: str                    # device name of the other side
    iface: str                   # interface on the peer's BB
    addr: str = ""               # chosen address (global first, else link-local)
    link_local: bool = False
    error: str = ""


class BbNetService:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._busy: set[str] = set()   # node ids with an address check in flight

    @staticmethod
    def _spec(node) -> tr.ConnectSpec:
        return tr.connect_spec(node.config)

    def _bg(self, fn, *args) -> None:
        threading.Thread(target=fn, args=args, daemon=True).start()

    def check_addrs(self, node, ifaces: list[str]) -> None:
        if node.id in self._busy:
            return
        self._busy.add(node.id)
        self._bg(self._addrs, node, list(ifaces))

    def _addrs(self, node, ifaces: list[str]) -> None:
        try:
            code, out = run(self._spec(node), ADDR_CMD)
            found, links = parse_addrs(out)
            err = "" if links else (out.splitlines()[-1] if out else f"exit {code}")
            self.bus.publish(BbAddrs(node.id, ifaces, found, err))
        except tr.TransportError as e:
            self.bus.publish(BbAddrs(node.id, ifaces, {}, str(e)))
        finally:
            self._busy.discard(node.id)

    def routes(self, node, family: str, action: str = "", args: list[str] | None = None) -> None:
        """Show the table; with action 'add' / 'del' first run it once per entry of args."""
        self._bg(self._routes, node, family, action, list(args or []))

    def _routes(self, node, family: str, action: str, args: list[str]) -> None:
        try:
            spec = self._spec(node)
        except tr.TransportError as e:
            self.bus.publish(BbRoutes(node.id, family, [], str(e)))
            return
        lines = []
        for a in args if action else []:
            cmd = route_cmd(family, action, a)
            code, out = run(spec, cmd)
            lines.append(f"{'✔' if code == 0 else '✖'} {cmd}" + (f"\n   {out}" if out else ""))
        code, out = run(spec, route_cmd(family, "show"))
        routes = parse_routes(out)
        err = out if code != 0 and not routes else ""
        self.bus.publish(BbRoutes(node.id, family, routes, err, "\n".join(lines)))

    def exec_quick(self, node, key: str, command: str) -> None:
        def work():
            try:
                code, out = run(self._spec(node), command)
            except tr.TransportError as e:
                code, out = 255, str(e)
            self.bus.publish(BbExecDone(node.id, key, code, out))
        self._bg(work)

    def read_globals(self, node) -> None:
        """Global IPv6 addresses of a BB (`ifconfig`, inside its namespace if it has one)."""
        def work():
            try:
                code, out = run(self._spec(node), "ifconfig 2>&1")
            except tr.TransportError as e:
                self.bus.publish(BbGlobals(node.id, [], str(e)))
                return
            addrs = parse_globals(out)
            err = "" if addrs or code == 0 else (out.splitlines()[-1] if out else f"exit {code}")
            self.bus.publish(BbGlobals(node.id, addrs, err))
        self._bg(work)

    def peer_addr(self, node, key: str, peer_bb, peer: str, iface: str) -> None:
        """IPv6 address of `iface` on the peer's BB (ifconfig), for a ping / iperf typed on `node`."""
        def work():
            try:
                code, out = run(self._spec(peer_bb), ifconfig_cmd(iface))
            except tr.TransportError as e:
                self.bus.publish(BbPeerAddr(node.id, key, peer, iface, error=str(e)))
                return
            glob, link = parse_inet6(out)
            if glob or link:
                self.bus.publish(BbPeerAddr(node.id, key, peer, iface, (glob or link)[0], not glob))
            else:
                why = "no IPv6 address" if code == 0 else (out.splitlines()[-1] if out else f"exit {code}")
                self.bus.publish(BbPeerAddr(node.id, key, peer, iface, error=why))
        self._bg(work)
