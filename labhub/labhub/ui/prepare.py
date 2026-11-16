"""
Prepare backbones (Traffic tab), with the backbone buttons of the Console, in order:

  1. AP BB     Kill DHCP server
  2. CPE BBs   Kill dhclient (every one)
  3. AP BB     Run DHCP server, then wait in its shell until dhcpd serves
               ("Server starting service"; "No subnet6 declaration for X" is reported)
  4. CPE BBs   one at a time: dhclient, then wait until it is bound and the
               Solicits stop ("Bound to lease" and no new Solicit for a few seconds)
  5. verify    the global IPv6 addresses: the AP BB's data address, each CPE's data interface

The traffic buttons do not touch DHCP; they only check the addresses again.
"""

from __future__ import annotations

import re
import time

from PySide6.QtCore import QObject, QTimer

from ..core.traffic import AP_DATA_ADDR, bare

EXEC_TIMEOUT_S = 30
DHCPD_TIMEOUT_S = 40
DHCLIENT_TIMEOUT_S = 120
QUIET_S = 8                          # no new Solicit for this long = dhclient is done
VERIFY_TIMEOUT_S = 40

_DHCPD_READY = re.compile(r"Server starting service", re.I)
_DHCPD_FAIL = re.compile(r"Not configured to listen on any interfaces|exiting\.?\s*$|Can't open lease database", re.I | re.M)
_NO_SUBNET = re.compile(r"No subnet6? declaration for (\S+)", re.I)
_SOLICIT = re.compile(r"\bSolicit\b")
_BOUND = re.compile(r"Bound to lease", re.I)


class BackbonePrep(QObject):
    def __init__(self, win) -> None:
        super().__init__(win)
        self.w = win
        self.steps: list[dict] = []
        self.i = -1
        self.active = False
        self._deadline = 0.0
        self._watch: dict = {}           # what the current watch step has seen
        self._timer = QTimer(self, interval=500, timeout=self._tick)

    # --- control ---------------------------------------------------------------------

    def start(self, ap_bb, cpes: list[tuple]) -> None:
        """ap_bb: NodeView; cpes: [(cpe NodeView, its BB NodeView, data interface)] in order."""
        cpe_bbs = list({bb.id: bb for _c, bb, _d in cpes}.values())
        self.ap_bb, self.cpes = ap_bb, cpes
        self.steps = [{"kind": "exec", "node": ap_bb, "key": "apbb.kill_dhcpd", "label": "Kill DHCP server"}]
        self.steps += [{"kind": "exec", "node": bb, "key": "cpebb.kill_dhclient", "label": "Kill dhclient"}
                       for bb in cpe_bbs]
        self.steps += [{"kind": "send", "node": ap_bb, "key": "apbb.dhcpd", "label": "Run DHCP server"},
                       {"kind": "dhcpd", "node": ap_bb, "label": "DHCP server listening"}]
        for cpe, bb, _d in cpes:
            self.steps += [{"kind": "send", "node": bb, "key": "cpebb.dhclient", "label": f"dhclient for {cpe.name}"},
                           {"kind": "dhclient", "node": bb, "label": f"{cpe.name}: Solicits done", "cpe": cpe}]
        self.steps.append({"kind": "verify", "node": ap_bb, "label": "verify addresses"})
        self.failed: list[str] = []
        self.failures: list[tuple[str, str]] = []   # (stage, why) of what went wrong, for the result line
        self.w.traffic.set_prep_result(None, "running…")
        self.i = -1
        self.active = True
        self.w.traffic.set_prep_running(True)
        self.w.traffic.log("info", "Prepare backbones: " + " → ".join(s["label"] for s in self.steps))
        self._timer.start()
        self._next()

    def stop(self, why: str = "stopped") -> None:
        if self.active:
            step = self._step()
            self._finish(None, f"Prepare backbones {why}",
                         f"■ Stopped{' at ' + self._stage(step) if step else ''} ({why})")

    def _stage(self, step: dict) -> str:
        return f"step {self.i + 1}/{len(self.steps)} · {step['node'].name}: {step['label']}"

    # --- steps -------------------------------------------------------------------------

    def _step(self) -> dict | None:
        return self.steps[self.i] if self.active and 0 <= self.i < len(self.steps) else None

    def _next(self) -> None:
        self.i += 1
        step = self._step()
        if step is None:
            if self.failed:
                names = ", ".join(dict.fromkeys(self.failed))      # each name once
                detail = "\n".join(f"   at {st}: {why}" for st, why in self.failures)
                return self._finish(False, f"Prepare backbones: done – problems: {names}",
                                    f"✖ Failed ({names}):\n{detail}")
            return self._finish(True, "Prepare backbones: done – ready for traffic",
                                "✔ Done successfully – the backbones are ready for traffic")
        node = step["node"]
        self.w.traffic.set_prep_state(f"{node.name}: {step['label']}…", "info")
        sent = self._watch.get("sent", "") if step["kind"] in ("dhcpd", "dhclient") else ""
        self._watch = {"t0": time.monotonic(), "solicit": None, "bound": False, "text": "", "no_subnet": set(),
                       "sent": sent, "echoed": not sent}
        if step["kind"] in ("exec", "send"):
            cmd = self.w.quick_text(node, step["key"])
            err = self.w.assoc_quick(node, step["key"])
            if err:
                return self._fail(err)
            self.w.traffic.log("info", f"{node.name}: {step['label']}  ({cmd})")
            if step["kind"] == "send":         # typed into the shell: the next step watches its output
                self._watch["sent"] = cmd.strip().splitlines()[0][:24] if cmd.strip() else ""
                return self._next()
            self._deadline = time.monotonic() + EXEC_TIMEOUT_S
        elif step["kind"] == "dhcpd":
            self._deadline = time.monotonic() + DHCPD_TIMEOUT_S
        elif step["kind"] == "dhclient":
            self._deadline = time.monotonic() + DHCLIENT_TIMEOUT_S
        else:
            self._verify_start()

    def _tick(self) -> None:
        step = self._step()
        if step is None:
            return
        if step["kind"] == "dhclient":
            last = self._watch["solicit"]
            quiet = time.monotonic() - (last or self._watch["t0"])
            if self._watch["bound"] and quiet >= QUIET_S:
                self.w.traffic.log("ok", f"{step['node'].name}: dhclient bound, no Solicit for {QUIET_S} s")
                return self._next()
        if time.monotonic() > self._deadline:
            if step["kind"] == "dhclient":
                why = ("still sending Solicits (no DHCP answer on " + step["cpe"].name + "'s interfaces?)"
                       if not self._watch["bound"] else "Solicits did not stop")
                return self._fail(f"{why} after {DHCLIENT_TIMEOUT_S} s")
            return self._fail("no result (timeout)")

    # --- results ------------------------------------------------------------------------

    def on_quick(self, node_id: str, key: str, ok: bool, detail: str) -> None:
        step = self._step()
        if not step or step["kind"] != "exec" or step["node"].id != node_id or step["key"] != key:
            return
        if ok:
            self.w.traffic.log("ok", f"{step['node'].name}: {step['label']}" + (f" – {detail[:100]}" if detail else ""))
            self._next()
        else:
            self._fail(f"{step['label']} failed: {detail[:200]}")

    def on_output(self, node_id: str, text: str) -> None:
        """Output of a backbone's Console shell."""
        step = self._step()
        if not step or step["node"].id != node_id or step["kind"] not in ("dhcpd", "dhclient"):
            return
        self._watch["text"] = (self._watch["text"] + text)[-6000:]
        if not self._watch["echoed"]:         # wait for the echo of our command: older output is not ours
            at = self._watch["text"].find(self._watch["sent"])
            if at < 0:
                return
            self._watch["echoed"] = True
            self._watch["text"] = self._watch["text"][at:]
            text = self._watch["text"]
        seen = self._watch["text"]
        if step["kind"] == "dhcpd":
            for iface in set(_NO_SUBNET.findall(seen)) - self._watch["no_subnet"]:
                self._watch["no_subnet"].add(iface)
                self.w.traffic.log("warn", f"{step['node'].name}: dhcpd: no subnet6 declaration for {iface} – "
                                           f"nothing is served there (check /etc/dhcp/dhcpd6_combined.conf)")
            if _DHCPD_FAIL.search(seen):
                return self._fail("dhcpd did not start: " + seen.strip().splitlines()[-1][:160])
            if _DHCPD_READY.search(seen):
                self.w.traffic.log("ok", f"{step['node'].name}: dhcpd: Server starting service")
                self._next()
        else:
            if _SOLICIT.search(text):
                self._watch["solicit"] = time.monotonic()
                n = len(_SOLICIT.findall(seen))
                self.w.traffic.set_prep_state(f"{step['label']}… ({n} Solicit lines)", "info")
            if _BOUND.search(text):
                self._watch["bound"] = True

    # --- verify -------------------------------------------------------------------------

    def _verify_start(self) -> None:
        self._deadline = time.monotonic() + VERIFY_TIMEOUT_S
        self._pending = {self.ap_bb.id} | {bb.id for _c, bb, _d in self.cpes}
        self._found: dict[str, list] = {}
        for bid in self._pending:
            node = self.w.ctx.registry.get(bid)
            if node is not None:
                self.w.ctx.bbnet.read_globals(node)

    def on_globals(self, node_id: str, addrs: list, error: str) -> None:
        step = self._step()
        if not step or step["kind"] != "verify" or node_id not in self._pending:
            return
        self._pending.discard(node_id)
        self._found[node_id] = addrs if not error else None
        if self._pending:
            return
        ap = self._found.get(self.ap_bb.id)
        if ap and AP_DATA_ADDR in {bare(a) for _f, a in ap}:
            self.w.traffic.log("ok", f"{self.ap_bb.name}: {AP_DATA_ADDR} (data) present")
        else:
            self.failed.append(self.ap_bb.name)
            self.failures.append((self._stage(step), f"{AP_DATA_ADDR} (data) not found on {self.ap_bb.name}"))
            self.w.traffic.log("fail", f"{self.ap_bb.name}: {AP_DATA_ADDR} (data) not found")
        for cpe, bb, data in self.cpes:
            on_data = [a for f, a in (self._found.get(bb.id) or []) if f.lower() == data.lower()]
            if on_data:
                self.w.traffic.log("ok", f"{cpe.name}: {data} = {', '.join(on_data)}")
            else:
                self.failed.append(cpe.name)
                self.failures.append((self._stage(step), f"{cpe.name}: no global IPv6 address on {data} ({bb.name})"))
                self.w.traffic.log("fail", f"{cpe.name}: no global IPv6 address on {data} ({bb.name})")
        self._next()

    # --- end ----------------------------------------------------------------------------

    def _fail(self, why: str) -> None:
        step = self._step()
        if step is None:
            return
        if step["kind"] == "dhclient":           # one CPE: note it and go on with the next
            self.failed.append(step["cpe"].name)
            self.failures.append((self._stage(step), why))
            self.w.traffic.log("fail", f"{step['node'].name}: {why} – next CPE")
            return self._next()
        self._finish(False, f"Prepare backbones stopped – {step['node'].name}: {why}",
                     f"✖ Failed at {self._stage(step)}\n   {why}")

    def _finish(self, ok: bool | None, text: str, result: str = "") -> None:
        self.active = False
        self._timer.stop()
        self.w.traffic.set_prep_running(False)
        self.w.traffic.set_prep_result(ok, result or text)
        self.w.traffic.set_prep_state(text, {True: "ok", False: "fail", None: "warn"}[ok])
        self.w.traffic.log({True: "ok", False: "fail", None: "warn"}[ok], text)
        self.w.statusBar().showMessage(text, 12000)
        if ok is not None:
            self.w.read_traffic_addrs()           # refresh the Addresses table
