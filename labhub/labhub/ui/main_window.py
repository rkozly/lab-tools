"""
Main window: toolbar, device strip, command bar, presets + workspace tabs.
"""

from __future__ import annotations

import dataclasses
import os
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QHBoxLayout,
                               QLabel, QMainWindow, QMessageBox, QPushButton,
                               QSplitter, QTabWidget, QVBoxLayout, QWidget)

from .. import parsers
from ..core import events as ev
from ..core import quick
from ..core.bbnet import BbAddrs, BbExecDone, BbPeerAddr, BbRoutes, lookup
from ..core.topology import IfaceDetail, TopologyResult
from ..core.live import BeaconObservation, BeaconsUpdated, PollerState, Sample, SampleAdded
from ..core.models import DeviceConfig
from ..core.presets import Preset, SequenceSpec, expand
from ..core.runner import JobOutput, JobStateChanged, RunFinished, RunStarted
from ..core.shells import ShellOutput, ShellState
from ..core.sequence import (DEFAULT_SUCCESS_WORDS, CheckResult, CommandCheck, MenuSequence,
                             SequenceProgress, words_regex)
from ..core.ramdump import DumpAction, DumpListing
from ..core.reboot import RebootProgress
from ..core import license as lic
from .ap_view import ApView
from ..core.targets import TargetError, node_role
from ..discovery.adb_discovery import Discovery
from .beacons import BeaconsView
from .common import STATUS_COLOR, QtBridge
from .console import CommandBar, ConsoleView, StatsView
from .graphs import GraphsView
from .device_strip import DeviceStrip
from .mapping_editor import MappingEditor
from .monitor import MonitorView
from .presets_panel import PresetPanel
from .routes import RoutesDialog
from .topology import TopologyView

ENV_LAB = "__env__"


class MainWindow(QMainWindow):
    def __init__(self, ctx) -> None:
        super().__init__()
        self.ctx = ctx
        self.setWindowTitle("LabHub")
        self.resize(1440, 900)
        self.bridge = QtBridge(ctx.bus)
        self.bridge.event.connect(self._on_event)
        from ..transports import base as transport
        transport.ADB_OVERRIDE = ctx.user.get("adb_path") or None
        self.discovery = Discovery(ctx.registry, ctx.bus)

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._toolbar())
        self.strip = DeviceStrip()
        self.strip.selection_changed.connect(self._on_selection)
        self.strip.assign_requested.connect(lambda s: self.edit_mappings(assign=s))
        root.addWidget(self.strip)
        self.cmd = CommandBar()
        self.cmd.run_requested.connect(self.run_command)
        self.cmd.stop_all.connect(self.stop_everything)
        self.cmd.open_shells.connect(self.open_shells)
        self.cmd.reboot_requested.connect(self._reboot_targets)
        self._reopen_after_reboot: dict[str, bool] = {}
        self.cmd.set_resolver(lambda t: [n.name for n in self.ctx.resolve(t)])
        root.addWidget(self.cmd)

        self.presets = PresetPanel(ctx)          # kept for editing; shown as a menu, not a side panel
        self.presets.activated.connect(self.run_preset)
        self.presets.changed.connect(self._presets_changed)
        self.cmd.set_preset_menu(self._build_preset_menu)
        self.tabs = QTabWidget()
        self.console = ConsoleView()
        self.console.stop_job.connect(ctx.runner.stop_job)
        self.console.export_run.connect(self.export_last_run)
        self.console.shell_send.connect(lambda nid, text: self._shell_write(nid, text))
        self.console.shell_interrupt.connect(self._shell_interrupt)
        self.console.shell_key.connect(self._shell_key)
        self.console.shell_close.connect(self.close_shell)
        self.console.shell_reconnect.connect(self.reconnect_shell)
        self.console.close_shells.connect(self.close_all_shells)
        self.console.quick.connect(self.run_quick)
        self.console.quick_edit.connect(self.edit_quick)
        self.console.addr_refresh.connect(self.check_bb_addrs)
        self._bb_actual: dict[str, dict[str, str]] = {}   # BB id -> {lower-case iface: name on the BB}
        self._routes: dict[str, RoutesDialog] = {}         # BB id -> its routing table window
        self._topology_pending: list[str] = []
        self._addr_timer = QTimer(self, interval=20000, timeout=self._check_all_bb_addrs)
        self._addr_timer.start()
        self.console.set_row_layout(self._shell_rows)
        self._iperf_carry: dict[str, str] = {}    # shell id -> unfinished output line
        self._quick_checks: dict[str, str] = {}   # node id -> quick key of a running check
        self._quick_seq: dict[str, str] = {}      # node id -> quick key of a running QCMAP
        self.monitor = MonitorView()
        self.monitor.start_requested.connect(self.start_monitor_id)
        self.monitor.stop_requested.connect(self.stop_monitor)
        self.monitor.export_requested.connect(self.export_samples)
        self.beacons = BeaconsView()
        self.beacons.scan_requested.connect(self.collect_beacons)
        self.beacons.auto_changed.connect(self._beacon_auto)
        self.beacons.clear_requested.connect(ctx.beacons.clear)
        self.ap = ApView()
        self.ap.scan_requested.connect(self.ap_scan)
        self.ap.qcmap_requested.connect(self.ap_qcmap)
        self.ap.qcmap_stop.connect(self.stop_sequences)
        self.ap.qcmap_exit.connect(self.exit_menu)
        self.ap.stats_start.connect(self.ap_stats_start)
        self.ap.stats_stop.connect(self.ap_stats_stop)
        self.ap.dumps.list_requested.connect(lambda path: self.ctx.ramdumps.list(self._dump_nodes(), path))
        self.ap.dumps.watch_changed.connect(self._dump_watch)
        self.ap.dumps.pull_requested.connect(self.pull_dumps)
        self.ap.dumps.clear_requested.connect(self.clear_dumps)
        self.ap.shell_open.connect(lambda: [self._open_one_shell(n, show=False) for n in self._need_ap()])
        self.ap.shell_show.connect(self._show_ap_shell)
        self.ap.reboot_requested.connect(lambda: self.reboot(self._need_ap()))
        self.ap.license_push.connect(self.push_license)
        self._qwes_carry: dict[str, str] = {}   # qwes shell key -> unfinished line
        self._qwes_timer = QTimer(self, interval=8000, timeout=self._qwes_tick)
        self._qwes_timer.start()
        self.ap.mode.currentIndexChanged.connect(self._qcmap_groups)
        self.ap.success_words.setText(ctx.user.get("qcmap_success_words") or DEFAULT_SUCCESS_WORDS)
        self.ap.success_words.editingFinished.connect(
            lambda: self.ctx.user.set("qcmap_success_words", self.ap.success_words.text().strip()))
        self._sequences: dict[str, MenuSequence] = {}
        # Topology + Console + Stats. AP / Monitor / Beacons stay built (the event handlers
        # still feed them) but are not tabs any more.
        self.topology = TopologyView()
        self.topology.refresh_requested.connect(self.refresh_topology)
        self.topology.iface_clicked.connect(self._iface_clicked)
        self.tabs.addTab(self.topology, "Topology")
        self.tabs.addTab(self.console, "Console")
        self.stats = StatsView()
        self.stats.restart.connect(self.restart_stats)
        self.stats.interrupt.connect(self.ctx.shells.interrupt)
        self.stats.restart_all.connect(lambda: [self.restart_stats(d) for d in list(self.stats.panes)])
        self.stats.stop_all.connect(self.stop_all_stats)
        self.tabs.addTab(self.stats, "Stats")
        self.graphs = GraphsView()
        self.graphs.restart_all.connect(lambda: [self.restart_graphs(d) for d in list(self.graphs.cards)])
        self.graphs.stop_all.connect(self.stop_all_graphs)
        self.tabs.addTab(self.graphs, "Graphs")
        self.tabs.setCurrentWidget(self.console)
        self.tabs.currentChanged.connect(self._tab_changed)
        self._qcmap_groups()
        root.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self.statusBar().showMessage("Starting…")

        self._monitor_presets: dict[str, Preset] = {}  # node id -> preset
        self._beacon_timer = QTimer(self, timeout=self.collect_beacons)
        self._tick = QTimer(self, interval=1000, timeout=self.monitor.tick)
        self._tick.start()
        self._presets_changed()
        self._fill_labs()
        self.discovery.start()

    # --- toolbar ------------------------------------------------------------

    def _toolbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("Toolbar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(14, 8, 14, 8)
        t = QLabel("LabHub")
        t.setObjectName("AppTitle")
        h.addWidget(t)
        h.addSpacing(18)
        h.addWidget(QLabel("Lab"))
        self.lab_box = QComboBox()
        self.lab_box.setMinimumWidth(240)
        self.lab_box.activated.connect(self._lab_chosen)
        h.addWidget(self.lab_box)
        for text, tip, fn in (("⟳ Refresh", "Re-read adb devices and the lab file", self.refresh),
                              ("✎ Mappings", "Edit devices, backbones and groups", self.edit_mappings),
                              ("Open lab file…", "Load a lab .yaml from anywhere", self._open_lab),
                              ("Exports", "Open the export folder", self._open_exports)):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            h.addWidget(b)
        h.addStretch(1)
        self.theme_btn = QPushButton()
        self.theme_btn.setToolTip("Switch between night (dark) and day (light) colours")
        self.theme_btn.clicked.connect(self.toggle_theme)
        self._theme_label()
        h.addWidget(self.theme_btn)
        self.lab_msg = QLabel()
        self.lab_msg.setStyleSheet("color: #ff6b7a;")
        h.addWidget(self.lab_msg)
        self.adb_label = QPushButton("● adb …")
        self.adb_label.setToolTip("Show what LabHub gets from adb, or choose adb.exe")
        self.adb_label.clicked.connect(self.show_adb_diagnostics)
        h.addWidget(self.adb_label)
        return bar

    def _theme_label(self) -> None:
        from .common import theme
        self.theme_btn.setText("☀ Day" if theme() == "night" else "☾ Night")

    def toggle_theme(self) -> None:
        from PySide6.QtWidgets import QApplication
        from .common import apply_theme, theme
        new = "day" if theme() == "night" else "night"
        apply_theme(QApplication.instance(), new)
        self.ctx.user.set("theme", new)
        self._theme_label()
        for w in QApplication.allWidgets():   # charts and status labels pick up the new colours
            w.update()

    def _fill_labs(self) -> None:
        self.lab_box.blockSignals(True)
        self.lab_box.clear()
        self.lab_box.addItem("From environment variables", ENV_LAB)
        for p in self.ctx.available_labs():
            self.lab_box.addItem(f"{p.stem}   ({p.parent})", str(p))
        last = self.ctx.user.get("last_lab")
        if last and self.lab_box.findData(last) < 0 and Path(last).exists():
            self.lab_box.addItem(f"{Path(last).stem}   ({Path(last).parent})", last)
        idx = self.lab_box.findData(last) if last else -1
        if idx < 0 and self.lab_box.count() > 1 and not last:
            idx = 0
        self.lab_box.setCurrentIndex(max(0, idx))
        self.lab_box.blockSignals(False)
        self._lab_chosen(self.lab_box.currentIndex())

    def _lab_chosen(self, _i: int) -> None:
        data = self.lab_box.currentData()
        self.ctx.load_lab(None if data == ENV_LAB else data)
        if data == ENV_LAB:
            self.ctx.user.set("last_lab", None)
        self.lab_msg.setText(f"⚠ {self.ctx.lab_error}" if self.ctx.lab_error else "")
        self.discovery.refresh()

    def _open_lab(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open lab file", "", "Lab files (*.yaml *.yml)")
        if path:
            dirs = self.ctx.user.get("lab_dirs", [])
            folder = str(Path(path).parent)
            if folder not in dirs:
                self.ctx.user.set("lab_dirs", dirs + [folder])
            self.ctx.user.set("last_lab", path)
            self._fill_labs()

    def _open_exports(self) -> None:
        self.ctx.export_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.ctx.export_dir)))

    def show_adb_diagnostics(self) -> None:
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QPlainTextEdit
        from ..core import config as cfg
        from ..transports import base as transport
        self.discovery.poll_once()
        lab = self.ctx.lab
        env = cfg.load_env()
        lines = [f"adb used:   {self.discovery.adb_path or 'NOT FOUND'}",
                 f"(override:  {transport.ADB_OVERRIDE or '-'})", "",
                 "--- raw output ---", self.discovery.last_output.strip() or "(none)", "",
                 "--- lab devices ---"]
        seen = {n.serial.lower() for n in self.ctx.registry.nodes() if n.kind == "unassigned"}
        adb_serials = {d.lower() for d in self.ctx.registry._adb}
        for d in lab.devices.values():
            ser = cfg.resolved_serial(d, env)
            state = "found" if ser.lower() in adb_serials else "NOT in adb list"
            lines.append(f"{d.name:6} serial={d.serial!r} -> {ser!r}   {state}")
        if seen:
            lines.append(f"unassigned: {', '.join(sorted(seen))}")
        dlg = QDialog(self)
        dlg.setWindowTitle("adb diagnostics")
        dlg.resize(760, 460)
        v = QVBoxLayout(dlg)
        t = QPlainTextEdit("\n".join(lines))
        t.setReadOnly(True)
        t.setStyleSheet("font-family: Consolas;")
        v.addWidget(t)
        box = QDialogButtonBox(QDialogButtonBox.Close)
        pick = box.addButton("Choose adb.exe…", QDialogButtonBox.ActionRole)
        reset = box.addButton("Auto-detect", QDialogButtonBox.ActionRole)

        def choose():
            path, _ = QFileDialog.getOpenFileName(dlg, "Choose adb.exe", "", "adb (adb.exe);;All files (*)")
            if path:
                transport.ADB_OVERRIDE = path
                self.ctx.user.set("adb_path", path)
                dlg.accept()
                self.show_adb_diagnostics()

        def auto():
            transport.ADB_OVERRIDE = None
            transport._found = None
            self.ctx.user.set("adb_path", None)
            dlg.accept()
            self.show_adb_diagnostics()

        pick.clicked.connect(choose)
        reset.clicked.connect(auto)
        box.rejected.connect(dlg.reject)
        v.addWidget(box)
        dlg.exec()

    def refresh(self) -> None:
        data = self.lab_box.currentData()
        self.ctx.load_lab(None if data == ENV_LAB else data)
        self.lab_msg.setText(f"⚠ {self.ctx.lab_error}" if self.ctx.lab_error else "")
        self.discovery.refresh()

    def edit_mappings(self, assign: str | None = None) -> None:
        seen = sorted({n.serial for n in self.ctx.registry.nodes() if n.serial and n.state.status.value != "disconnected"})
        dlg = MappingEditor(self, self.ctx.lab, seen, assign if isinstance(assign, str) else None)
        if dlg.exec() and dlg.result_lab:
            try:
                path = self.ctx.save_lab(dlg.result_lab, dlg.save_as)
            except OSError as e:
                QMessageBox.warning(self, "Save failed", f"Could not save the lab file:\n{e}")
                return
            self.statusBar().showMessage(f"Saved {path}", 6000)
            self._fill_labs()

    # --- events -------------------------------------------------------------

    def _on_event(self, e) -> None:
        if isinstance(e, ev.NodesChanged):
            self.strip.set_nodes(e.nodes)
            self.topology.set_nodes(e.nodes, self._topology_links())
            self.ap.set_target([n.name for n in self._ap_nodes(connected=False)])
            self.cmd.refresh_hint()
        elif isinstance(e, ev.LabLoaded):
            self.cmd.set_groups(list(e.lab.groups))
            self.presets.rebuild()
            self._presets_changed()
            self.statusBar().showMessage(f"Lab '{e.lab.name}' loaded"
                                         + (f" from {e.lab.path}" if e.lab.path else " from environment variables"), 6000)
        elif isinstance(e, ev.AdbStatus):
            color = STATUS_COLOR[ev_status(e.ok)]
            self.adb_label.setText(f"● {e.message}")
            self.adb_label.setStyleSheet(f"color: {color};")
        elif isinstance(e, ev.NodeStatusChanged):
            self.statusBar().showMessage(f"{datetime.now():%H:%M:%S}  {e.name}: {e.old} → {e.new}", 8000)
        elif isinstance(e, ev.LogMessage):
            self.statusBar().showMessage(e.text, 10000)
        elif isinstance(e, RunStarted):
            self.console.start_run(e.run)
        elif isinstance(e, JobOutput):
            self.console.output(e.job, e.text)
        elif isinstance(e, JobStateChanged):
            self.console.job_changed(e.job)
        elif isinstance(e, RunFinished):
            r = e.run
            counts: dict[str, int] = {}
            for j in r.jobs:
                counts[j.status] = counts.get(j.status, 0) + 1
            summary = ", ".join(f"{v} {k}" for k, v in counts.items())
            self.statusBar().showMessage(f"{datetime.now():%H:%M:%S}  '{r.label}' finished: {summary}", 10000)
            if r.preset_id == "__beacons__":
                self._ingest_beacons(r)
        elif isinstance(e, SequenceProgress):
            qkey = self._quick_seq.get(e.node_id)
            pane = self.console.shells.get(e.node_id)
            if qkey and pane:
                ok = {"done": True, "failed": False, "stopped": False}.get(e.state)
                pane.set_quick_state(qkey, ok, e.text + (f"\n{e.detail}" if e.detail else ""))
                if ok is not None:
                    self._quick_seq.pop(e.node_id, None)
                    self.statusBar().showMessage(f"QCMAP on {e.name}: {e.text}", 12000)
                return
            self.ap.set_qcmap(f"{e.name}: {e.text}", e.state)
            self.ap.qcmap_step(e.group, e.state, e.detail or e.text)
            if e.state == "pass":
                self.ap.log(True, f"QCMAP {e.text}  {e.detail}")
            elif e.state in ("done", "failed", "stopped"):
                self.ap.log(e.state == "done" if e.state != "stopped" else None,
                            f"QCMAP on {e.name}: {e.text}" + (f"  ({e.detail})" if e.detail else ""))
                self.statusBar().showMessage(f"QCMAP on {e.name}: {e.text}", 12000)
        elif isinstance(e, RebootProgress):
            ok = {"back": True, "failed": False}.get(e.state)
            self.ap.log(ok, f"Reboot {e.name}: {e.text}")
            self.statusBar().showMessage(f"Reboot {e.name}: {e.text}", 15000)
            if e.state in ("back", "failed"):
                if self._reopen_after_reboot.pop(e.node_id, False) and e.state == "back":
                    node = self.ctx.registry.get(e.node_id)
                    if node:
                        self._open_one_shell(node, show=False)
            self._ap_shell_state()
        elif isinstance(e, CheckResult):
            qkey = self._quick_checks.pop(e.node_id, None)
            if qkey and e.node_id in self.console.shells:
                self.console.shells[e.node_id].set_quick_state(qkey, e.ok, f"{'PASS' if e.ok else 'FAIL'}: {e.detail}")
                self.statusBar().showMessage(f"{e.label} on {e.name}: {'PASS' if e.ok else 'FAIL'} – {e.detail}", 12000)
                return
            if e.label.startswith("Short scan"):
                self.ap.set_scan_result(e.ok, e.detail)
            self.ap.log(e.ok, f"{e.label} on {e.name}: {e.detail}")
        elif isinstance(e, DumpListing):
            self.ap.dumps.set_listing(e)
            if e.new:
                self.statusBar().showMessage(f"⚠ New crash dump on {e.name}: {', '.join(e.new)}", 20000)
        elif isinstance(e, DumpAction):
            what = "Pulled" if e.action == "pull" else "Cleared"
            msg = f"{what} ramdump on {e.name}: {e.message}" if e.ok else f"{e.action} failed on {e.name}: {e.message}"
            self.statusBar().showMessage(msg + (f"  →  {e.dest}" if e.ok and e.dest else ""), 15000)
            if e.ok and e.dest:
                QDesktopServices.openUrl(QUrl.fromLocalFile(e.dest))
        elif isinstance(e, ShellOutput):
            if e.session.node_id.startswith(self.QWES_KEY.format("")):
                self._qwes_output(e.session, e.text)
            elif not self.stats.output(e.session, e.text, e.clear):
                self.console.shell_output(e.session, e.text, e.clear)
                if "[SUM" in e.text or e.session.node_id in self._iperf_carry:
                    self._iperf_lines(e.session, e.text)
        elif isinstance(e, ShellState):
            if e.session.node_id.startswith(self.QWES_KEY.format("")):
                if e.session.status == "closed" and self.ctx.shells.sessions.get(e.session.node_id) in (None, e.session):
                    self.ap.set_max_sta(None, f"QWES watch closed: {e.session.message or 'shell ended'}")
                return
            if self.stats.state(e.session):
                return
            self.console.shell_state(e.session)
            self._ap_shell_state()
            if e.session.status == "closed":   # a running quick check/QCMAP cannot finish
                pane = self.console.shells.get(e.session.node_id)
                for d in (self._quick_checks, self._quick_seq):
                    k = d.pop(e.session.node_id, None)
                    if k and pane:
                        pane.set_quick_state(k, False, "shell closed")
        elif isinstance(e, TopologyResult):
            self.topology.set_result(e.node_id, e.ifaces, e.error)
            left = [n for n in self._topology_pending if n != e.node_id]
            self._topology_pending = left
            self.topology.status.setText(f"reading interfaces… ({len(left)} left)" if left else
                                         f"Read at {datetime.now():%H:%M:%S}. Click an interface for its config.")
        elif isinstance(e, IfaceDetail):
            self.topology.set_detail(e.node_id, e.name, e.text)
        elif isinstance(e, BbAddrs):
            self._show_bb_addrs(e)
        elif isinstance(e, BbRoutes):
            dlg = self._routes.get(e.node_id)
            if dlg is not None:
                dlg.set_routes(e.family, e.routes, e.error, e.message)
        elif isinstance(e, BbExecDone):
            pane = self.console.shells.get(e.node_id)
            qc = quick.find(e.key)
            if pane and qc:
                # killall exits 1 when nothing was running: not worth a red button
                ok = e.code == 0 or "no process" in e.output.lower()
                pane.set_quick_state(e.key, ok, f"exit {e.code}: {e.output[:300]}" if e.output else f"exit {e.code}")
                pane.note(f"{qc.label}: exit {e.code}" + (f"\n{e.output}" if e.output else ""))
            QTimer.singleShot(1500, lambda nid=e.node_id: self.check_bb_addrs(nid))
        elif isinstance(e, lic.LicensePushed):
            self.ap.log(e.ok, f"License push to {e.name}: {e.text}")
            self.statusBar().showMessage(f"License push to {e.name}: {'OK' if e.ok else 'FAILED'} – {e.text}", 15000)
            if not e.ok:
                QMessageBox.warning(self, "Push license", f"License push to {e.name} failed:\n\n{e.text}")
        elif isinstance(e, BbPeerAddr):
            self._peer_ready(e)
        elif isinstance(e, SampleAdded):
            self._show_sample(e.node_id, e.sample)
        elif isinstance(e, PollerState):
            if e.key == "graphs" and e.error and e.node_id in self.graphs.cards:
                self.graphs.cards[e.node_id].set_status(e.error, "#ff6b7a")
            if e.key == "monitor" and e.error and e.node_id in self.monitor.cards:
                self.monitor.cards[e.node_id].set_error(e.error)
        elif isinstance(e, BeaconsUpdated):
            names = {n.id: n.name for n in self.ctx.registry.nodes()}
            self.beacons.set_items(self.ctx.beacons.all(), names)

    def _on_selection(self, ids: list[str]) -> None:
        self.ctx.selection = ids
        self.ctx.bus.publish(ev.SelectionChanged(ids))
        self.cmd.refresh_hint()
        # A shell per selected device, opened automatically.
        if self.cmd.auto_shells.isChecked():
            for nid in ids:
                node = self.ctx.registry.get(nid)
                if node and node.kind != "unassigned" and not self.ctx.shells.is_open(nid):
                    self._open_one_shell(node)

    def _build_preset_menu(self):
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        for cat in self.ctx.presets.categories():
            sub = menu.addMenu(cat)
            for p in self.ctx.presets.all():
                if p.category != cat:
                    continue
                choices = next((v for v in p.params.values() if v.choices), None)
                pname = next((k for k, v in p.params.items() if v.choices), None)
                if choices:
                    for label, value in choices.choices.items():
                        sub.addAction(f"{p.name}  [{label}]",
                                      lambda p=p, k=pname, v=value: self.run_preset(p, {k: v}, None))
                else:
                    sub.addAction(p.name, lambda p=p: self.run_preset(p, {}, None))
        menu.addSeparator()
        menu.addAction("+ New preset…", lambda: self.presets.edit_preset(None))
        edit = menu.addMenu("Edit preset")
        for p in self.ctx.presets.all():
            edit.addAction(p.name, lambda p=p: self.presets.edit_preset(p))
        return menu

    # --- running ------------------------------------------------------------

    def _targets(self, targets: list[str] | None = None):
        targets = targets or self.cmd.targets()
        try:
            nodes = self.ctx.resolve(targets)
        except TargetError as e:
            QMessageBox.warning(self, "Target", str(e))
            return None, targets
        if not nodes:
            self.statusBar().showMessage("No targets: select tiles at the top, or choose a target "
                                         "(All CPEs, All APs, …) in the command bar.", 8000)
            return None, targets
        return nodes, targets

    @staticmethod
    def _values(node, extra: dict) -> dict:
        digits = "".join(ch for ch in (node.config.id if node.config else node.id) if ch.isdigit())
        vals = {"name": node.name, "id": node.id, "n": digits, "serial": node.serial}
        # A backbone serving one device takes that device's number ({n} in dhclient CPE{n}.960).
        if node.kind == "backbone" and len(node.linked) == 1:
            vals["n"] = "".join(ch for ch in node.linked[0] if ch.isdigit())
        vals.update(extra)
        return vals

    def run_command(self, command: str, targets: list[str]) -> None:
        nodes, targets = self._targets(targets)
        if not nodes:
            return
        per_node = {n.id: expand(command, self._values(n, {})) for n in nodes}
        self._auto_open(nodes)
        rest = self._to_shells(nodes, per_node)
        if rest:
            self.ctx.runner.run(command, command, rest, targets, per_node)
        self.tabs.setCurrentWidget(self.console)

    def run_preset(self, preset: Preset, params: dict, delay_ms) -> None:
        nodes, targets = self._targets()
        if not nodes:
            return
        misfit = [n.name for n in nodes if not preset.fits(node_role(n))]
        if misfit and preset.roles:
            fit = [n for n in nodes if preset.fits(node_role(n))]
            msg = (f"'{preset.name}' is meant for {', '.join(preset.roles)}.\n"
                   f"Not suitable: {', '.join(misfit)}.")
            box = QMessageBox(QMessageBox.Question, "Preset targets", msg, parent=self)
            only = box.addButton(f"Run on the {len(fit)} suitable", QMessageBox.AcceptRole) if fit else None
            allb = box.addButton("Run on all", QMessageBox.DestructiveRole)
            box.addButton(QMessageBox.Cancel)
            box.exec()
            if only is not None and box.clickedButton() is only:
                nodes = fit
            elif box.clickedButton() is not allb:
                return
        label = preset.name + (" [" + ", ".join(f"{k}={v}" for k, v in params.items()) + "]" if params else "")
        if preset.kind == "monitor":
            self.start_monitor(preset, nodes)
            return
        if preset.kind == "sequence":
            self.start_sequence(preset, nodes, delay_ms)
            return
        else:
            per_node = {n.id: expand(preset.script, self._values(n, params)) for n in nodes}
            self._auto_open(nodes)
            rest = self._to_shells(nodes, per_node)
            if rest:
                self.ctx.runner.run(label, preset.script, rest, targets, per_node, preset.id, params)
        self.tabs.setCurrentWidget(self.console)

    def stop_everything(self) -> None:
        self.ctx.runner.stop_all()
        n = self.ctx.shells.interrupt_all()
        for ap in self._ap_nodes(connected=False):   # Ctrl+C also stopped the QWES tail: start it again
            self.ctx.shells.close(self.QWES_KEY.format(ap.id))
        QTimer.singleShot(1500, self._qwes_tick)
        self.stop_all_stats()
        self.stop_all_graphs()
        self.statusBar().showMessage("Stopping all running commands…" + (f" (Ctrl+C sent to {n} shell(s))" if n else ""), 4000)

    # --- shells -------------------------------------------------------------

    def open_shells(self, targets: list[str]) -> None:
        nodes, _ = self._targets(targets)
        if not nodes:
            return
        errors = [e for e in (self._open_one_shell(n) for n in nodes) if e]
        opened = len(nodes) - len(errors)
        msg = f"{opened} shell(s) open. Run / presets now go into them."
        if errors:
            msg += "   Not opened: " + "; ".join(errors)
        self.statusBar().showMessage(msg, 12000)

    def _quick_role(self, node) -> str:
        """'ap' / 'cpe' for devices; 'ap-bb' / 'cpe-bb' for a backbone, from the device it serves."""
        role = node_role(node)
        if role != "backbone":
            return role
        dev = self._bb_device(node)
        return f"{node_role(dev)}-bb" if dev is not None else "cpe-bb"

    def _bb_device(self, node):
        """The device a backbone serves (the first, if several)."""
        for did in node.linked:
            dev = self.ctx.registry.get(did)
            if dev is not None and isinstance(dev.config, DeviceConfig):
                return dev
        return None

    def _quick_text(self, node, qc, over=None) -> str:
        """The command of a quick button for this node, with {ctrl} {data} {ifaces} filled in."""
        text = quick.text_of(qc, self._quick_overrides() if over is None else over)
        if node_role(node) == "backbone":
            dev = self._bb_device(node)
            if dev is not None:
                ifaces = dev.config.backbone.interfaces if dev.config.backbone else []
                text = expand(text, quick.bb_values(dev.name, ifaces, self._bb_actual.get(node.id), node_role(dev)))
        return text

    # --- backbones: IPv6 addresses and routes -----------------------------------

    def _bb_ifaces(self, node) -> list[str]:
        """Control and data interface of every device the BB serves."""
        out = []
        for did in node.linked:
            dev = self.ctx.registry.get(did)
            if dev is not None and isinstance(dev.config, DeviceConfig):
                ifaces = dev.config.backbone.interfaces if dev.config.backbone else []
                out += quick.bb_interfaces(dev.name, ifaces, node_role(dev))
        return out

    def check_bb_addrs(self, node_id: str) -> None:
        node = self.ctx.registry.get(node_id)
        if node is not None and node.kind == "backbone" and node_id in self.console.shells:
            self.ctx.bbnet.check_addrs(node, self._bb_ifaces(node))

    def _check_all_bb_addrs(self) -> None:
        for nid in list(self.console.shells):
            self.check_bb_addrs(nid)

    def _show_bb_addrs(self, e: BbAddrs) -> None:
        from html import escape
        pane = self.console.shells.get(e.node_id)
        if pane is None:
            return
        if e.error:
            pane.set_addrs(f"<span style='color:#ff6b7a'>IPv6: cannot check – {escape(e.error)}</span>")
            return
        self._bb_actual[e.node_id] = {k: ia.name for k, ia in e.found.items()}
        parts, tips = [], []
        for name in e.ifaces:
            ia = lookup(e.found, name)
            if ia.global_addrs:
                parts.append(f"<span style='color:#3ccf7a'>● {escape(ia.name)}  "
                             f"{escape(', '.join(ia.global_addrs))}</span>")
            else:
                why = "no IPv6 address" if ia.present else "interface not found"
                parts.append(f"<span style='color:#ff6b7a'>✖ {escape(ia.name)}  {why}</span>")
            tips.append(f"{ia.name}: " + (", ".join(ia.global_addrs + ia.link_local) or
                                          ("no IPv6 address" if ia.present else "not on this backbone")))
        pane.set_addrs("&nbsp;&nbsp;&nbsp;".join(parts) or "<span style='color:#8a90a0'>IPv6: no interfaces</span>",
                       "\n".join(tips) + "\n\nInterfaces from Mappings → BB interfaces "
                       "(empty = <name>.960 + <name>.1037 for the AP, <name>.1015 for a CPE). Checked every 20 s.")
        node = self.ctx.registry.get(e.node_id)
        if node is not None:
            pane.set_quick_tips(self._quick_tips(node))
        dlg = self._routes.get(e.node_id)
        if dlg is not None:
            dlg.set_ifaces(self._route_ifaces(e.node_id))

    def _route_ifaces(self, node_id: str) -> list[str]:
        """Choices for "through iface": the device interfaces first, then the rest of the BB's."""
        node = self.ctx.registry.get(node_id)
        actual = self._bb_actual.get(node_id, {})
        ifaces = [actual.get(i.lower(), i) for i in self._bb_ifaces(node)] if node else []
        return ifaces + sorted(set(actual.values()) - set(ifaces) - {"lo"})

    def show_routes(self, node) -> None:
        dlg = self._routes.get(node.id)
        if dlg is None:
            dlg = RoutesDialog(self, node.id, node.name, self._route_ifaces(node.id))
            dlg.request.connect(self._route_request)
            self._routes[node.id] = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        dlg.refresh()

    def _route_request(self, node_id: str, family: str, action: str, args: list) -> None:
        node = self.ctx.registry.get(node_id)
        if node is not None:
            self.ctx.bbnet.routes(node, family, action, args)

    def _quick_tips(self, node, over=None) -> dict:
        return {c.key: self._quick_text(node, c, over) for c in quick.for_role(self._quick_role(node))}

    def _open_one_shell(self, node, show: bool = True) -> str | None:
        err = self.ctx.shells.open(node)
        s = self.ctx.shells.sessions.get(node.id)
        if s is not None:
            cmds = quick.for_role(self._quick_role(node))
            is_bb = node.kind == "backbone"
            known = node.id in self.console.shells
            pane = self.console.add_shell(s, node.name, cmds, addr_bar=is_bb)
            pane.set_quick_tips(self._quick_tips(node))
            if is_bb and not known:
                self.check_bb_addrs(node.id)
            if show:
                self.tabs.setCurrentWidget(self.console)
            self._ap_shell_state()
            self._sync_stats()
        return err

    def _shell_rows(self, ids: list[str]) -> list[list[str]]:
        """Console rows in display order (AP first):  device | its BB.
        The BB only goes next to a device when its shell is open (picked)."""
        nodes = [n for n in self.ctx.registry.nodes() if n.id in ids]
        rows, used = [], set()
        for n in nodes:
            if n.kind == "backbone" or n.id in used:
                continue
            row = [n.id]
            bb = n.linked[0] if n.linked else None
            if bb in ids and bb not in used:
                row.append(bb)
                used.add(bb)
            used.add(n.id)
            rows.append(row)
        rows += [[n.id] for n in nodes if n.id not in used]
        return rows

    # --- ping / iperf between backbones ---------------------------------------

    def _peers(self, node) -> list:
        """Devices on the other side of a BB's link: CPEs for the AP's BB, APs for a CPE's BB."""
        dev = self._bb_device(node)
        want = "cpe" if dev is not None and node_role(dev) == "ap" else "ap"
        return [n for n in self.ctx.registry.nodes() if isinstance(n.config, DeviceConfig)
                and n.config.role == want and n.config.backbone and n.config.backbone.backbone_id]

    def _run_peer(self, node, qc, pane) -> None:
        from PySide6.QtWidgets import QInputDialog
        peers = self._peers(node)
        if not peers:
            pane.set_quick_state(qc.key, False, "no device with a BB on the other side (see Mappings)")
            return
        peer = peers[0]
        if len(peers) > 1:
            name, ok = QInputDialog.getItem(self, qc.label, f"{qc.label} – which device?",
                                            [p.name for p in peers], 0, False)
            if not ok:
                return
            peer = next(p for p in peers if p.name == name)
        bb = self.ctx.registry.get(peer.config.backbone.backbone_id)
        if bb is None:
            pane.set_quick_state(qc.key, False, f"{peer.name}'s BB is not in the lab")
            return
        iface = quick.bb_values(peer.name, peer.config.backbone.interfaces, self._bb_actual.get(bb.id),
                                node_role(peer))["data"]
        pane.set_quick_state(qc.key, None, f"reading {peer.name}'s IPv6: ifconfig {iface} on {bb.name}…")
        self.ctx.bbnet.peer_addr(node, qc.key, bb, peer.name, iface)

    def _peer_ready(self, e: BbPeerAddr) -> None:
        node = self.ctx.registry.get(e.node_id)
        pane = self.console.shells.get(e.node_id)
        qc = quick.find(e.key)
        if node is None or pane is None or qc is None:
            return
        if e.error:
            pane.set_quick_state(e.key, False, f"{e.peer} {e.iface}: {e.error}")
            pane.note(f"{qc.label}: no IPv6 address for {e.peer} ({e.iface}): {e.error}")
            return
        addr = e.addr
        if e.link_local:   # fe80:: needs the interface it is reached through, on this BB
            addr += "%" + self._bb_data_iface(node)
        text = expand(self._quick_text(node, qc), {"peer": addr})
        pane.note(f"{e.peer} ({e.iface}) = {e.addr}" + (" (link-local)" if e.link_local else ""))
        err = self._open_one_shell(node, show=False)
        s = self.ctx.shells.sessions.get(node.id)
        if s is None or s.status == "closed":
            pane.set_quick_state(e.key, False, err or "shell not available")
            return
        self._shell_write(node.id, text)
        pane.set_quick_state(e.key, True, f"sent: {text}")

    def _bb_data_iface(self, node) -> str:
        """Data interface of the device this BB serves, as the BB spells it."""
        dev = self._bb_device(node)
        if dev is None:
            return ""
        ifaces = dev.config.backbone.interfaces if dev.config.backbone else []
        return quick.bb_values(dev.name, ifaces, self._bb_actual.get(node.id), node_role(dev))["data"]

    # --- quick commands (buttons on each AP / CPE shell) ---------------------

    def _quick_overrides(self) -> dict:
        return self.ctx.user.get("quick_commands") or {}

    def edit_quick(self, key: str) -> None:
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QPlainTextEdit
        qc = quick.find(key)
        if qc is None or qc.kind == "ui":
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Edit '{qc.label}'")
        dlg.resize(760, 300)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel({"check": "Typed into the shell; FAIL if an error line is printed.",
                            "qcmap": "QCMAP_CLI key groups, e.g. 1-2-1 4-29-1 4-1-1. "
                                     "Each group must print a success word.",
                            "send": "Typed into the shell as it is.",
                            "exec": "Run over a second connection (works while the shell is busy).",
                            "peer": "Typed into the shell; {peer} = IPv6 address of the other side's data "
                                    "interface, read with ifconfig on its BB (AP BB → a CPE, CPE BB → the AP)."
                            }[qc.kind]))
        if "{" in qc.default:
            v.addWidget(QLabel("{ctrl} = control interface (.960), {data} = data interface (AP .1037, CPE .1015), "
                               "{ifaces} = both – per device, from Mappings → BB interfaces "
                               "(empty there = <name>.960 / <name>.1037 or .1015). Typing a fixed name instead "
                               "changes it for every backbone."))
        t = QPlainTextEdit(quick.text_of(qc, self._quick_overrides()))
        t.setStyleSheet("font-family: Consolas;")
        v.addWidget(t, 1)
        box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        reset = box.addButton("Reset to default", QDialogButtonBox.ResetRole)
        reset.clicked.connect(lambda: t.setPlainText(qc.default))
        box.accepted.connect(dlg.accept)
        box.rejected.connect(dlg.reject)
        v.addWidget(box)
        if not dlg.exec():
            return
        text = t.toPlainText().strip()
        if qc.kind == "qcmap" and not quick.parse_groups(text):
            QMessageBox.warning(self, "QCMAP", "Enter key groups like 1-2-1 4-29-1 4-1-1.")
            return
        over = dict(self._quick_overrides())
        if text and text != qc.default:
            over[key] = text
        else:
            over.pop(key, None)
        self.ctx.user.set("quick_commands", over)
        for nid, pane in self.console.shells.items():
            node = self.ctx.registry.get(nid)
            if node:
                pane.set_quick_tips(self._quick_tips(node, over))
        self.statusBar().showMessage(f"'{qc.label}' saved" + ("" if key in over else " (default)"), 6000)

    def run_quick(self, node_id: str, key: str) -> None:
        qc = quick.find(key)
        node = self.ctx.registry.get(node_id)
        pane = self.console.shells.get(node_id)
        if qc is None or node is None or pane is None:
            return
        if qc.kind == "ui":
            self.show_routes(node)
            return
        text = self._quick_text(node, qc)
        if qc.kind == "exec":
            pane.set_quick_state(key, None, f"running: {text}")
            self.ctx.bbnet.exec_quick(node, key, text)
            return
        if qc.kind == "peer":
            self._run_peer(node, qc, pane)
            return
        err = self._open_one_shell(node, show=False)
        s = self.ctx.shells.sessions.get(node_id)
        if s is None or s.status == "closed":
            pane.set_quick_state(key, False, err or "shell not available")
            return
        if qc.kind in ("send", "check") and s.in_menu():
            # still inside QCMAP_CLI ("Option >"): typing there goes to the menu, not the shell.
            # Send it to the background (suspend, `fg` brings it back), then run the command.
            import threading
            pane.set_quick_state(key, None, "sending QCMAP_CLI to the background first…")

            def leave_then_run():
                how = self.ctx.shells.leave_menu(node_id, kill=False)
                if how == "still in menu":
                    s.note(f"{qc.label} not sent: QCMAP_CLI is still in the foreground")
                    return
                QTimer.singleShot(0, lambda: self.run_quick(node_id, key))

            threading.Thread(target=leave_then_run, daemon=True).start()
            return
        if qc.kind == "send":
            self._shell_write(node_id, text)
            pane.set_quick_state(key, True, f"sent: {text[:200]}")
            if node.kind == "backbone":   # dhclient / dhcpd: the address shows up a little later
                for ms in (4000, 12000):
                    QTimer.singleShot(ms, lambda: self.check_bb_addrs(node_id))
            return
        if qc.kind == "check":
            if node_id in self._quick_checks:
                self.statusBar().showMessage(f"{node.name}: a check is still running.", 6000)
                return
            self._quick_checks[node_id] = key
            pane.set_quick_state(key, None, f"running: {text}")
            CommandCheck(s, text, self.ctx.bus, f"{qc.label}")
            return
        # qcmap
        old = self._sequences.get(node_id)
        if old and old.active:
            self.statusBar().showMessage(f"{node.name}: QCMAP is already running (^C to stop).", 8000)
            return
        base = self.ctx.presets.get("ap-bridge-mode" if node_role(node) == "ap" else "cpe-bridge-mode")
        groups = quick.parse_groups(text)
        if not groups:
            pane.set_quick_state(key, False, "no key groups – right-click to edit")
            return
        spec = base.sequence if base and base.sequence else SequenceSpec("QCMAP_CLI", [])
        spec = dataclasses.replace(spec, groups=groups, steps=[k for g in groups for k in g])
        words = self.ctx.user.get("qcmap_success_words") or DEFAULT_SUCCESS_WORDS
        self._quick_seq[node_id] = key
        pane.set_quick_state(key, None, f"QCMAP_CLI {text}: running")
        seq = MenuSequence(s, spec, self.ctx.bus, f"{qc.label} ({text})", success=words_regex(words))
        self._sequences[node_id] = seq
        seq.start()

    def _shell_write(self, node_id: str, text: str) -> bool:
        lines = text.replace("\r\n", "\n").split("\n")
        return self.ctx.shells.write(node_id, "\n".join(lines) + "\n")

    def _auto_open(self, nodes) -> None:
        if self.cmd.auto_shells.isChecked():
            for n in nodes:
                if not self.ctx.shells.is_open(n.id) and n.id not in self.ctx.shells.sessions:
                    self._open_one_shell(n)

    def _to_shells(self, nodes, per_node: dict) -> list:
        """Send to nodes with an open shell; return the nodes that have none."""
        rest, sent = [], []
        for n in nodes:
            if self.ctx.shells.is_open(n.id) and self._shell_write(n.id, per_node[n.id]):
                sent.append(n.name)
            else:
                rest.append(n)
        if sent:
            self.statusBar().showMessage(f"Typed into shell(s): {', '.join(sent)}"
                                         + (f"   ran as new job on: {', '.join(n.name for n in rest)}" if rest else ""),
                                         8000)
        return rest

    def close_shell(self, node_id: str) -> None:
        self.ctx.shells.close(node_id)
        self.console.remove_shell(node_id)
        self._close_stats(node_id)

    def close_all_shells(self) -> None:
        for nid in list(self.console.shells):
            self.close_shell(nid)

    # --- Stats tab: a second shell per AP / CPE open in the Console -----------

    STATS_KEY = "stats:{}"

    def _stats_devices(self) -> list:
        """AP / CPE nodes with a Console shell, in display order (AP first)."""
        out = []
        for n in self.ctx.registry.nodes():
            if n.id in self.console.shells and isinstance(n.config, DeviceConfig) \
                    and node_role(n) in quick.STATS:
                out.append(n)
        return out

    def _sync_stats(self) -> None:
        """Stats tab = the AP / CPE shells of the Console. New ones start their watch."""
        if not hasattr(self, "graphs"):
            return
        want = {n.id: n for n in self._stats_devices()}
        for did in [d for d in self.stats.panes if d not in want]:
            self._close_stats(did)
        for did in [d for d in self.graphs.cards if d not in want]:
            self._close_graphs(did)
        for did, n in want.items():
            if did not in self.stats.panes and self.ctx.shells.is_open(did):
                self.restart_stats(did)
            if did not in self.graphs.cards and self.ctx.shells.is_open(did):
                self.restart_graphs(did)
        self.stats.relayout(list(want))
        self.graphs.relayout(list(want))

    def restart_stats(self, device_id: str) -> None:
        node = self.ctx.registry.get(device_id)
        if node is None or node_role(node) not in quick.STATS:
            return
        key = self.STATS_KEY.format(device_id)
        self.ctx.shells.close(key)
        err = self.ctx.shells.open(node, key, f"{node.name} stats")
        s = self.ctx.shells.sessions.get(key)
        if s is None:
            self.statusBar().showMessage(f"Stats not started: {err}", 8000)
            return
        pane = self.stats.add(device_id, s, f"{node.name}  ·  stats")
        pane.text.clear()
        cmd = quick.text_of(quick.STATS[node_role(node)], self._quick_overrides())
        s.write(cmd + "\n")          # queued until the shell is up (after adb root)
        self.stats.relayout([n.id for n in self._stats_devices()])

    def _close_stats(self, device_id: str, graphs: bool = True) -> None:
        if graphs:
            self._close_graphs(device_id)
        key = self.STATS_KEY.format(device_id)
        if key in self.ctx.shells.sessions:
            self.ctx.shells.interrupt(key)
            self.ctx.shells.close(key)
        self.stats.remove(device_id)
        self.stats.relayout([n.id for n in self._stats_devices()])

    def stop_all_stats(self) -> None:
        for did in list(self.stats.panes):
            self._close_stats(did, graphs=False)

    # --- Graphs tab: the same wifistats values, parsed and drawn -------------

    def _iperf_lines(self, session, text: str) -> None:
        """iperf [SUM] lines typed out in a BB shell -> throughput / PER graph of the
        device that BB serves (or of the device itself, if iperf runs there)."""
        buf = self._iperf_carry.pop(session.node_id, "") + text
        lines = buf.split("\n")
        if lines[-1] and "[SUM" in lines[-1]:
            self._iperf_carry[session.node_id] = lines[-1][-400:]
        node = self.ctx.registry.get(session.node_id)
        if node is None:
            return
        dev = self._bb_device(node) if node.kind == "backbone" else node
        card = self.graphs.cards.get(dev.id) if dev is not None else None
        if card is None:
            return
        for line in lines[:-1]:
            s = parsers.parse_iperf_sum(line)
            if s and not s.get("final"):
                card.add_iperf(s, node.name)

    def restart_graphs(self, device_id: str) -> None:
        node = self.ctx.registry.get(device_id)
        role = node_role(node) if node else ""
        preset = self.ctx.presets.get(f"{role}-stats")
        if node is None or role not in quick.STATS or preset is None or preset.monitor is None:
            return
        parser = parsers.PARSERS.get(preset.monitor.parser)
        card = self.graphs.card(device_id, node.name, role)
        self.graphs.relayout([n.id for n in self._stats_devices()])

        def on_output(n, text, parser=parser):
            try:
                metrics = parser(text)
            except Exception as e:  # a parser bug must not kill the poller
                metrics = {"parse_error": str(e)}
            self._enrich(n, metrics)
            self.ctx.samples.add(Sample(n.id, datetime.now(), metrics, text))

        err = self.ctx.pollers.start("graphs", node, expand(preset.monitor.command, self._values(node, {})),
                                     preset.monitor.interval_s, on_output)
        card.set_status(f"not started: {err}" if err else "starting…", "#ff6b7a" if err else "#8a90a0")

    def _close_graphs(self, device_id: str) -> None:
        self.ctx.pollers.stop("graphs", device_id)
        self.graphs.remove(device_id)
        self.graphs.relayout([n.id for n in self._stats_devices()])

    def stop_all_graphs(self) -> None:
        self.ctx.pollers.stop("graphs")
        for card in self.graphs.cards.values():
            card.set_status("stopped")

    def reconnect_shell(self, node_id: str) -> None:
        self.ctx.shells.close(node_id)
        node = self.ctx.registry.get(node_id)
        if node is None:
            return
        err = self.ctx.shells.open(node)
        s = self.ctx.shells.sessions.get(node_id)
        if s is not None:
            self.console.add_shell(s)
        if err:
            self.statusBar().showMessage(f"Reconnect failed: {err}", 8000)

    def export_last_run(self) -> None:
        run = self.console.last_run
        if not run:
            self.statusBar().showMessage("Nothing to export yet.", 4000)
            return
        try:
            out = self.ctx.runner.export(run, self.ctx.export_dir)
        except OSError as e:
            QMessageBox.warning(self, "Export failed", str(e))
            return
        self.statusBar().showMessage(f"Exported to {out}", 8000)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(out)))

    # --- monitor ------------------------------------------------------------

    def _presets_changed(self) -> None:
        self.monitor.set_presets([p for p in self.ctx.presets.all() if p.kind == "monitor"])

    def start_monitor_id(self, preset_id: str) -> None:
        p = self.ctx.presets.get(preset_id)
        if p:
            self.run_preset(p, {}, None)

    def start_monitor(self, preset: Preset, nodes) -> None:
        th = self.ctx.lab.features.get("monitor", {}) if isinstance(self.ctx.lab.features, dict) else {}
        parser = parsers.PARSERS.get(preset.monitor.parser)
        if parser is None:
            QMessageBox.warning(self, "Monitor", f"Unknown parser '{preset.monitor.parser}'")
            return
        errors = []
        for n in nodes:
            role = node_role(n)
            card = self.monitor.card(n.id, n.name, "ap" if preset.monitor.parser.endswith(".ap") else role, th)
            self._monitor_presets[n.id] = preset

            def on_output(node, text, parser=parser):
                try:
                    metrics = parser(text)
                except Exception as e:  # parser bug must not kill the poller
                    metrics = {"parse_error": str(e)}
                self._enrich(node, metrics)
                self.ctx.samples.add(Sample(node.id, datetime.now(), metrics, text))

            err = self.ctx.pollers.start("monitor", n, expand(preset.monitor.command, self._values(n, {})),
                                         float(th.get("interval_s", preset.monitor.interval_s)), on_output)
            if err:
                errors.append(err)
                card.set_error(err)
        if errors:
            self.statusBar().showMessage("Not started: " + "; ".join(errors), 10000)

    def _enrich(self, node, metrics: dict) -> None:
        """Name peers and remember MACs (used to label beacons)."""
        reg = self.ctx.registry
        ifc = metrics.get("ifconfig") or {}
        if ifc.get("mac"):
            reg.learn(node.id, macs=[ifc["mac"]])
        link = metrics.get("link") or {}
        if link.get("bssid"):
            peer = reg.device_by_mac(link["bssid"])
            if peer:
                link["peer_name"] = peer.name
        for sts in (metrics.get("stations") or {}).values():
            for st in sts:
                peer = reg.device_by_mac(st["mac"])
                if peer:
                    st["name"] = peer.name

    def _show_sample(self, node_id: str, s: Sample) -> None:
        if node_id in self.ctx.pollers.running("graphs") and node_id in self.graphs.cards:
            self.graphs.cards[node_id].add_sample(s.metrics)
            return
        if node_id in self.ctx.pollers.running("apstats"):
            self.ap.stats.update_sample(s)
            return
        card = self.monitor.cards.get(node_id)
        if card:
            card.update_sample(s, self.ctx.samples.history(node_id, "rssi_dbm"))

    def stop_monitor(self, node_id) -> None:
        self.ctx.pollers.stop("monitor", node_id)
        if node_id is None:
            for nid in list(self.monitor.cards):
                self.monitor.remove(nid)
        else:
            self.monitor.remove(node_id)

    def export_samples(self, node_id: str) -> None:
        node = self.ctx.registry.get(node_id)
        path = self.monitor.ask_csv(node.name if node else node_id)
        if path:
            n = self.ctx.samples.export_csv(node_id, path)
            self.statusBar().showMessage(f"Exported {n} samples to {path}", 8000)

    # --- beacons ------------------------------------------------------------

    def collect_beacons(self) -> None:
        preset = self.ctx.presets.get("scan-results")
        nodes, targets = self._targets()
        if not nodes or preset is None:
            return
        nodes = [n for n in nodes if n.kind == "device"]
        if not nodes:
            return
        per_node = {n.id: expand(preset.script, self._values(n, {})) for n in nodes}
        self.ctx.runner.run("Collect scan results", preset.script, nodes, targets, per_node, "__beacons__")

    def _ingest_beacons(self, run) -> None:
        reg = self.ctx.registry
        for job in run.jobs:
            if job.status != "done":
                continue
            items = []
            for r in parsers.parse_iw_scan(job.output.text()):
                peer = reg.device_by_mac(r["bssid"])
                dtype = "?"
                if peer and isinstance(peer.config, DeviceConfig):
                    dtype = peer.config.role.upper()
                items.append(BeaconObservation(
                    job.node_id, job.ended or datetime.now(), r["bssid"], r.get("ssid", ""),
                    str(r.get("channel", "")), r.get("freq_mhz"), r.get("rssi_dbm"),
                    peer.id if peer else None, dtype, bool(r.get("associated"))))
            self.ctx.beacons.replace(job.node_id, items)

    def _beacon_auto(self, on: bool, interval_s: int) -> None:
        if on:
            self._beacon_timer.start(interval_s * 1000)
            self.collect_beacons()
        else:
            self._beacon_timer.stop()

    # --- close --------------------------------------------------------------

    # --- AP workflow --------------------------------------------------------

    # --- AP license (QWES max_sta) ---------------------------------------------

    QWES_KEY = "qwes:{}"

    def _qwes_tick(self) -> None:
        """Keep a hidden shell on the AP running the QWES tail; reopened after a reboot / disconnect."""
        aps = self._ap_nodes(connected=False)
        ap = aps[0] if aps else None
        if ap is None or ap.state.status.value != "connected" or self.ctx.reboots.is_rebooting(ap.id):
            self.ap.set_max_sta(None, "AP not connected" if ap else "no AP in the lab")
            return
        key = self.QWES_KEY.format(ap.id)
        if self.ctx.shells.is_open(key):
            return
        self.ctx.shells.close(key)
        self._qwes_carry.pop(key, None)
        if self.ctx.shells.open(ap, key, f"{ap.name} QWES") is None:
            self.ctx.shells.sessions[key].write(lic.WATCH_CMD + "\n")   # queued until adb root is done

    def _qwes_output(self, session, text: str) -> None:
        buf = self._qwes_carry.pop(session.node_id, "") + text
        lines = buf.split("\n")
        self._qwes_carry[session.node_id] = lines[-1][-400:]
        value = lic.last_max_sta("\n".join(lines[:-1]))
        if value is not None:
            last = next(l for l in reversed(lines[:-1]) if "max_sta=" in l).strip()
            self.ap.set_max_sta(value, f"{last}\n\nfrom: {lic.WATCH_CMD}")

    def push_license(self) -> None:
        aps = self._need_ap()
        if not aps:
            return
        ap = aps[0]
        local, dest = lic.settings(self.ctx.lab.features)
        if QMessageBox.question(self, "Push license", f"Push the license to {ap.name}?\n\n{local}\n→ {dest}/") \
                != QMessageBox.Yes:
            return
        self.ap.log(None, f"License push to {ap.name}: adb push … {dest}/")
        self.ctx.license.push(ap.id, ap.name, self.ctx.registry.serial_of(ap), local, dest)

    def _ap_nodes(self, connected: bool = True):
        nodes = [n for n in self.ctx.registry.nodes()
                 if isinstance(n.config, DeviceConfig) and n.config.role == "ap"]
        if connected:
            nodes = [n for n in nodes if n.state.status.value == "connected"]
        return nodes

    def _need_ap(self):
        nodes = self._ap_nodes()
        if not nodes:
            QMessageBox.information(self, "AP", "No connected AP. Check the AP tile (adb) or Mappings.")
        return nodes

    def ap_scan(self, short: bool, value: str) -> None:
        nodes = self._need_ap()
        if not nodes:
            return
        if not short:
            self.statusBar().showMessage("Long scan selected: no command sent to the AP.", 8000)
            self.ap.set_scan_result(True, "long scan: no command sent")
            self.ap.log(None, "Long scan selected: no command sent")
            for n in nodes:
                if self.ctx.shells.is_open(n.id):
                    self.ctx.shells.sessions[n.id].note("long scan: no scan command sent")
            return
        if not value:
            QMessageBox.warning(self, "Scan", "Enter the hw_ver_override value (e.g. 2).")
            return
        preset = self.ctx.presets.get("ap-short-scan")
        if not preset:
            return
        for n in nodes:
            err = self._open_one_shell(n, show=False)
            s = self.ctx.shells.sessions.get(n.id)
            if s is None:
                self.ap.set_scan_result(False, err or "shell not available")
                continue
            cmd = expand(preset.script, self._values(n, {"value": value}))
            self.ap.set_scan_result(None, f"sending: {cmd}")
            CommandCheck(s, cmd, self.ctx.bus, f"Short scan (hw_ver_override {value})")

    def _run_on(self, preset: Preset, nodes, params: dict) -> None:
        per_node = {n.id: expand(preset.script, self._values(n, params)) for n in nodes}
        self._auto_open(nodes)
        rest = self._to_shells(nodes, per_node)
        if rest:
            label = preset.name + (" [" + ", ".join(f"{k}={v}" for k, v in params.items()) + "]" if params else "")
            self.ctx.runner.run(label, preset.script, rest, [n.id for n in rest], per_node, preset.id, params)

    def _reboot_targets(self, targets: list[str]) -> None:
        try:
            nodes = self.ctx.resolve(targets or self.cmd.targets())
        except TargetError as e:
            QMessageBox.warning(self, "Target", str(e))
            return
        self.reboot(nodes)   # no target -> the devices whose shells are open

    def _reboot_devices(self, nodes) -> list:
        """Devices to reboot: a BB tile stands for the device(s) it serves (BBs themselves are never
        rebooted); unmapped adb devices count too. Nothing device-like -> the devices with an open shell."""
        out: dict = {}
        for n in nodes or []:
            if n.kind == "backbone":
                for did in n.linked:
                    d = self.ctx.registry.get(did)
                    if d is not None and d.kind == "device":
                        out.setdefault(d.id, d)
            elif n.kind in ("device", "unassigned"):
                out.setdefault(n.id, n)
        if not out:
            for nid in self.console.shells:
                d = self.ctx.registry.get(nid)
                if d is not None and d.kind in ("device", "unassigned"):
                    out.setdefault(d.id, d)
        return list(out.values())

    def reboot(self, nodes) -> None:
        nodes = self._reboot_devices(nodes)
        down = [n.name for n in nodes if n.state.status.value != "connected"]
        busy = [n.name for n in nodes if self.ctx.reboots.is_rebooting(n.id)]
        nodes = [n for n in nodes if n.state.status.value == "connected"
                 and not self.ctx.reboots.is_rebooting(n.id)]
        if not nodes:
            why = ([f"{', '.join(down)} not connected"] if down else []) + \
                  ([f"{', '.join(busy)} already rebooting"] if busy else [])
            self.statusBar().showMessage("Nothing to reboot" + (f" ({'; '.join(why)})" if why else
                                         ": select device tiles or open their shells in the Console") + ".", 8000)
            return
        names = ", ".join(n.name for n in nodes)
        extra = f"\n\nNot connected (skipped): {', '.join(down)}" if down else ""
        if QMessageBox.question(self, "Reboot", f"Reboot {names}?\n\nA running menu (QCMAP_CLI) is left with "
                                f"Ctrl+C / Ctrl+Z first, then 'reboot' is typed.{extra}") != QMessageBox.Yes:
            return
        for n in nodes:
            seq = self._sequences.get(n.id)
            if seq and seq.active:
                seq.stop()
            if n.id in self.ctx.pollers.running("apstats"):
                self.ap.stats.set_status("Stopped (device rebooting).")
            self.ctx.pollers.stop("apstats", n.id)
            self.ctx.pollers.stop("monitor", n.id)
            self._close_stats(n.id)
            self._reopen_after_reboot[n.id] = self.ctx.shells.is_open(n.id)
            self.ap.log(None, f"Reboot {n.name}: requested")
            self.ctx.reboots.reboot(n)
        self._ap_shell_state()

    def _qcmap_groups(self) -> None:
        p = self.ctx.presets.get(self.ap.mode.currentData() or "")
        if p and p.sequence:
            self.ap.set_qcmap_groups(p.sequence.groups or [[k] for k in p.sequence.steps])

    def _ap_shell_state(self) -> None:
        aps = self._ap_nodes(connected=False)
        if aps and self.ctx.reboots.is_rebooting(aps[0].id):
            self.ap.set_shell("rebooting")
            return
        s = self.ctx.shells.sessions.get(aps[0].id) if aps else None
        self.ap.set_shell(s.status if s else "", s.message if s else "")

    def _show_ap_shell(self) -> None:
        for n in self._need_ap():
            self._open_one_shell(n, show=False)
        self.tabs.setCurrentWidget(self.console)

    def _tab_changed(self, _i: int) -> None:
        if self.tabs.currentWidget() is self.stats:
            self._sync_stats()
        elif self.tabs.currentWidget() is self.topology and not self.topology.data:
            self.refresh_topology()

    # --- Topology tab -------------------------------------------------------

    def _topology_links(self) -> dict[str, list[str]]:
        """device id -> its control / data interface on the BB (from Mappings)."""
        out = {}
        for n in self.ctx.registry.nodes():
            if isinstance(n.config, DeviceConfig) and n.config.backbone:
                actual = self._bb_actual.get(n.config.backbone.backbone_id, {})
                ifaces = quick.bb_interfaces(n.name, n.config.backbone.interfaces, n.config.role)
                out[n.id] = [actual.get(i.lower(), i) for i in ifaces]
        return out

    def _topology_spec(self, node):
        from ..transports import base as transport
        return transport.connect_spec(node.config, self.ctx.registry.serial_of(node))

    def refresh_topology(self) -> None:
        from ..core.models import Status
        from ..transports import base as transport
        nodes = [n for n in self.ctx.registry.nodes() if n.kind in ("device", "backbone")]
        self.topology.set_nodes(nodes, self._topology_links())
        self._topology_pending = []
        for n in nodes:
            if n.kind == "device" and n.state.status != Status.CONNECTED:
                self.topology.set_error(n.id, f"{n.state.status.value} – cannot read")
                continue
            try:
                spec = self._topology_spec(n)
            except transport.TransportError as err:
                self.topology.set_error(n.id, str(err))
                continue
            if spec.type == "none":
                self.topology.set_error(n.id, "not configured (Mappings)")
                continue
            self.topology.set_loading(n.id)
            self._topology_pending.append(n.id)
            serial = spec.params.get("serial", "") if spec.type == "adb" else ""
            self.ctx.topology.collect(n.id, spec, serial)
        self.topology.redraw()
        self.topology.status.setText(f"reading interfaces of {len(self._topology_pending)} node(s)…"
                                     if self._topology_pending else "Nothing reachable to read.")

    def _iface_clicked(self, node_id: str, name: str) -> None:
        node = self.ctx.registry.get(node_id)
        if node is None:
            return
        try:
            self.ctx.topology.detail(node_id, self._topology_spec(node), name)
        except Exception as err:
            self.topology.set_detail(node_id, name, f"cannot read: {err}")

    def ap_qcmap(self, preset_id: str, delay_ms: int, settle_ms: int) -> None:
        nodes = self._need_ap()
        preset = self.ctx.presets.get(preset_id)
        if nodes and preset:
            words = self.ap.success_words.text().strip() or DEFAULT_SUCCESS_WORDS
            self.ctx.user.set("qcmap_success_words", words)
            self.start_sequence(preset, nodes, delay_ms, settle_ms, words_regex(words))

    def start_sequence(self, preset: Preset, nodes, delay_ms=None, settle_ms=None, success=None) -> None:
        for n in nodes:
            old = self._sequences.get(n.id)
            if old and old.active:
                self.statusBar().showMessage(f"{n.name}: a sequence is already running (Stop first).", 8000)
                continue
            err = self._open_one_shell(n, show=False)
            s = self.ctx.shells.sessions.get(n.id)
            if s is None:
                self.statusBar().showMessage(f"{n.name}: {err}", 8000)
                self.ap.log(False, f"{preset.name} on {n.name}: {err}")
                continue
            self.ap.qcmap_reset()
            self.ap.log(None, f"{preset.name} started on {n.name}")
            seq = MenuSequence(s, preset.sequence, self.ctx.bus, preset.name, delay_ms, settle_ms,
                               success=success)
            self._sequences[n.id] = seq
            seq.start()
        self.ap.set_qcmap(f"{preset.name}: starting…", "running")

    def _shell_interrupt(self, node_id: str) -> None:
        """^C on a pane: stop a QCMAP run there, then Ctrl+C (enforced with SIGINT)."""
        seq = self._sequences.get(node_id)
        if seq and seq.active:
            seq.stop()
        if not self.ctx.shells.interrupt(node_id):
            self.statusBar().showMessage("Ctrl+C not sent: the shell is not open (⟳ to reconnect).", 6000)

    def _shell_key(self, node_id: str, key: str) -> None:
        """Ctrl+Z / Ctrl+D / Ctrl+\\ from a shell pane: exactly that key (plus the
        matching signal over a second adb connection), nothing else."""
        seq = self._sequences.get(node_id)
        if seq and seq.active:
            seq.stop()
        if not self.ctx.shells.send_key(node_id, key):
            self.statusBar().showMessage("Key not sent: the shell is not open (⟳ to reconnect).", 6000)

    def exit_menu(self) -> None:
        import threading
        self.stop_sequences()
        for n in self._need_ap():
            if not self.ctx.shells.is_open(n.id):
                self.ap.log(None, f"{n.name}: no shell open")
                continue
            self.ap.log(None, f"Exit QCMAP_CLI on {n.name}: trying Ctrl+C, Ctrl+Z, then killall")

            def work(n=n):
                how = self.ctx.shells.leave_menu(n.id)
                ok = how not in ("still in menu", "no shell")
                self.ctx.bus.publish(CheckResult(n.id, n.name, "Exit QCMAP_CLI", ok,
                                                 {"Ctrl+C": "left with Ctrl+C", "Ctrl+Z": "left with Ctrl+Z",
                                                  "killall": "menu ignored Ctrl+C/Z, closed with killall",
                                                  "not in a menu": "the shell was not in a menu"}.get(how, how)))

            threading.Thread(target=work, daemon=True).start()

    def stop_sequences(self) -> None:
        for seq in self._sequences.values():
            seq.stop()

    def ap_stats_start(self) -> None:
        nodes = self._need_ap()
        preset = self.ctx.presets.get("ap-stats")
        if not nodes or not preset:
            return
        parser = parsers.PARSERS[preset.monitor.parser]
        n = nodes[0]

        import time as _time
        last: dict[str, tuple[float, int, str]] = {}  # phy -> (time, counter, clock)

        def on_output(node, text):
            try:
                metrics = parser(text)
            except Exception as e:
                metrics = {"parse_error": str(e)}
            now = _time.monotonic()
            for name, r in (metrics.get("radios") or {}).items():
                total = r.get("legacy_total")
                if total is None:
                    continue
                t, clock = r.get("uptime_s"), "device"
                if t is None:
                    t, clock = now, "pc"
                r["bcn_t"], r["bcn_clock"] = t, clock
                prev = last.get(name)
                if prev and prev[2] == clock and t > prev[0]:
                    dt, dc = t - prev[0], total - prev[1]
                    r["bcn_reset"] = dc < 0
                    if dc < 0:
                        dc = total
                    r["bcn_dt"], r["bcn_delta"], r["bcn_rate"] = dt, dc, dc / dt
                    r["bcn_interval_ms"] = dt / dc * 1000 if dc > 0 else None
                last[name] = (t, total, clock)
            self._enrich(node, metrics)
            self.ctx.samples.add(Sample(node.id, datetime.now(), metrics, text))

        self.ap.stats.beacons.clear()
        self.ctx.pollers.stop("monitor", n.id)
        err = self.ctx.pollers.start("apstats", n, expand(preset.monitor.command, self._values(n, {})),
                                     preset.monitor.interval_s, on_output)
        if err:
            self.ap.stats.set_status(f"Not started: {err}", "#ff6b7a")
        else:
            self.ap.stats.set_status(f"Starting on {n.name}…", "#2f6fed")

    def ap_stats_stop(self) -> None:
        self.ctx.pollers.stop("apstats")
        self.ap.stats.set_status("Stopped.")

    # --- crash dumps --------------------------------------------------------

    def _dump_nodes(self):
        sel = [self.ctx.registry.get(i) for i in self.ctx.selection]
        sel = [n for n in sel if n is not None and self.ctx.ramdumps.usable(n)]
        return sel or [n for n in self._ap_nodes() if self.ctx.ramdumps.usable(n)]

    def _dump_watch(self, on: bool, interval_s: int) -> None:
        if on:
            self.ctx.ramdumps.watch(self._dump_nodes, lambda: self.ap.dumps.path.text().strip(), interval_s)
            self.statusBar().showMessage(f"Watching crash dumps every {interval_s} s.", 6000)
        else:
            self.ctx.ramdumps.stop_watch()

    def pull_dumps(self, path: str, selected_only: bool) -> None:
        nodes = self._dump_nodes()
        if not nodes:
            self.statusBar().showMessage("No connected device to pull from.", 6000)
            return
        files = self.ap.dumps.selected() if selected_only else None
        if selected_only and not files:
            self.statusBar().showMessage("Select files in the list first (or use Pull all).", 6000)
            return
        if files is not None:
            nodes = [n for n in nodes if n.id in files] or [self.ctx.registry.get(i) for i in files]
            nodes = [n for n in nodes if n]
        dest = self.ctx.export_dir / "ramdumps"
        self.ctx.ramdumps.pull(nodes, path, dest, files)
        self.statusBar().showMessage(f"Pulling {path} from {', '.join(n.name for n in nodes)} to {dest} …", 10000)

    def clear_dumps(self, path: str) -> None:
        nodes = self._dump_nodes()
        if not nodes:
            self.statusBar().showMessage("No connected device.", 6000)
            return
        names = ", ".join(n.name for n in nodes)
        if QMessageBox.question(self, "Clear crash dumps",
                                f"Delete ALL files in {path} on {names}?\nThis cannot be undone.") != QMessageBox.Yes:
            return
        self.ctx.ramdumps.clear(nodes, path)

    def closeEvent(self, e) -> None:
        active = self.ctx.runner.active_jobs()
        if active and QMessageBox.question(
                self, "Quit", f"{len(active)} command(s) still running. Stop them and quit?") != QMessageBox.Yes:
            e.ignore()
            return
        self.discovery.stop()
        self.ctx.shutdown()
        e.accept()


def ev_status(ok: bool):
    from ..core.models import Status
    return Status.CONNECTED if ok else Status.UNAUTHORIZED
