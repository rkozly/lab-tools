"""
Associate (Traffic tab): bring the AP and the CPEs up one after the other.

  AP     Short scan, then QCMAP bridge (the AP quick buttons of the Console),
         then wait until its MLDs (mld0, mld1) are UP
  CPEs   one at a time, in order: Short scan on the CPE's channel, QCMAP bridge,
         then wait until it has heard 10 beacons (counted from its scan) -
         only then the next CPE starts

Before the CPEs (Associate all, or one CPE's own button): is the AP beaconing
(its phy0 / phy1 beacon counters rising)? Yes: straight to the CPEs. No: the AP
steps first. "Make AP transmit" runs only the AP steps, as often as wanted.
Scan short / long: hw_ver_override 2 / 1 (the Short / Long scan buttons).

Every step goes through the Console shell of that device (the same buttons, so
edits to their text apply) and has a timeout. A failed AP step stops the run;
a failed CPE is marked and the next CPE starts.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QObject, QTimer

from ..core.associate import AP_SCAN, BEACONS_NEEDED, CHANNELS, LONG_CHANNELS, cpe_scan_key

QUICK_TIMEOUT_S = {"check": 90, "qcmap": 300}
MLD_TIMEOUT_S = 180
AP_CHECK_S = 10           # deciding whether the AP is beaconing (two counter readings)
BEACON_TIMEOUT_S = 180


class Associator(QObject):
    def __init__(self, win) -> None:
        super().__init__(win)
        self.w = win
        self.steps: list[dict] = []
        self.i = -1
        self.active = False
        self.baseline: dict[str, int | None] = {}   # cpe id -> beacon counter when its scan was sent
        self._deadline = 0.0
        self._poll = QTimer(self, interval=1000, timeout=self._tick)

    # --- control -------------------------------------------------------------------

    def _ap_steps(self, ap) -> list[dict]:
        word = "Long" if self.scan == "long" else "Short"
        return [
            {"kind": "quick", "node": ap, "key": AP_SCAN[self.scan], "label": f"{word} scan", "cpe": None},
            {"kind": "quick", "node": ap, "key": "ap.qcmap", "label": "QCMAP bridge", "cpe": None},
            {"kind": "mlds", "node": ap, "label": "MLDs up", "cpe": None, "timeout": MLD_TIMEOUT_S},
        ]

    def start(self, ap, cpes: list[tuple], ap_mode: str = "auto", scan: str = "short") -> None:
        """ap: NodeView; cpes: [(NodeView, channel: a CHANNELS key, 'A' / 'B' / 'C' / '26000_airtel'...)] in the order to run.
        ap_mode "auto": the AP steps only if it is not beaconing; "always": the AP steps first.
        scan "short" / "long"."""
        self.scan = scan
        word = "Long" if scan == "long" else "Short"
        if ap_mode == "always":
            self.steps = self._ap_steps(ap)
        else:
            self.steps = [{"kind": "apcheck", "node": ap, "label": "AP beaconing?", "cpe": None}]
        for node, ch in cpes:
            self.steps += [
                {"kind": "quick", "node": node, "key": cpe_scan_key(ch, scan), "label": f"{word} scan {ch}",
                 "cpe": node.id},
                {"kind": "quick", "node": node, "key": "cpe.qcmap", "label": "QCMAP bridge", "cpe": node.id},
                {"kind": "beacons", "node": node, "label": f"{BEACONS_NEEDED} beacons", "cpe": node.id},
            ]
        self.baseline.clear()
        self.failed: list[str] = []
        self.i = -1
        self.active = True
        self.w.traffic.set_assoc_running(True)
        for node, _ch in cpes:
            self.w.traffic.set_assoc_state(node.id, "waiting for its turn", "info")
        self.w.traffic.set_assoc_state("ap", "starting…", "info")
        first = f"{ap.name} ({word.lower()} scan)" if ap_mode == "always" else f"{ap.name} if not beaconing"
        self.w.traffic.log("info", f"{'Make AP transmit' if not cpes else 'Associate'}: {first}"
                           + (", then " + " → ".join(f"{n.name} ({c})" for n, c in cpes) if cpes else ""))
        self._poll.start()
        self._next()

    def stop(self, why: str = "stopped") -> None:
        if not self.active:
            return
        step = self._step()
        if step:
            self.w.traffic.set_assoc_state(step["cpe"] or "ap", why, "warn")
        self._finish(None, f"Associate {why}")

    # --- steps -----------------------------------------------------------------------

    def _step(self) -> dict | None:
        return self.steps[self.i] if self.active and 0 <= self.i < len(self.steps) else None

    def _next(self) -> None:
        self.i += 1
        step = self._step()
        if step is None:
            if self.failed:
                return self._finish(False, f"Associate: done – failed: {', '.join(self.failed)}")
            if not any(st["cpe"] for st in self.steps):
                return self._finish(True, "Make AP transmit: done – the AP is up")
            return self._finish(True, "Associate: done – all CPEs heard the AP")
        who = step["cpe"] or "ap"
        node = step["node"]
        self.w.traffic.set_assoc_state(who, f"{step['label']}…", "info")
        if step["kind"] == "quick":
            if step["cpe"] and step["key"] in list(CHANNELS.values()) + list(LONG_CHANNELS.values()):
                # beacons are counted from here: remember the counter now (or at its first reading)
                self.baseline[step["cpe"]] = self.w.beacon_count(step["cpe"])
            err = self.w.assoc_quick(node, step["key"])
            if err:
                return self._fail(err)
            kind = "qcmap" if step["key"].endswith("qcmap") else "check"
            self._deadline = time.monotonic() + QUICK_TIMEOUT_S[kind]
            self.w.traffic.log("info", f"{node.name}: {step['label']}")
        elif step["kind"] == "apcheck":
            self._deadline = time.monotonic() + AP_CHECK_S
            self.w.traffic.log("info", f"{node.name}: beaconing? (its beacon counters)")
        elif step["kind"] == "mlds":
            self._deadline = time.monotonic() + step.get("timeout", MLD_TIMEOUT_S)
            self.w.traffic.log("info", f"{node.name}: waiting for {self._mld_names()} to be UP…")
        else:
            self._deadline = time.monotonic() + BEACON_TIMEOUT_S
            self.w.traffic.log("info", f"{node.name}: waiting for {BEACONS_NEEDED} beacons…")
        self._tick()

    def _tick(self) -> None:
        step = self._step()
        if step is None:
            return
        if step["kind"] == "apcheck":
            beaconing = self.w.ap_beaconing()
            if beaconing is None and time.monotonic() <= self._deadline:
                self.w.poll_ap_beacons()           # two readings tell whether the counters rise
                return
            node = step["node"]
            if beaconing:
                self.w.traffic.log("ok", f"{node.name}: beaconing – straight to the CPEs")
                self.w.traffic.set_assoc_state("ap", "✔ beaconing", "ok")
            else:
                self.w.traffic.log("info", f"{node.name}: not beaconing – bringing it up first")
                self.steps[self.i + 1:self.i + 1] = self._ap_steps(node)
            return self._next()
        if time.monotonic() > self._deadline:
            return self._fail(f"{step['label']}: no result after {self._timeout(step)} s (timeout)")
        if step["kind"] == "mlds":
            self.w.ctx.devreader.check_mlds(step["node"].id, self.w.ctx.registry.serial_of(step["node"]))
        elif step["kind"] == "beacons":
            self.w.ctx.devreader.read_beacons(step["node"].id, self.w.ctx.registry.serial_of(step["node"]))

    @staticmethod
    def _timeout(step: dict) -> int:
        if step["kind"] == "quick":
            return QUICK_TIMEOUT_S["qcmap" if step["key"].endswith("qcmap") else "check"]
        return step.get("timeout", MLD_TIMEOUT_S) if step["kind"] == "mlds" else BEACON_TIMEOUT_S

    @staticmethod
    def _mld_names() -> str:
        from ..core.associate import AP_MLDS
        return " / ".join(AP_MLDS)

    # --- results (from the main window's event handlers) ------------------------------

    def on_quick(self, node_id: str, key: str, ok: bool, detail: str) -> None:
        step = self._step()
        if not step or step["kind"] != "quick" or step["node"].id != node_id or step["key"] != key:
            return
        if ok:
            self.w.traffic.log("ok", f"{step['node'].name}: {step['label']} – {detail[:120]}")
            self._next()
        else:
            self._fail(f"{step['label']} failed: {detail[:200]}")

    def on_mlds(self, node_id: str, up: bool, detail: str) -> None:
        step = self._step()
        if not step or step["kind"] != "mlds" or step["node"].id != node_id:
            return
        self.w.traffic.set_assoc_state("ap", f"waiting for MLDs: {detail}", "info")
        if up:
            self.w.traffic.log("ok", f"{step['node'].name}: {detail}")
            self.w.traffic.set_assoc_state("ap", f"✔ up ({detail})", "ok")
            self._next()

    def on_beacons(self, node_id: str, value: int | None) -> None:
        if value is None or not self.active:
            return
        if node_id in self.baseline and self.baseline[node_id] is None:
            self.baseline[node_id] = value      # first reading after its scan was sent
        step = self._step()
        if not step or step["kind"] != "beacons" or step["node"].id != node_id:
            return
        base = self.baseline.get(node_id) or 0
        if value < base:                        # counter was reset (e.g. by the QCMAP bridge)
            base = self.baseline[node_id] = 0
        heard = value - base
        self.w.traffic.set_assoc_state(node_id, f"heard {heard} / {BEACONS_NEEDED} beacons", "info")
        if heard >= BEACONS_NEEDED:
            self.w.traffic.log("ok", f"{step['node'].name}: heard {heard} beacons (counter {value})")
            self.w.traffic.set_assoc_state(node_id, f"✔ heard {heard} beacons", "ok")
            self._next()

    # --- end ---------------------------------------------------------------------------

    def _fail(self, why: str) -> None:
        step = self._step()
        if step is None:
            return
        name = step["node"].name
        if step["cpe"] is None:                 # AP step: nothing can follow
            self.w.traffic.set_assoc_state("ap", f"✖ {why}", "fail")
            return self._finish(False, f"Associate stopped – {name}: {why}")
        cpe = step["cpe"]
        self.failed.append(name)
        self.w.traffic.set_assoc_state(cpe, f"✖ {why}", "fail")
        self.w.traffic.log("fail", f"{name}: {why} – next CPE")
        while self._step() and self._step()["cpe"] == cpe:   # skip the rest of this CPE
            self.i += 1
        self.i -= 1
        self._next()

    on_done = None            # called once with ok (True / False / None) when the run ends

    def _finish(self, ok: bool | None, text: str) -> None:
        self.active = False
        self._poll.stop()
        self.w.traffic.set_assoc_running(False)
        self.w.traffic.log({True: "ok", False: "fail", None: "warn"}[ok], text)
        self.w.statusBar().showMessage(text, 12000)
        cb, self.on_done = self.on_done, None
        if cb:
            cb(ok)
