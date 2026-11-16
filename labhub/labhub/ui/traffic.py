"""
Traffic tab: iperf scenarios between the AP's backbone and the CPEs' backbones.

    Uplink    CPEs -> AP     UDP / TCP
    Downlink  AP -> CPEs     UDP / TCP

Running: Downlink UDP / TCP for now (core/traffic.py does the checks; the iperf3
shells - one per CPE on its BB, one AP BB shell per CPE - show up below the
table). The other scenarios only animate the drawing.

Addresses: the address each device's traffic uses. One row per AP / CPE with a
backbone; the choices are the global IPv6 addresses on that BB (`ifconfig`,
inside the BB's namespace if it has one), written with their prefix length
(2008:db9::20/64). The address on the device's data interface is picked by
default; a pick made by hand is remembered.
"""

from __future__ import annotations

from html import escape

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
                               QPushButton, QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem, QTextEdit,
                               QVBoxLayout, QWidget)

from ..core.associate import BEACONS_NEEDED
from ..core.traffic import AP_DATA_ADDR, TOTAL_MBPS, bare
from .ap_view import muted, section
from .common import c as tc
from .console import ShellPane
from .traffic_diagram import TrafficDiagram
from .traffic_graph import TrafficGraph

COLS = ("Device", "Role", "Backbone", "IPv6 address (global)", "-b per stream (Mbit/s)", "")
SCENARIOS = (("ul", "udp", "Uplink UDP"), ("ul", "tcp", "Uplink TCP"),
             ("dl", "udp", "Downlink UDP"), ("dl", "tcp", "Downlink TCP"))
PREVIEW = "(preview – this scenario is not wired to iperf yet)"
# as in the CPE Short scan A / B / C buttons, plus the airtel channels (quick.TRAFFIC_SCANS)
CHANNEL_FREQ = {"A": 26660, "B": 27000, "C": 27340, "26000_airtel": 26000, "26330_airtel": 26330}
LOG_COLOR = {"info": "#8a90a0", "ok": "#3ccf7a", "warn": "#f0b43c", "fail": "#ff6b7a"}


class TrafficView(QWidget):
    refresh = Signal()
    picked = Signal(str, str)        # device id, "addr/prefixlen"
    scenario = Signal(str, str)      # "ul" / "dl", "udp" / "tcp"; ("", "") = stop
    shell_send = Signal(str, str)    # session key, text
    shell_interrupt = Signal(str)
    shell_key = Signal(str, str)
    associate = Signal()
    ap_up = Signal()                 # Make AP transmit
    ap_up_stop = Signal()
    associate_one = Signal(str)      # cpe id
    picks_changed = Signal()         # CPEs ticked / unticked for Associate checked
    run_picks_changed = Signal()     # CPEs ticked / unticked in Addresses (traffic runs, Prepare)
    prepare = Signal()
    prepare_stop = Signal()
    associate_stop = Signal()
    channel_changed = Signal(str, str)   # cpe id, a CHANNEL_FREQ key ("A" / "B" / "C" / "26000_airtel"...)

    def __init__(self) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(10)

        bar = QHBoxLayout()
        self.status = muted("")
        bar.addWidget(self.status, 1)
        b = QPushButton("⟳ Read addresses")
        b.setToolTip("ifconfig on every backbone (in its namespace, if set): the global IPv6 addresses")
        b.clicked.connect(self.refresh.emit)
        bar.addWidget(b)
        lay.addLayout(bar)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        host = QWidget()
        outer, lay = lay, QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 6, 0)
        lay.setSpacing(10)
        scroll.setWidget(host)
        # the run log at the top, outside the scroll area: the status of whatever runs is always in view
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFixedHeight(120)
        self.log_box.setPlaceholderText("Run log: Associate, Prepare backbones and the traffic runs report here.")
        outer.addWidget(self.log_box)
        outer.addWidget(scroll, 1)

        box, v = section("AP transmit")
        v.addWidget(muted("Short / Long scan → QCMAP bridge → MLDs up on the AP. Run it as often as you want. "
                          "Beacons sent: the TX legacy OFDM counters of wifistats wifi0 9 (phy0) / wifi1 9 (phy1)."))
        row = QHBoxLayout()
        self.ap_bcn = QLabel("phy0: –    phy1: –")
        self.ap_bcn.setStyleSheet("background: transparent; font-weight: 600; font-size: 10.5pt;")
        row.addWidget(self.ap_bcn)
        self.ap_bcn_state = QLabel("")
        self.ap_bcn_state.setStyleSheet("background: transparent; font-weight: 600;")
        row.addWidget(self.ap_bcn_state, 1)
        row.addWidget(QLabel("Scan"))
        self.scan_ap = self._scan_combo()
        row.addWidget(self.scan_ap)
        self.ap_up_btn = QPushButton("Make AP transmit")
        self.ap_up_btn.setObjectName("Primary")
        self.ap_up_btn.setToolTip("AP: scan (short / long) → QCMAP bridge → wait for mld0 / mld1 UP")
        self.ap_up_btn.clicked.connect(self.ap_up.emit)
        row.addWidget(self.ap_up_btn)
        b = QPushButton("■ Stop")
        b.setObjectName("Danger")
        b.clicked.connect(self.ap_up_stop.emit)
        row.addWidget(b)
        v.addLayout(row)
        lay.addWidget(box)

        box, v = section("Associate")
        v.addWidget(muted(f"AP: Short scan → QCMAP bridge → MLDs up.  Then one CPE at a time: Short scan on its "
                          f"channel → QCMAP bridge → {BEACONS_NEEDED} beacons heard → the next CPE."))
        head = QHBoxLayout()
        self.ap_assoc = QLabel("AP: –")
        self.ap_assoc.setStyleSheet("background: transparent; font-weight: 600;")
        head.addWidget(self.ap_assoc, 1)
        head.addWidget(QLabel("Scan"))
        self.scan_assoc = self._scan_combo()
        head.addWidget(self.scan_assoc)
        self.scan_ap.currentIndexChanged.connect(lambda i: self.scan_assoc.setCurrentIndex(i))
        self.scan_assoc.currentIndexChanged.connect(lambda i: self.scan_ap.setCurrentIndex(i))
        for text, on in (("All", True), ("None", False)):
            b = QPushButton(text)
            b.setToolTip("Tick every CPE" if on else "Untick every CPE")
            b.clicked.connect(lambda _c=False, on=on: self._pick_all(on))
            head.addWidget(b)
        self.assoc_btn = QPushButton("Associate checked")
        self.assoc_btn.setToolTip("If the AP is not beaconing: bring it up first. Then every connected CPE, "
                                  "one after the other")
        self.assoc_btn.setObjectName("Primary")
        self.assoc_btn.clicked.connect(self.associate.emit)
        head.addWidget(self.assoc_btn)
        self.assoc_stop_btn = QPushButton("■ Stop")
        self.assoc_stop_btn.setObjectName("Danger")
        self.assoc_stop_btn.setToolTip("Stop Associate, clear every status, then Reboot all (APs and CPEs)")
        self.assoc_stop_btn.clicked.connect(self.associate_stop.emit)
        head.addWidget(self.assoc_stop_btn)
        v.addLayout(head)
        self.assoc_table = QTableWidget(0, 6)
        self.assoc_table.setHorizontalHeaderLabels(("CPE", "Channel", "", "Beacons heard", "Status", "Associate step"))
        self.assoc_table.verticalHeader().hide()
        self.assoc_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.assoc_table.setSelectionMode(QTableWidget.NoSelection)
        ah = self.assoc_table.horizontalHeader()
        for col in (0, 1, 2, 3):
            ah.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        ah.setSectionResizeMode(4, QHeaderView.Interactive)
        self.assoc_table.setColumnWidth(4, 300)   # "✔ associated · after ≤ 150 beacons" fits
        ah.setSectionResizeMode(5, QHeaderView.Stretch)
        v.addWidget(self.assoc_table)
        lay.addWidget(box)
        self.assoc_rows: dict[str, dict] = {}      # cpe id -> {pick, combo, count, state}
        self.assoc_off: set[str] = set()           # CPEs unticked (left out of Associate checked)
        self.channels: dict[str, str] = {}         # cpe id -> channel picked (remembered)

        box, v = section("Backbones")
        v.addWidget(muted("AP BB: Kill DHCP server → CPE BBs: Kill dhclient → AP BB: Run DHCP server (waits until it "
                          "serves) → each CPE BB in turn: dhclient (waits until bound and the Solicits stop) → "
                          "verify the addresses.  Run it before the traffic."))
        row = QHBoxLayout()
        self.prep_state = QLabel("not prepared yet")
        self.prep_state.setStyleSheet(f"background: transparent; font-weight: 600; color: {tc('#8a90a0')};")
        self.prep_state.setWordWrap(True)
        row.addWidget(self.prep_state, 1)
        self.prep_btn = QPushButton("Prepare backbones")
        self.prep_btn.setObjectName("Primary")
        self.prep_btn.clicked.connect(self.prepare.emit)
        row.addWidget(self.prep_btn)
        self.prep_stop_btn = QPushButton("■ Stop")
        self.prep_stop_btn.setObjectName("Danger")
        self.prep_stop_btn.setEnabled(False)
        self.prep_stop_btn.clicked.connect(self.prepare_stop.emit)
        row.addWidget(self.prep_stop_btn)
        v.addLayout(row)
        self.prep_result = QLabel("")          # under the button: done successfully / failed + at which stage
        self.prep_result.setWordWrap(True)
        self.prep_result.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.prep_result.hide()
        v.addWidget(self.prep_result)
        lay.addWidget(box)

        box, v = section("Scenarios")
        self.diagram = TrafficDiagram()
        v.addWidget(self.diagram)
        row = QHBoxLayout()
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for direction, proto, label in SCENARIOS:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setToolTip(("CPEs → AP" if direction == "ul" else "AP → CPEs") + f", {proto.upper()}"
                         + ("  (data one way, ACKs back)" if proto == "tcp" else ""))
            b.clicked.connect(lambda _c=False, d=direction, pr=proto: self._start(d, pr))
            self.group.addButton(b)
            row.addWidget(b)
        stop = QPushButton("■ Stop")
        stop.setObjectName("Danger")
        stop.clicked.connect(self.stop)
        row.addWidget(stop)
        row.addStretch(1)
        v.addLayout(row)
        lay.addWidget(box)

        box, v = section("Addresses")
        pick_row = QHBoxLayout()
        pick_row.addWidget(QLabel("Pick by default"))
        self.pick_mode = QComboBox()
        self.pick_mode.addItem("data interface (.1015 / v10371015)", "data")
        self.pick_mode.addItem("control interface (.960)", "ctrl")
        self.pick_mode.setToolTip("Which address every row takes unless you pick one by hand.\n"
                                  "Traffic belongs on the data interface; the control (.960) one is much slower.")
        self.pick_mode.currentIndexChanged.connect(lambda _i: self._repick())
        pick_row.addWidget(self.pick_mode)
        pick_row.addSpacing(30)
        b = QPushButton(f"Auto ({TOTAL_MBPS}/N)")
        b.setToolTip(f"Every CPE back to -b {TOTAL_MBPS}/N per stream, N = CPEs with an address (values set by hand "
                     "are dropped)")
        b.clicked.connect(self._rates_auto)
        pick_row.addWidget(b)
        pick_row.addStretch(1)
        v.addLayout(pick_row)
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        h = self.table.horizontalHeader()
        for col in (0, 1, 2, 4):
            h.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.Stretch)
        h.setSectionResizeMode(5, QHeaderView.Stretch)
        v.addWidget(self.table)
        lay.addWidget(box)

        box, v = section("Traffic  (left: iperf3 shells, per CPE the AP BB client and the CPE BB server · "
                         "right: live throughput of the whole run)")
        split = QHBoxLayout()
        split.setSpacing(24)
        left = QVBoxLayout()
        self.shell_grid = QGridLayout()
        self.shell_grid.setVerticalSpacing(6)
        left.addLayout(self.shell_grid)
        self.no_shells = muted("No traffic running. Pick a scenario above.")
        left.addWidget(self.no_shells)
        left.addStretch(1)
        split.addLayout(left, 4)
        right = QVBoxLayout()
        self.graph = TrafficGraph()
        right.addWidget(self.graph, 1)
        cap = QHBoxLayout()
        view = QComboBox()
        view.addItem("One chart per CPE", True)
        view.addItem("All CPEs in one chart", False)
        view.setToolTip("Per CPE: same time axis and Mbit/s scale, so CPEs with the same rate do not hide each other")
        view.currentIndexChanged.connect(lambda _i: self.graph.set_split(bool(view.currentData())))
        cap.addWidget(view)
        cap.addStretch(1)
        b = QPushButton("Capture graph")
        b.setToolTip("Save the graph of the whole run as a PNG, with a table of 10 samples under it")
        b.clicked.connect(self.capture_graph)
        cap.addWidget(b)
        right.addLayout(cap)
        split.addLayout(right, 6)
        v.addLayout(split)
        lay.addWidget(box)
        lay.addStretch(1)
        self.panes: dict[str, ShellPane] = {}     # session key -> pane
        self._flush = QTimer(self, interval=80, timeout=lambda: [p.flush() for p in self.panes.values()])
        self._flush.start()

        self.rows: dict[str, dict] = {}     # device id -> {row, bb_id, data, ctrl, combo, note}
        self.saved: dict[str, str] = {}     # device id -> address picked by hand (remembered)
        self.by_hand: set[str] = set()      # picked by hand in this session: wins over "Pick by default"
        self.rates: dict[str, int] = {}     # cpe id -> -b per stream set by hand (Mbit/s)
        self.traffic_off: set[str] = set()  # CPEs unticked in Addresses: left out of runs and Prepare
        self.manual_addr: dict[str, str] = {}   # device id -> address typed in by hand

    # --- scenarios ---------------------------------------------------------------

    def _start(self, direction: str, proto: str) -> None:
        self.scenario.emit(direction, proto)

    def stop(self) -> None:
        self.uncheck()
        self.scenario.emit("", "")

    def uncheck(self) -> None:
        self.group.setExclusive(False)
        for b in self.group.buttons():
            b.setChecked(False)
        self.group.setExclusive(True)

    def log(self, level: str, text: str) -> None:
        self.log_box.append(f"<span style='color:{tc(LOG_COLOR.get(level, '#8a90a0'))}'>"
                            f"{'✔ ' if level == 'ok' else '⚠ ' if level == 'warn' else '✖ ' if level == 'fail' else ''}"
                            f"{escape(text)}</span>")

    # --- AP transmit -------------------------------------------------------------

    @staticmethod
    def _scan_combo() -> QComboBox:
        c = QComboBox()
        c.addItem("Short scan (hw_ver_override 2)", "short")
        c.addItem("Long scan (hw_ver_override 1)", "long")
        return c

    def scan_mode(self) -> str:
        return self.scan_assoc.currentData() or "short"

    def set_ap_beacons(self, phy0: int | None, phy1: int | None, beaconing: bool | None, error: str = "") -> None:
        fmt = lambda v: "–" if v is None else f"{v:,}"
        self.ap_bcn.setText(f"phy0: {fmt(phy0)}    phy1: {fmt(phy1)}")
        self.ap_bcn.setToolTip(error or "beacons sent since boot (TX legacy OFDM frames)")
        text, color = {True: ("● beaconing", "#3ccf7a"), False: ("✖ not beaconing", "#ff6b7a"),
                       None: ("", "#8a90a0")}[beaconing]
        if error and beaconing is None:
            text, color = f"✖ {error[:80]}", "#ff6b7a"
        self.ap_bcn_state.setText("   " + text)
        self.ap_bcn_state.setStyleSheet(f"background: transparent; font-weight: 600; color: {tc(color)};")

    # --- associate ----------------------------------------------------------------

    def _assoc_devices(self) -> None:
        cpes = [(did, r["name"]) for did, r in self.rows.items() if r["role"] == "cpe"]
        if [c[0] for c in cpes] == list(self.assoc_rows):
            return
        self.assoc_rows.clear()
        self.assoc_table.setRowCount(len(cpes))
        for i, (did, name) in enumerate(cpes):
            pick = QCheckBox(name)
            pick.setChecked(did not in self.assoc_off)
            pick.setToolTip("Ticked: part of 'Associate checked'")
            pick.toggled.connect(lambda on, d=did: self._pick(d, on))
            self.assoc_table.setCellWidget(i, 0, pick)
            combo = QComboBox()
            for ch, freq in CHANNEL_FREQ.items():
                combo.addItem(ch if str(freq) in ch else f"{ch}  ({freq})", ch)   # "26000_airtel" has it already
            combo.setCurrentIndex(max(0, combo.findData(self.channels.get(did, "B"))))
            combo.activated.connect(lambda _i, d=did, c=combo: self._channel(d, c.currentData()))
            self.assoc_table.setCellWidget(i, 1, combo)
            one = QPushButton("Associate")
            one.setToolTip(f"Only {name}: Short scan on its channel → QCMAP bridge → {BEACONS_NEEDED} beacons "
                           "(the AP must be up already)")
            one.clicked.connect(lambda _c=False, d=did: self.associate_one.emit(d))
            self.assoc_table.setCellWidget(i, 2, one)
            count = QLabel("–")
            count.setStyleSheet("background: transparent; padding: 0 8px; font-weight: 600;")
            self.assoc_table.setCellWidget(i, 3, count)
            assoc = QLabel("–")
            assoc.setStyleSheet("background: transparent; padding: 0 10px 0 6px; font-weight: 600;")
            self.assoc_table.setCellWidget(i, 4, assoc)
            state = QLabel("")
            state.setStyleSheet("background: transparent; padding-left: 6px;")
            self.assoc_table.setCellWidget(i, 5, state)
            self.assoc_rows[did] = {"combo": combo, "one": one, "count": count, "assoc": assoc, "state": state,
                                    "pick": pick}
        self.assoc_table.resizeRowsToContents()
        rows_h = sum(self.assoc_table.rowHeight(r) for r in range(self.assoc_table.rowCount()))
        self.assoc_table.setFixedHeight(self.assoc_table.horizontalHeader().height() + rows_h + 6)

    def _channel(self, did: str, ch: str) -> None:
        self.channels[did] = ch
        self.channel_changed.emit(did, ch)

    def is_ticked(self, cpe_id: str) -> bool:
        return cpe_id not in self.assoc_off

    def _pick(self, did: str, on: bool) -> None:
        (self.assoc_off.discard if on else self.assoc_off.add)(did)
        self.picks_changed.emit()

    def _pick_all(self, on: bool) -> None:
        for did, r in self.assoc_rows.items():
            r["pick"].setChecked(on)

    def channel(self, cpe_id: str) -> str:
        r = self.assoc_rows.get(cpe_id)
        return (r["combo"].currentData() if r else None) or self.channels.get(cpe_id, "B")

    def set_beacons(self, cpe_id: str, value: int | None, error: str = "") -> None:
        r = self.assoc_rows.get(cpe_id)
        if r:
            r["count"].setText("–" if value is None else f"📶 {value}")
            r["count"].setToolTip(error or "rx_legacy_ofdm_rate[3]: beacons heard (wifistats wifi0 10)")
            r["count"].setStyleSheet(f"background: transparent; padding: 0 8px; font-weight: 600; "
                                     f"color: {tc('#ff6b7a' if value is None else '#56d4dd')};")
        name = self.rows.get(cpe_id, {}).get("name")
        if name:
            self.diagram.set_beacons(name, value)

    def set_association(self, cpe_id: str, associated: bool | None, at_beacons: int | None,
                        exact: bool = True) -> None:
        """Status column: associated (as in Graphs: iw ath0 link) and the beacons heard until then."""
        r = self.assoc_rows.get(cpe_id)
        if not r:
            return
        if associated is None:
            text, color = "–", "#8a90a0"
        elif associated:
            # exact = seen going from not associated to associated; else it already was (≤ the counter then)
            text = "✔ associated" + (f" · after {'' if exact else '≤ '}{at_beacons} beacons"
                                     if at_beacons is not None else "")
            color = "#3ccf7a"
        else:
            text, color = "✖ not associated", "#ff6b7a"
        r["assoc"].setText(text)
        r["assoc"].setToolTip("iw ath0 link on the CPE ('Connected to' = associated);\n"
                              "the beacon counter when it was first seen associated")
        r["assoc"].setStyleSheet(f"background: transparent; padding: 0 10px 0 6px; font-weight: 600; "
                                 f"color: {tc(color)};")

    def set_assoc_state(self, who: str, text: str, level: str = "info") -> None:
        """who: 'ap' or a CPE id."""
        color = tc(LOG_COLOR.get(level, "#8a90a0"))
        if who == "ap":
            self.ap_assoc.setText(f"AP: {text}")
            self.ap_assoc.setStyleSheet(f"background: transparent; font-weight: 600; color: {color};")
            return
        r = self.assoc_rows.get(who)
        if r:
            r["state"].setText(text)
            r["state"].setStyleSheet(f"background: transparent; padding-left: 6px; color: {color};")

    def set_prep_state(self, text: str, level: str = "info") -> None:
        self.prep_state.setText(text)
        self.prep_state.setStyleSheet(f"background: transparent; font-weight: 600; "
                                      f"color: {tc(LOG_COLOR.get(level, '#8a90a0'))};")

    def set_prep_result(self, ok: bool | None, text: str) -> None:
        """ok True: green, False: red, None: running / stopped (amber)."""
        color = {True: "#3ccf7a", False: "#ff6b7a", None: "#f0b43c"}[ok]
        self.prep_result.setText(text)
        self.prep_result.setStyleSheet(f"background: transparent; font-weight: 600; font-size: 10pt; "
                                       f"color: {tc(color)}; padding: 4px 0;")
        self.prep_result.setVisible(bool(text))

    def set_prep_running(self, running: bool) -> None:
        self.prep_btn.setEnabled(not running)
        self.prep_stop_btn.setEnabled(running)

    def clear_assoc(self) -> None:
        """Back to a clean table: no AP line, step, association or beacon count."""
        self.set_assoc_state("ap", "–", "info")
        self.set_ap_beacons(None, None, None)
        for did, r in self.assoc_rows.items():
            r["state"].setText("")
            self.set_association(did, None, None)
            r["count"].setText("–")
            r["count"].setStyleSheet("background: transparent; padding: 0 8px; font-weight: 600;")
        self.diagram.beacons.clear()
        self.diagram.update()

    def set_assoc_running(self, running: bool) -> None:
        self.assoc_btn.setEnabled(not running)
        self.ap_up_btn.setEnabled(not running)
        for r in self.assoc_rows.values():
            r["combo"].setEnabled(not running)
            r["one"].setEnabled(not running)

    # --- iperf3 shells -----------------------------------------------------------

    def add_pane(self, key: str, session, title: str, row: int, col: int) -> ShellPane:
        pane = self.panes.get(key)
        if pane is None:
            pane = ShellPane(session, title)
            pane.setMinimumHeight(150)          # small: the graph on the right is the main view
            pane.setMaximumHeight(200)
            pane.send.connect(self.shell_send.emit)
            pane.interrupt.connect(self.shell_interrupt.emit)
            pane.key.connect(self.shell_key.emit)
            pane.close_req.connect(self.shell_interrupt.emit)   # ✕ here = stop that iperf3
            self.panes[key] = pane
        else:
            pane.set_session(session)
        self.shell_grid.addWidget(pane, row * 2 + col, 0)   # one column: client, server, next CPE...
        self.no_shells.hide()
        return pane

    def capture_graph(self) -> None:
        from datetime import datetime

        from PySide6.QtWidgets import QFileDialog, QMessageBox
        if not self.graph.has_data():
            QMessageBox.information(self, "Capture graph", "No traffic data yet.")
            return
        name = f"traffic_{datetime.now():%Y%m%d_%H%M%S}.png"
        path, _ = QFileDialog.getSaveFileName(self, "Save graph", name, "PNG image (*.png)")
        if path:
            ok = self.graph.capture().save(path)
            self.log("ok" if ok else "fail", f"graph saved: {path}" if ok else f"could not save {path}")

    def clear_panes(self) -> None:
        for pane in self.panes.values():
            pane.deleteLater()
        self.panes.clear()
        self.no_shells.show()

    def output(self, session, text: str, clear: bool) -> bool:
        pane = self.panes.get(session.node_id)
        if pane and pane.session is session:
            pane.append(text, clear)
            return True
        return pane is not None

    def state(self, session) -> bool:
        pane = self.panes.get(session.node_id)
        if pane and pane.session is session:
            pane.update_state()
        return pane is not None

    # --- -b per stream ---------------------------------------------------------------

    def is_run_ticked(self, did: str) -> bool:
        return did not in self.traffic_off

    def _run_pick(self, did: str, on: bool) -> None:
        (self.traffic_off.discard if on else self.traffic_off.add)(did)
        self.run_picks_changed.emit()
        self._show_rates()

    def auto_rate(self) -> int:
        n = sum(1 for did, r in self.rows.items()
                if r["role"] == "cpe" and self.selected(did) and did not in self.traffic_off)
        return max(1, round(TOTAL_MBPS / n)) if n else TOTAL_MBPS

    def manual_rate(self, did: str) -> int:
        """-b set by hand for this CPE, 0 = the rule (TOTAL_MBPS / CPEs in the run)."""
        return self.rates.get(did, 0)

    def _rate_typed(self, did: str, value: int) -> None:
        if not self._rates_updating:
            self.rates[did] = value
            self._show_rates()

    def _rates_auto(self) -> None:
        self.rates.clear()
        self._show_rates()

    _rates_updating = False

    def _show_rates(self) -> None:
        auto = self.auto_rate()
        self._rates_updating = True
        try:
            for did, r in self.rows.items():
                spin = r.get("rate")
                if spin is None:
                    continue
                spin.setValue(self.rates.get(did, auto))
                by_hand = did in self.rates
                spin.setStyleSheet(f"color: {tc('#f0b43c' if by_hand else '#d8dce4')};")
                spin.setPrefix("✎ " if by_hand else "")
        finally:
            self._rates_updating = False

    def _sync_diagram(self) -> None:
        def node(did: str) -> dict:
            r = self.rows[did]
            return {"name": r["name"], "bb_name": r["bb_name"], "addr": self.selected(did)}
        self._show_rates()                    # N (CPEs with an address) may have changed
        aps = [did for did, r in self.rows.items() if r["role"] == "ap"]
        self.diagram.set_nodes(node(aps[0]) if aps else None,
                               [node(did) for did, r in self.rows.items() if r["role"] == "cpe"])

    # --- devices -----------------------------------------------------------------

    def set_devices(self, devices: list[dict]) -> None:
        """devices: [{id, name, role, bb_id, bb_name, data}] (data = the device's data interface on its BB)."""
        if [d["id"] for d in devices] == list(self.rows) and \
                all(self.rows[d["id"]]["bb_id"] == d["bb_id"] for d in devices):
            for d in devices:   # same rows; the BB's own spelling of the interface may be known now
                self.rows[d["id"]]["data"] = d["data"]
                self.rows[d["id"]]["ctrl"] = d.get("ctrl", "")
            return
        old = {did: self._items(r["combo"]) for did, r in self.rows.items()}
        self.rows.clear()
        self.table.setRowCount(len(devices))
        for r, d in enumerate(devices):
            if d["role"] == "cpe":
                tick = QCheckBox(d["name"])
                tick.setChecked(d["id"] not in self.traffic_off)
                tick.setToolTip("Ticked: in the traffic runs and Prepare backbones. Unticked: left out entirely (its backbone is not touched).")
                tick.toggled.connect(lambda on, did=d["id"]: self._run_pick(did, on))
                self.table.setCellWidget(r, 0, tick)
            else:
                self.table.setItem(r, 0, QTableWidgetItem(d["name"]))
            for col, val in enumerate((d["role"].upper(), d["bb_name"] or "–"), 1):
                self.table.setItem(r, col, QTableWidgetItem(val))
            combo = QComboBox()
            combo.setMinimumWidth(320)
            combo.setEditable(True)            # an address can also be typed in by hand
            combo.setInsertPolicy(QComboBox.NoInsert)
            combo.lineEdit().setPlaceholderText("pick, or type an address (e.g. 2008:db9::21)")
            combo.lineEdit().editingFinished.connect(lambda did=d["id"]: self._typed(did))
            combo.activated.connect(lambda _i, did=d["id"]: self._picked(did))
            self.table.setCellWidget(r, 3, combo)
            rate = None
            if d["role"] == "cpe":
                rate = QSpinBox()
                rate.setRange(1, 100000)
                rate.setSuffix(" M")
                rate.setToolTip(f"iperf3 -b per stream for this CPE. Auto: {TOTAL_MBPS} / CPEs with an address; "
                                "a value typed here stays (Auto button: back to the rule)")
                rate.valueChanged.connect(lambda v, did=d["id"]: self._rate_typed(did, v))
                self.table.setCellWidget(r, 4, rate)
            note = QLabel()
            note.setStyleSheet("background: transparent; padding-left: 6px;")
            self.table.setCellWidget(r, 5, note)
            self.rows[d["id"]] = {"row": r, "bb_id": d["bb_id"], "data": d["data"], "ctrl": d.get("ctrl", ""),
                                  "combo": combo, "note": note, "rate": rate,
                                  "name": d["name"], "role": d["role"], "bb_name": d["bb_name"]}
            if not d["bb_id"]:
                self._note(d["id"], "no backbone in Mappings", "#8a90a0")
            elif old.get(d["id"]):
                self._fill(d["id"], old[d["id"]])
            else:
                self._note(d["id"], "not read yet", "#8a90a0")
        self.table.resizeRowsToContents()
        rows_h = sum(self.table.rowHeight(r) for r in range(self.table.rowCount()))
        self.table.setFixedHeight(self.table.horizontalHeader().height() + rows_h + 6)   # all rows, no scrollbar
        self._assoc_devices()
        self._sync_diagram()

    def bb_ids(self) -> list[str]:
        return sorted({r["bb_id"] for r in self.rows.values() if r["bb_id"]})

    # --- addresses ---------------------------------------------------------------

    def reading(self, bb_id: str) -> None:
        for did, r in self.rows.items():
            if r["bb_id"] == bb_id:
                self._note(did, "reading…", "#8a90a0")

    def set_addrs(self, bb_id: str, addrs: list, error: str = "") -> None:
        """addrs: [(interface, "addr/prefixlen")] of one BB."""
        for did, r in self.rows.items():
            if r["bb_id"] != bb_id:
                continue
            if error:
                r["combo"].clear()
                self._note(did, f"✖ {error}", "#ff6b7a")
            else:
                self._fill(did, list(addrs))
        self._sync_diagram()

    def selected(self, device_id: str) -> str:
        """The address of a device: the one picked ("addr/prefixlen"), or the one typed in."""
        r = self.rows.get(device_id)
        if not r:
            return ""
        typed = self.manual_addr.get(device_id)
        if typed:
            return typed
        data = r["combo"].currentData()
        return data[1] if data else ""

    def is_manual(self, device_id: str) -> bool:
        return bool(self.manual_addr.get(device_id))

    def _typed(self, did: str) -> None:
        """Text typed in a row's address box: one of the BB's addresses, or one of the user's own."""
        r = self.rows.get(did)
        if not r:
            return
        combo = r["combo"]
        text = combo.currentText().strip()
        hit = combo.findText(text)
        if not text or (hit >= 0 and combo.itemData(hit)):
            self.manual_addr.pop(did, None)    # back to a listed one
        else:
            self.manual_addr[did] = text.split()[0]
        self._update_note(did)
        self._sync_diagram()

    def _fill(self, did: str, addrs: list) -> None:
        r = self.rows[did]
        combo = r["combo"]
        combo.clear()
        for iface, addr in addrs:
            combo.addItem(f"{addr}    ({iface})", (iface, addr))
        if self.manual_addr.get(did):          # typed by hand: keep it over a re-read
            combo.setEditText(self.manual_addr[did])
            self._update_note(did)
            return
        if not addrs:
            self._note(did, "✖ no global IPv6 address on the BB – pick after dhclient, or type one", "#ff6b7a")
            return
        combo.setCurrentIndex(self._default_index(did, addrs))
        self._update_note(did)

    def _target(self, did: str) -> str:
        """The interface a row should use: its data one (default) or its control one."""
        r = self.rows[did]
        return (r["ctrl"] if self.pick_mode.currentData() == "ctrl" else r["data"]) or ""

    def _default_index(self, did: str, addrs: list) -> int:
        r = self.rows[did]
        target = self._target(did).lower()
        want = self.saved.get(did)
        hit = next((i for i, (f, a) in enumerate(addrs) if a == want), None)
        # a remembered pick only if it was made by hand now, or it is on the interface to use
        if hit is not None and (did in self.by_hand or addrs[hit][0].lower() == target):
            return hit
        if r["role"] == "ap" and self.pick_mode.currentData() == "data":   # the AP BB's data address is fixed
            hit = next((i for i, (_f, a) in enumerate(addrs) if bare(a) == AP_DATA_ADDR), None)
            if hit is not None:
                return hit
        return next((i for i, (f, _a) in enumerate(addrs) if f.lower() == target), 0)

    def _repick(self) -> None:
        """"Pick by default" changed: every row takes that interface again (hand picks are dropped)."""
        self.by_hand.clear()
        for did, r in self.rows.items():
            items = self._items(r["combo"])
            if items:
                r["combo"].setCurrentIndex(self._default_index(did, items))
                self._update_note(did)
        self._sync_diagram()

    def _picked(self, did: str) -> None:
        addr = self.selected(did)
        if addr:
            self.saved[did] = addr
            self.by_hand.add(did)
            self.picked.emit(did, addr)
        self._update_note(did)
        self._sync_diagram()

    def _update_note(self, did: str) -> None:
        r = self.rows[did]
        if self.manual_addr.get(did):
            self._note(did, f"✎ typed by hand: {self.manual_addr[did]}", "#f0b43c")
            return
        data = r["combo"].currentData()
        n = r["combo"].count()
        if not data:
            return
        on_data = data[0].lower() == (r["data"] or "").lower() or (r["role"] == "ap" and bare(data[1]) == AP_DATA_ADDR)
        on_ctrl = bool(r["ctrl"]) and data[0].lower() == r["ctrl"].lower()
        where = "data" if on_data else ("control – slow for traffic" if on_ctrl else f"not on {r['data']}")
        self._note(did, f"✔ {n} global address{'es' if n != 1 else ''}  ·  {where}",
                   "#3ccf7a" if on_data or not r["data"] else ("#ff6b7a" if on_ctrl else "#f0b43c"))

    def _note(self, did: str, text: str, color: str) -> None:
        note = self.rows[did]["note"]
        note.setText(text)
        note.setStyleSheet(f"color: {tc(color)}; background: transparent; padding-left: 6px;")

    @staticmethod
    def _items(combo: QComboBox) -> list:
        return [combo.itemData(i) for i in range(combo.count()) if combo.itemData(i)]
