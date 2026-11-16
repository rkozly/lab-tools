"""
Traffic runs (iperf3) between the AP's backbone and the CPEs' backbones.

Before a run (in a worker thread, over exec connections):
  1. every BB:  killall iperf3 (a run before may still hold the ports), then
     check with pidof that it is gone (killall -9 if not). DHCP is left alone:
     the Traffic tab's "Prepare backbones" does the DHCP server / dhclient part.
  2. AP BB:     the data / control interfaces must have their fixed addresses
                (data 2008:db9::20, control 2008:db8::20); a different one -> warning
  3. CPE BBs:   the CPE's global address on its data interface (.1015)
  4. CPE BBs:   routes to the AP side through the CPE's data interface
Then the GUI types the iperf3 commands, one shell per CPE on each side
(so the AP BB has a shell per CPE too). Every CPE has its own port.

  downlink  server on the CPE BB (bound to the CPE's data address), client on the AP BB
  uplink    server on the AP BB (bound to its data address 2008:db9::20), client on the CPE BB
  TCP = the UDP commands without -u
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from . import bbnet
from .events import Event, EventBus

AP_DATA_ADDR = "2008:db9::20"      # AP BB data interface (e.g. v10371015)
AP_CTRL_ADDR = "2008:db8::20"      # AP BB control interface (AP.960)
EXTRA_ROUTE = "2008:db9::1"
BASE_PORT = 5005
TOTAL_MBPS = 400        # -b (per stream, Mbit/s) = TOTAL_MBPS / number of CPEs in the run, unless set by hand
KILL = ("iperf3",)

# "server_on": the side that receives (runs iperf3 -s); the other side runs the client.
COMMANDS = {
    ("dl", "udp"): {
        "server_on": "cpe",
        "server": "iperf3 -s -B {cpe} -p {port} -i 1 | grep SUM",
        "client": "iperf3 -c {cpe} -B {ap} -p {port} -u -b {rate}M -P 10 -t 86400 -i 1 | grep SUM",
    },
    ("dl", "tcp"): {
        "server_on": "cpe",
        "server": "iperf3 -s -B {cpe} -p {port} -i 1 | grep SUM",
        "client": "iperf3 -c {cpe} -B {ap} -p {port} -b {rate}M -P 10 -t 86400 -i 1 | grep SUM",
    },
    ("ul", "udp"): {
        "server_on": "ap",
        "server": "iperf3 -s -B {ap} -p {port} -i 1 | grep SUM",
        "client": "iperf3 -c {ap} -B {cpe} -p {port} -u -b {rate}M -P 10 -t 86400 -i 1 | grep SUM",
    },
    ("ul", "tcp"): {
        "server_on": "ap",
        "server": "iperf3 -s -B {ap} -p {port} -i 1 | grep SUM",
        "client": "iperf3 -c {ap} -B {cpe} -p {port} -b {rate}M -P 10 -t 86400 -i 1 | grep SUM",
    },
}


def label(direction: str, proto: str) -> str:
    return f"{'Uplink' if direction == 'ul' else 'Downlink'} {proto.upper()}"


def supported(direction: str, proto: str) -> bool:
    return (direction, proto) in COMMANDS


def bare(addr: str) -> str:
    """'2008:db9::20/64' -> '2008:db9::20'"""
    return addr.split("/", 1)[0]


@dataclass
class CpePlan:
    id: str
    name: str
    bb: object                     # NodeView of the CPE's backbone
    data: str                      # data interface on that BB (CPE1.1015)
    wanted: str = ""               # address picked in the Traffic tab ("addr/len"), may be empty
    port: int = 0
    rate: int = 0                  # -b per stream (Mbit/s); 0 = TOTAL_MBPS / CPEs in the run (set by prepare)
    addr: str = ""                 # filled in by prepare (bare address)
    manual: bool = False           # wanted was typed in by hand: used as it is


@dataclass
class Plan:
    direction: str
    proto: str
    ap_bb: object                  # NodeView
    ap_ctrl: str                   # AP.960
    ap_data: str                   # v10371015
    cpes: list[CpePlan] = field(default_factory=list)
    ap_wanted: str = ""            # AP address picked in the Traffic tab ("addr/len"), may be empty
    ap_addr: str = ""              # filled in by prepare


@dataclass
class TrafficLog(Event):
    level: str                     # info | ok | warn | fail
    text: str


@dataclass
class TrafficReady(Event):
    plan: Plan


@dataclass
class TrafficFailed(Event):
    text: str


class TrafficService:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.busy = False

    def prepare(self, plan: Plan) -> bool:
        if self.busy:
            return False
        self.busy = True
        threading.Thread(target=self._prepare, args=(plan,), name="traffic-prepare", daemon=True).start()
        return True

    # --- steps ---------------------------------------------------------------------

    def _log(self, level: str, text: str) -> None:
        self.bus.publish(TrafficLog(level, text))

    def _run(self, node, command: str) -> tuple[int, str]:
        return bbnet.run(bbnet.tr.connect_spec(node.config), command)

    def _prepare(self, plan: Plan) -> None:
        try:
            self._steps(plan)
        except _Abort as e:
            self._log("fail", str(e))
            self.bus.publish(TrafficFailed(str(e)))
        except Exception as e:   # never leave the service busy
            self._log("fail", f"unexpected error: {e}")
            self.bus.publish(TrafficFailed(str(e)))
        finally:
            self.busy = False

    def _steps(self, plan: Plan) -> None:
        bbs = {plan.ap_bb.id: plan.ap_bb}
        for c in plan.cpes:
            bbs.setdefault(c.bb.id, c.bb)
        # 1. nothing left running. A CPE BB that fails here only drops its CPEs; the AP BB stops the run.
        self._log("info", f"1/4  killall {' '.join(KILL)} on {len(bbs)} backbone(s)…")
        for bb in bbs.values():
            try:
                self._kill(bb)
            except _Abort as e:
                if bb.id == plan.ap_bb.id:
                    raise
                self._drop(plan, [c for c in plan.cpes if c.bb.id == bb.id], str(e))
        # 2. AP BB addresses
        self._log("info", f"2/4  checking the addresses of {plan.ap_bb.name}…")
        plan.ap_addr = self._check_ap(plan)
        # 3. CPE addresses
        self._log("info", "3/4  reading the CPE addresses…")
        cache: dict[str, list] = {}
        for c in list(plan.cpes):
            if c.bb.id not in cache:
                try:
                    cache[c.bb.id] = self._globals(c.bb)
                except _Abort as e:
                    cache[c.bb.id] = None
                    self._log("fail", str(e))
            if cache[c.bb.id] is None and not (c.manual and c.wanted):
                self._drop(plan, [c], f"{c.bb.name}: addresses could not be read")
                continue
            c.addr = self._cpe_addr(c, cache[c.bb.id] or [])
            if not c.addr:
                plan.cpes.remove(c)
        if not plan.cpes:
            raise _Abort("no CPE has a global IPv6 address on its data interface – nothing to run")
        # 4. routes
        self._log("info", "4/4  routes on the CPE backbones…")
        for c in list(plan.cpes):
            for cmd in (f"ip -6 r a {plan.ap_addr} dev {c.data} proto kernel",
                        f"ip -6 route add {EXTRA_ROUTE} dev {c.data}"):
                code, out = self._run(c.bb, cmd)
                if code == 0 or "file exists" in out.lower():
                    self._log("ok", f"{c.bb.name}: {cmd}" + ("  (already there)" if code else ""))
                else:                          # this CPE only: the others still run
                    self._drop(plan, [c], f"{c.bb.name}: {cmd} failed: {out or f'exit {code}'}")
                    break
        if not plan.cpes:
            raise _Abort("no CPE is left to run (see the red lines above)")
        # -b: TOTAL_MBPS / the CPEs that passed every check
        auto = max(1, round(TOTAL_MBPS / len(plan.cpes)))
        for c in plan.cpes:
            by_hand = bool(c.rate)
            c.rate = c.rate or auto
            self._log("info", f"{c.name}: -b {c.rate}M per stream"
                              + (" (set by hand)" if by_hand else f" ({TOTAL_MBPS}/{len(plan.cpes)} CPEs)"))
        self.bus.publish(TrafficReady(plan))

    def _drop(self, plan: Plan, cpes: list, why: str) -> None:
        """Leave these CPEs out of this run (the others go on)."""
        for c in cpes:
            if c in plan.cpes:
                plan.cpes.remove(c)
                self._log("fail", f"{c.name} left out: {why}")

    def _kill(self, bb) -> None:
        names = " ".join(KILL)
        code, out = self._run(bb, f"killall {names} 2>/dev/null; sleep 1; pidof {names} || echo NONE")
        if "NONE" not in out:      # pidof found some (or the BB was not reached)
            if code == 255:
                raise _Abort(f"{bb.name}: cannot connect: {out}")
            code, out = self._run(bb, f"killall -9 {names} 2>/dev/null; sleep 1; pidof {names} || echo NONE")
            if "NONE" not in out:
                raise _Abort(f"{bb.name}: still running after killall -9 (pids {out.strip()})")
            self._log("warn", f"{bb.name}: needed killall -9")
        self._log("ok", f"{bb.name}: no {', '.join(KILL)} running")

    def _globals(self, bb) -> list[tuple[str, str]]:
        code, out = self._run(bb, "ifconfig 2>&1")
        found = bbnet.parse_globals(out)
        if not found and code != 0:
            raise _Abort(f"{bb.name}: ifconfig failed: {out or f'exit {code}'}")
        return found

    def _check_ap(self, plan: Plan) -> str:
        """The fixed addresses are looked up by address, on whatever interface has them
        (the BB's names, e.g. v10371015, need not match Mappings / the defaults)."""
        found = [(f, bare(a)) for f, a in self._globals(plan.ap_bb)]
        where = {a: f for f, a in found}
        name = plan.ap_bb.name
        if AP_CTRL_ADDR in where:
            self._log("ok", f"{name}: {where[AP_CTRL_ADDR]} = {AP_CTRL_ADDR} (control)")
        else:
            self._log("warn", f"{name}: {AP_CTRL_ADDR} (control) is not on any interface")
        if AP_DATA_ADDR in where:
            self._log("ok", f"{name}: {where[AP_DATA_ADDR]} = {AP_DATA_ADDR} (data)")
            want = bare(plan.ap_wanted)
            if want and want != AP_DATA_ADDR:
                self._log("warn", f"{name}: picked {want} in the table – using the fixed data address {AP_DATA_ADDR}")
            return AP_DATA_ADDR
        # not the fixed address: the pick in the table, else the data interface from Mappings
        want = bare(plan.ap_wanted)
        if want and want in where and want != AP_CTRL_ADDR:
            self._log("warn", f"{name}: {AP_DATA_ADDR} (data) not found – using the picked {want} ({where[want]})")
            return want
        data = [a for f, a in found if f.lower() == plan.ap_data.lower()]
        if data:
            self._log("warn", f"{name}: {AP_DATA_ADDR} (data) not found – using {data[0]} ({plan.ap_data})")
            return data[0]
        have = ", ".join(f"{a} ({f})" for f, a in found) or "none"
        raise _Abort(f"{name}: {AP_DATA_ADDR} (data) is not on any interface – global addresses: {have}")

    def _cpe_addr(self, c: CpePlan, found: list) -> str:
        on_data = [bare(a) for f, a in found if f.lower() == c.data.lower()]
        everywhere = [bare(a) for _f, a in found]
        want = bare(c.wanted) if c.wanted else ""
        if want and c.manual:
            self._log("ok", f"{c.name}: {want} (typed by hand)  · port {c.port}")
            return want
        if want and want in on_data:
            addr = want
        elif want and want in everywhere:
            addr = want
            self._log("warn", f"{c.name}: {want} (picked) is not on {c.data}")
        elif on_data:
            addr = on_data[0]
            if want:
                self._log("warn", f"{c.name}: picked {want} is not on {c.bb.name} any more – using {addr}")
        else:
            self._log("fail", f"{c.name}: no global IPv6 address on {c.data} ({c.bb.name}) – skipped"
                              " (run dhclient on its BB)")
            return ""
        self._log("ok", f"{c.name}: {c.data} = {addr}  · port {c.port}")
        return addr


class _Abort(Exception):
    pass
