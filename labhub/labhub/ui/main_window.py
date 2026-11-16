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
from ..core.bbnet import BbAddrs, BbExecDone, BbGlobals, BbPeerAddr, BbRoutes, lookup
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
from ..core import traffic as trf
from ..core.remote_hosts import HostState
from ..core.associate import ApBeacons, BeaconCount, MldState
from .associate import Associator
from .prepare import BackbonePrep
from .ap_view import ApView, show_max_sta
from ..core.targets import TargetError, node_role
from ..discovery.adb_discovery import Discovery
from .beacons import BeaconsView
from .common import STATUS_COLOR, QtBridge
from .console import CommandBar, ConsoleView, StatsView
from .graphs import GraphsView
from .logs_tab import FwWirelessTab, LogsTab
from .traffic import PREVIEW as PREVIEW_NOTE, TrafficView
from .traffic_graph import fmt_rate
from .split_tabs import SplitTabs
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
        self.cmd.hide()   # no command bar any more; Reboot all / Stop all are in the toolbar

        self.presets = PresetPanel(ctx)          # kept for editing; shown as a menu, not a side panel
        self.presets.activated.connect(self.run_preset)
        self.presets.changed.connect(self._presets_changed)
        self.cmd.set_preset_menu(self._build_preset_menu)
        self.tabs = SplitTabs(ctx.user)          # right-click a tab: show it side by side
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
        self._qwes_timer = QTimer(self, interval=5000, timeout=self._qwes_tick)
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
        self.fw = FwWirelessTab(ctx.user, ctx.registry)   # DevReader (devices from Mappings), loaded when first opened
        # one rescan of the embedded apps for a burst of device changes
        self._apps_rescan = QTimer(self, singleShot=True, interval=1500,
                                   timeout=lambda: (self.fw.devices_changed(), self.logs.devices_changed()))
        self.tabs.addTab(self.fw, "FW/Wireless")
        self.tabs.addTab(self.console, "Console")
        self.stats = StatsView()
        self.stats.restart.connect(self.restart_stats)
        self.stats.interrupt.connect(self.ctx.shells.interrupt)
        self.stats.restart_all.connect(lambda: [self.restart_stats(d) for d in list(self.stats.panes)])
        self.stats.stop_all.connect(self.stop_all_stats)
        self.tabs.addTab(self.stats, "Stats")
        self.traffic = TrafficView()
        self.traffic.saved = dict(ctx.user.get("traffic_addrs") or {})
        self.traffic.refresh.connect(self.read_traffic_addrs)
        self.traffic.picked.connect(self._traffic_picked)
        self.traffic.scenario.connect(self.run_traffic)
        self.traffic.shell_send.connect(self._shell_write)
        self.traffic.shell_interrupt.connect(self.ctx.shells.interrupt)
        self.traffic.shell_key.connect(self.ctx.shells.send_key)
        self._traffic_keys: dict[str, str] = {}    # iperf3 shell key -> CPE name
        self.traffic.channels = dict(ctx.user.get("assoc_channels") or {})
        self.traffic.assoc_off = set(ctx.user.get("assoc_unticked") or [])
        self.traffic.traffic_off = set(ctx.user.get("traffic_unticked") or [])
        self.traffic.run_picks_changed.connect(
            lambda: self.ctx.user.set("traffic_unticked", sorted(self.traffic.traffic_off)))
        self.traffic.picks_changed.connect(
            lambda: self.ctx.user.set("assoc_unticked", sorted(self.traffic.assoc_off)))
        self.traffic.channel_changed.connect(self._assoc_channel)
        self.traffic.associate.connect(self.associate)
        self.traffic.associate_one.connect(self.associate_one)
        self.traffic.associate_stop.connect(self.associate_stop)
        self.assoc = Associator(self)
        self.traffic.ap_up.connect(self.make_ap_transmit)
        self.traffic.ap_up_stop.connect(lambda: self.assoc.stop())
        self._ap_bcn: list[tuple[float, int | None, int | None]] = []   # (time, phy0, phy1) readings
        self.prep = BackbonePrep(self)
        self.traffic.prepare.connect(self.prepare_backbones)
        self.traffic.prepare_stop.connect(lambda: self.prep.stop())
        self._beacons: dict[str, int | None] = {}   # cpe id -> beacons heard (last reading)
        self._assoc_at: dict[str, tuple] = {}   # cpe id -> (associated, counter when it did, seen happening)
        self._bcn_hold: set[str] = set()   # CPEs cleared by Stop / Reboot all: not read until back or Associate
        self._bcn_timer = QTimer(self, interval=2000, timeout=self._poll_beacons)
        self._bcn_timer.start()
        self._traffic_carry: dict[str, str] = {}
        self.tabs.addTab(self.traffic, "Traffic")
        self.graphs = GraphsView()
        self.graphs.restart_all.connect(lambda: [self.restart_graphs(d) for d in list(self.graphs.cards)])
        self.graphs.stop_all.connect(self.stop_all_graphs)
        self.tabs.addTab(self.graphs, "Graphs")
        self.logs = LogsTab(ctx.user, ctx.registry)   # Log Collector (devices from Mappings), loaded when first opened
        self.tabs.addTab(self.logs, "Logs")
        self.tabs.setCurrentWidget(self.console)
        self.tabs.restore()                      # tabs left on the right side last time
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
        b = QPushButton("Open all shells")
        b.setToolTip("Open a Console shell on every AP and CPE (adb) and every backbone (ssh)")
        b.clicked.connect(lambda: self.open_shells(["all"]))
        h.addWidget(b)
        b = QPushButton("⏻  Reboot all")
        b.setToolTip("Reboot every AP and CPE of the lab (never the backbones).\n"
                     "A running menu (QCMAP_CLI) is left with Ctrl+C / Ctrl+Z first, then 'reboot' is typed.")
        b.clicked.connect(self.reboot_all)
        h.addWidget(b)
        b = QPushButton("■  Stop all")
        b.setObjectName("Danger")
        b.setToolTip("Ctrl+C to every open shell (devices, backbones, Stats, Traffic iperf3)\n"
                     "and stop all running commands and pollers")
        b.clicked.connect(self.stop_everything)
        h.addWidget(b)
        h.addSpacing(12)
        self.theme_btn = QPushButton()
        self.theme_btn.setToolTip("Switch between night (dark) and day (light) colours")
        self.theme_btn.clicked.connect(self.toggle_theme)
        self._theme_label()
        h.addWidget(self.theme_btn)
        self.lab_msg = QLabel()
        self.lab_msg.setStyleSheet("color: #ff6b7a;")
        h.addWidget(self.lab_msg)
        self.max_sta_label = QLabel()
        show_max_sta(self.max_sta_label, None, "max_sta from the AP's QWES lines in /tmp/hostapd.txt")
        h.addWidget(self.max_sta_label)
        b = QPushButton("Push license")
        b.setToolTip("adb push of the license file (Mappings → AP license file) to the AP")
        b.clicked.connect(self.push_license)
        h.addWidget(b)
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
        self.fw.theme_changed()                # DevReader's page has its own stylesheet: it follows too
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
            host = d.extra.get("host") or "this PC"
            lines.append(f"{d.name:6} serial={d.serial!r} -> {ser!r}   {state}   (on {host})")
        if seen:
            lines.append(f"unassigned: {', '.join(sorted(seen))}")
        for host, tun in sorted(self.ctx.remote.tunnels.items()):
            lines += ["", f"--- device host {host}: {'UP' if tun.up else 'DOWN'} – {tun.text} ---",
                      tun.started or "(adb not started there yet)"]
            if tun.up and self.discovery.adb_path:
                import subprocess
                try:
                    res = subprocess.run([self.discovery.adb_path, "-P", str(tun.port), "devices", "-l"],
                                         capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL,
                                         creationflags=transport.CREATE_NO_WINDOW)
                    lines += [f"$ adb -P {tun.port} devices -l   (through the tunnel, exit {res.returncode})",
                              (res.stdout + res.stderr).strip()]
                except subprocess.TimeoutExpired:
                    lines.append(f"adb -P {tun.port} devices -l: timed out")
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
            self.traffic.set_devices(self._traffic_devices())
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
            self._adb_msg = e.message
            self.adb_label.setText(f"● {e.message}" + self._hosts_text())
            self.adb_label.setStyleSheet(f"color: {color};")
        elif isinstance(e, HostState):
            self.adb_label.setText(f"● {getattr(self, '_adb_msg', 'adb')}" + self._hosts_text())
            self.statusBar().showMessage(f"Device host {e.host}: {e.text}", 12000)
            self.discovery.refresh()             # its devices show up (or go) now
        elif isinstance(e, ev.NodeStatusChanged):
            self._apps_rescan.start()            # FW/Wireless + Logs look at the devices again
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
                    self.assoc.on_quick(e.node_id, qkey, ok, e.text + (f" ({e.detail})" if e.detail else ""))
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
            if e.state in ("back", "failed"):
                self._bcn_hold.discard(e.node_id)      # rebooted: its beacons / association are read again
                if self._recovering.get(e.node_id) == "reboot":
                    self._recover_rebooted(e.node_id, e.state == "back")
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
            if qkey:
                self.assoc.on_quick(e.node_id, qkey, e.ok, e.detail)
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
            if e.session.node_id in self._traffic_keys:
                self.traffic.output(e.session, e.text, e.clear)
                self._traffic_rates(e.session, e.text)
                self._iperf_lines(e.session, e.text)
                if "iperf3: error" in e.text:
                    QTimer.singleShot(0, lambda s=e.session, t=e.text: self._traffic_error(s, t))
            elif not self.stats.output(e.session, e.text, e.clear):
                self.console.shell_output(e.session, e.text, e.clear)
                self.prep.on_output(e.session.node_id, e.text)
                if "[SUM" in e.text or e.session.node_id in self._iperf_carry:
                    self._iperf_lines(e.session, e.text)
        elif isinstance(e, ShellState):
            if e.session.node_id in self._traffic_keys:
                self.traffic.state(e.session)
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
        elif isinstance(e, BeaconCount):
            if e.node_id in self._bcn_hold:            # cleared: keep the table empty
                return
            if e.value is not None or e.node_id not in self._beacons:
                self._beacons[e.node_id] = e.value
            self.traffic.set_beacons(e.node_id, e.value, e.error)
            if e.associated is not None:
                was = self._assoc_at.get(e.node_id)
                if e.associated and not (was and was[0]):       # just associated: keep the counter now
                    self._assoc_at[e.node_id] = (True, e.value, was is not None)
                    name = self.ctx.registry.get(e.node_id).name if self.ctx.registry.get(e.node_id) else e.node_id
                    if was is not None:
                        self.traffic.log("ok", f"{name}: associated after {e.value} beacons")
                elif not e.associated:
                    self._assoc_at[e.node_id] = (False, None, False)
                self.traffic.set_association(e.node_id, *self._assoc_at[e.node_id])
            self.assoc.on_beacons(e.node_id, e.value)
        elif isinstance(e, ApBeacons):
            import time as _t
            if e.phy0 is not None or e.phy1 is not None:
                self._ap_bcn = [r for r in self._ap_bcn if _t.monotonic() - r[0] < 20] + [(_t.monotonic(), e.phy0, e.phy1)]
            self.traffic.set_ap_beacons(e.phy0, e.phy1, self.ap_beaconing(), e.error)
        elif isinstance(e, MldState):
            self.assoc.on_mlds(e.node_id, e.up, e.detail)
        elif isinstance(e, trf.TrafficLog):
            self.traffic.log(e.level, e.text)
        elif isinstance(e, trf.TrafficReady):
            self._traffic_start(e.plan)
        elif isinstance(e, trf.TrafficFailed):
            self.traffic.uncheck()
            self.traffic.diagram.set_flow(None)
            self.statusBar().showMessage(f"Traffic not started: {e.text}", 15000)
        elif isinstance(e, BbGlobals):
            self.traffic.set_addrs(e.node_id, e.addrs, e.error)
            self.prep.on_globals(e.node_id, e.addrs, e.error)
            self.traffic.status.setText(f"Read at {datetime.now():%H:%M:%S}.")
        elif isinstance(e, BbRoutes):
            if e.family == "6" and not e.error:
                self.topology.set_routes(e.node_id, e.routes)
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
                self.prep.on_quick(e.node_id, e.key, ok, e.output or f"exit {e.code}")
            QTimer.singleShot(1500, lambda nid=e.node_id: self.check_bb_addrs(nid))
        elif isinstance(e, lic.MaxSta):
            self._set_max_sta(e.value, e.detail)
        elif isinstance(e, lic.LicensePushed):
            self.ap.log(e.ok, f"License push to {e.name}: {e.text}")
            self.statusBar().showMessage(f"License push to {e.name}: {'OK' if e.ok else 'FAILED'} – {e.text}", 15000)
            if e.ok:                             # the license is read at boot: reboot the AP (not its BB)
                ap = self.ctx.registry.get(e.node_id)
                if ap is not None:
                    self.reboot([ap], ask=False)
                    self.ap.log(None, f"License push to {e.name}: rebooting {e.name} so the license is used")
            self._result_popup("Push license", e.ok, f"License push to {e.name}: {'PASSED' if e.ok else 'FAILED'}",
                               e.text + (f"\n\n{e.name} is rebooting now." if e.ok else ""))
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
        self._clear_assoc_data("Stop all")
        self.prep.stop("stopped (Stop all)")
        self.traffic.uncheck()
        self.stop_traffic()
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
                       "(empty = <name>.960 + v10371015 for the AP, <name>.1015 for a CPE). Checked every 20 s.")
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
            v.addWidget(QLabel("{ctrl} = control interface (.960), {data} = data interface (AP v10371015, CPE .1015), "
                               "{ifaces} = both – per device, from Mappings → BB interfaces "
                               "(empty there = <name>.960 / v10371015 or <name>.1015). Typing a fixed name instead "
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
            if key == "ap.license":
                self.push_license()
            else:
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
        node = self.ctx.registry.get(session.device_id)
        if node is None:
            return
        dev = self._bb_device(node) if node.kind == "backbone" else node
        card = self.graphs.cards.get(dev.id) if dev is not None else None
        if card is None:
            return
        for line in lines[:-1]:
            s = parsers.parse_iperf_sum(line)
            if s and not s.get("final"):
                card.add_iperf(s, session.name if session.node_id != session.device_id else node.name)

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

    def _qwes_tick(self) -> None:
        """Read the AP's newest QWES max_sta (a one-shot adb command every few seconds)."""
        aps = self._ap_nodes(connected=False)
        ap = aps[0] if aps else None
        if ap is None or ap.state.status.value != "connected" or self.ctx.reboots.is_rebooting(ap.id):
            self._set_max_sta(None, "AP not connected" if ap else "no AP in the lab")
            return
        self.ctx.license.read_max_sta(ap.id, self.ctx.registry.serial_of(ap))

    def _result_popup(self, title: str, ok: bool, headline: str, detail: str) -> None:
        """A green (passed) or red (failed) message box."""
        box = QMessageBox(QMessageBox.Information if ok else QMessageBox.Critical, title, headline,
                          QMessageBox.Ok, self)
        box.setInformativeText(detail)
        bg = "#1f6f43" if ok else "#8b1e2b"
        box.setStyleSheet(f"QMessageBox {{ background: {bg}; }} QLabel {{ color: #ffffff; background: transparent; }}"
                          " QPushButton { min-width: 70px; }")
        box.exec()

    def _set_max_sta(self, value: int | None, detail: str = "") -> None:
        show_max_sta(self.max_sta_label, value, detail)
        self.ap.set_max_sta(value, detail)

    def push_license(self) -> None:
        aps = self._need_ap()
        if not aps:
            return
        ap = aps[0]
        local, dest = lic.settings(self.ctx.lab.features)
        if QMessageBox.question(self, "Push license", f"Push the license to {ap.name}?\n\n{local}\n→ {dest}/\n\n"
                                f"{ap.name} is rebooted after the push, so the license takes effect.") \
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

    def reboot(self, nodes, ask: bool = True) -> None:
        """ask False: no question (the caller asked already, e.g. the license push)."""
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
        if ask and QMessageBox.question(self, "Reboot", f"Reboot {names}?\n\nA running menu (QCMAP_CLI) is left with "
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
        # two tabs can be in view (side by side): each one that is shown gets what it needs
        shown = self.tabs.is_shown
        if shown(self.stats):
            self._sync_stats()
        if shown(self.topology) and not self.topology.data:
            self.refresh_topology()
        if shown(self.traffic) and not self._traffic_read:
            self.read_traffic_addrs()
        if shown(self.logs):
            self.logs.load()
        if shown(self.fw):
            self.fw.load()

    # --- Traffic tab ----------------------------------------------------------

    _traffic_read = False

    def _traffic_devices(self) -> list[dict]:
        """AP / CPEs in display order, with their BB and data interface (Mappings)."""
        out = []
        for n in self.ctx.registry.nodes():
            if not isinstance(n.config, DeviceConfig) or n.config.role not in ("ap", "cpe"):
                continue
            link = n.config.backbone
            bb = self.ctx.registry.get(link.backbone_id) if link and link.backbone_id else None
            data = ctrl = ""
            if bb is not None:
                v = quick.bb_values(n.name, link.interfaces, self._bb_actual.get(bb.id), n.config.role)
                data, ctrl = v["data"], v["ctrl"]
            out.append({"id": n.id, "name": n.name, "role": n.config.role, "bb_id": bb.id if bb else "",
                        "bb_name": bb.name if bb else "", "data": data, "ctrl": ctrl})
        return out

    def read_traffic_addrs(self) -> None:
        self._traffic_read = True
        self.traffic.set_devices(self._traffic_devices())
        ids = self.traffic.bb_ids()
        if not ids:
            self.traffic.status.setText("No device has a backbone: set them in ✎ Mappings.")
            return
        self.traffic.status.setText(f"Reading the global IPv6 addresses of {len(ids)} backbone(s)…")
        for bid in ids:
            node = self.ctx.registry.get(bid)
            if node is not None:
                self.traffic.reading(bid)
                self.ctx.bbnet.read_globals(node)

    _traffic_plan = None

    # --- a CPE whose iperf3 fails: reboot it, associate it again, restart its traffic -----------

    @property
    def _recovering(self) -> dict:
        """cpe id -> asking / declined / reboot / assoc (one recovery per CPE per run)."""
        if not hasattr(self, "_recover_state"):
            self._recover_state = {}
        return self._recover_state

    def _traffic_error(self, session, text: str) -> None:
        plan = self._traffic_plan
        name = self._traffic_keys.get(session.node_id)
        c = next((c for c in plan.cpes if c.name == name), None) if plan else None
        if c is None or c.id in self._recovering:
            return
        line = next((l.strip() for l in text.splitlines() if "iperf3: error" in l), "iperf3: error")
        self.traffic.log("fail", f"{c.name}: {line}")
        self._recovering[c.id] = "asking"
        if QMessageBox.question(self, "Traffic", f"{c.name}: {line}\n\nReboot {c.name}, associate it again and "
                                f"restart its traffic? (the other CPEs keep running)") != QMessageBox.Yes:
            self._recovering[c.id] = "declined"
            return
        for key in (f"traffic:{c.id}:srv", f"traffic:{c.id}:cli"):
            self.ctx.shells.interrupt(key)
        node = self.ctx.registry.get(c.id)
        if node is None:
            self._recovering.pop(c.id, None)
            return
        self._recovering[c.id] = "reboot"
        self.traffic.log("info", f"{c.name}: rebooting – then Associate and its traffic again")
        self.reboot([node], ask=False)

    def _recover_rebooted(self, cpe_id: str, back: bool) -> None:
        name = self.ctx.registry.get(cpe_id).name if self.ctx.registry.get(cpe_id) else cpe_id
        if not back:
            self.traffic.log("fail", f"{name}: did not come back after the reboot – its traffic stays stopped")
            self._recovering.pop(cpe_id, None)
            return
        self._recovering[cpe_id] = "assoc"
        QTimer.singleShot(5000, lambda: self._recover_assoc(cpe_id))

    def _recover_assoc(self, cpe_id: str) -> None:
        if self._recovering.get(cpe_id) != "assoc":
            return
        if self.assoc.active:                    # another Associate is running: wait for it
            QTimer.singleShot(5000, lambda: self._recover_assoc(cpe_id))
            return
        self.assoc.on_done = lambda ok: self._recover_traffic(cpe_id, ok)
        self.associate_one(cpe_id)
        if not self.assoc.active:                # did not start (e.g. not connected)
            self.assoc.on_done = None
            self._recover_traffic(cpe_id, False)

    def _recover_traffic(self, cpe_id: str, ok) -> None:
        self._recovering.pop(cpe_id, None)
        plan = self._traffic_plan
        c = next((c for c in plan.cpes if c.id == cpe_id), None) if plan else None
        if c is None:
            return                               # the run was stopped meanwhile
        if not ok:
            self.traffic.log("fail", f"{c.name}: Associate failed – its traffic stays stopped")
            return
        cmds = trf.COMMANDS[(plan.direction, plan.proto)]
        values = {"cpe": c.addr, "ap": plan.ap_addr, "port": c.port, "rate": c.rate}
        i = plan.cpes.index(c)
        ap_serves = cmds["server_on"] == "ap"
        srv, cli = (plan.ap_bb, c.bb) if ap_serves else (c.bb, plan.ap_bb)
        self._traffic_shell(f"traffic:{c.id}:srv", srv, f"{srv.name} · iperf3 server for {c.name} :{c.port}",
                            i, 1, c.name, cmds["server"].format(**values))
        QTimer.singleShot(2000, lambda: self._traffic_plan is plan and self._traffic_shell(
            f"traffic:{c.id}:cli", cli, f"{cli.name} → {(c.name if not ap_serves else plan.ap_bb.name)}"
            f" · iperf3 client :{c.port}", i, 0, c.name, cmds["client"].format(**values)))
        self.traffic.log("ok", f"{c.name}: associated again – its traffic restarted")

    # --- Associate (Traffic tab) -------------------------------------------------

    def _cpe_nodes(self, connected: bool = False) -> list:
        return [n for n in self.ctx.registry.nodes() if isinstance(n.config, DeviceConfig) and n.config.role == "cpe"
                and (not connected or n.state.status.value == "connected")]

    def associate(self) -> None:
        aps = self._ap_nodes()
        if not aps:
            self._result_popup("Associate", False, "Associate not started", "The AP is not connected (adb).")
            return
        ticked = [n for n in self._cpe_nodes() if self.traffic.is_ticked(n.id)]
        if not ticked:
            self._result_popup("Associate", False, "Associate not started", "No CPE is ticked.")
            return
        cpes = [n for n in ticked if n.state.status.value == "connected"]
        down = [n.name for n in ticked if n not in cpes]
        if not cpes:
            self._result_popup("Associate", False, "Associate not started", "No CPE is connected (adb).")
            return
        if down:
            self.traffic.log("warn", f"not connected, skipped: {', '.join(down)}")
        self._bcn_hold.clear()
        self.assoc.start(aps[0], [(n, self.traffic.channel(n.id)) for n in cpes], ap_mode="auto",
                         scan=self.traffic.scan_mode())

    def _hosts_text(self) -> str:
        """' · hosts 1/2' for the adb button (+ its tooltip: every host's tunnel)."""
        states = self.ctx.remote.states()
        tip = "Show what LabHub gets from adb, or choose adb.exe"
        if states:
            tip += "\n\nDevice hosts (ssh tunnel to their adb):\n" + "\n".join(
                f"  {'●' if up else '✖'} {h}: {text}" for h, up, text in states)
        self.adb_label.setToolTip(tip)
        if not states:
            return ""
        return f" · hosts {sum(1 for _h, up, _t in states if up)}/{len(states)}"

    def ap_beaconing(self) -> bool | None:
        """True if a phy0 / phy1 beacon counter rose in the last readings; None = not known yet."""
        import time as _t
        recent = [r for r in self._ap_bcn if _t.monotonic() - r[0] < 10]
        if len(recent) < 2:
            return None
        first, last = recent[0], recent[-1]
        return any(a is not None and b is not None and b > a for a, b in zip(first[1:], last[1:]))

    def poll_ap_beacons(self) -> None:
        for ap in self._ap_nodes():
            if not self.ctx.reboots.is_rebooting(ap.id):
                self.ctx.devreader.read_ap_beacons(ap.id, self.ctx.registry.serial_of(ap))

    def make_ap_transmit(self) -> None:
        aps = self._ap_nodes()
        if not aps:
            self._result_popup("AP transmit", False, "Not started", "The AP is not connected (adb).")
            return
        self.assoc.start(aps[0], [], ap_mode="always", scan=self.traffic.scan_mode())

    def associate_one(self, cpe_id: str) -> None:
        """One CPE's own Associate button: its steps only (the AP must already be up)."""
        aps = self._ap_nodes()
        cpe = self.ctx.registry.get(cpe_id)
        if not aps:
            self._result_popup("Associate", False, "Associate not started", "The AP is not connected (adb).")
            return
        if cpe is None or cpe.state.status.value != "connected":
            self._result_popup("Associate", False, "Associate not started",
                               f"{cpe.name if cpe else cpe_id} is not connected (adb).")
            return
        self._bcn_hold.discard(cpe_id)
        self.assoc.start(aps[0], [(cpe, self.traffic.channel(cpe_id))], ap_mode="auto", scan=self.traffic.scan_mode())

    def associate_stop(self) -> None:
        """Associate's Stop: end the run, clear the statuses, then Reboot all."""
        self.reboot_all("Associate Stop")

    def reboot_all(self, why: str = "Reboot all") -> None:
        self._clear_assoc_data(why)
        self._reboot_targets(["kind:device"])

    def _clear_assoc_data(self, why: str) -> None:
        """Stop Associate and empty its table; the CPEs are not read again until they are back
        from a reboot or Associate starts (else the next reading would fill it right away)."""
        self.assoc.stop(f"stopped ({why})")
        self._beacons.clear()
        self._assoc_at.clear()
        self._ap_bcn.clear()
        self._bcn_hold = {n.id for n in self._cpe_nodes()}
        self.traffic.clear_assoc()
        self.traffic.log("info", f"Associate: cleared ({why})")

    def assoc_quick(self, node, key: str) -> str | None:
        """Run a quick button of the device's Console shell (opened if needed); error text or None."""
        err = self._open_one_shell(node, show=False)
        if node.id not in self.console.shells:
            return err or f"{node.name}: shell not available"
        if node.id in self._quick_checks or node.id in self._quick_seq:
            return f"{node.name}: a quick command is still running in its shell"
        self.run_quick(node.id, key)
        return None

    def quick_text(self, node, key: str) -> str:
        qc = quick.find(key)
        return self._quick_text(node, qc) if qc else ""

    def prepare_backbones(self) -> None:
        if self.ctx.traffic.busy:
            self.statusBar().showMessage("Traffic checks are running – try again in a moment.", 6000)
            return
        plan = self._traffic_plan_for("dl", "udp")    # AP BB + every CPE with its BB and data interface
        if isinstance(plan, str):
            self._result_popup("Prepare backbones", False, "Not started", plan)
            return
        cpes = [(self.ctx.registry.get(c.id), c.bb, c.data) for c in plan.cpes]
        self.prep.start(plan.ap_bb, [x for x in cpes if x[0] is not None])

    def beacon_count(self, cpe_id: str) -> int | None:
        return self._beacons.get(cpe_id)

    def _poll_beacons(self) -> None:
        """Beacons heard by every connected CPE, while the Traffic tab is shown or Associate runs."""
        if not (self.assoc.active or self.tabs.is_shown(self.traffic)):
            return
        self.poll_ap_beacons()
        for n in self._cpe_nodes(connected=True):
            if not self.ctx.reboots.is_rebooting(n.id) and n.id not in self._bcn_hold:
                self.ctx.devreader.read_beacons(n.id, self.ctx.registry.serial_of(n))

    def _assoc_channel(self, cpe_id: str, ch: str) -> None:
        self.ctx.user.set("assoc_channels", dict(self.traffic.channels))

    def run_traffic(self, direction: str, proto: str) -> None:
        if not direction:
            return self.stop_traffic()
        if not trf.supported(direction, proto):
            self.traffic.diagram.set_flow(direction, proto, note=PREVIEW_NOTE)
            runs = ", ".join(trf.label(*k) for k in trf.COMMANDS)
            self.traffic.log("info", f"{trf.label(direction, proto)}: not wired to iperf yet "
                                     f"(runs: {runs}) – animation only")
            return
        if self.ctx.traffic.busy:
            self.statusBar().showMessage("Traffic: the checks of the last start are still running.", 6000)
            return
        plan = self._traffic_plan_for(direction, proto)
        if isinstance(plan, str):
            self.traffic.uncheck()
            self.traffic.log("fail", plan)
            self._result_popup("Traffic", False, "Traffic not started", plan)
            return
        self._close_traffic_shells()
        self.traffic.log_box.clear()
        cpes = ", ".join(f"{c.name} (:{c.port})" for c in plan.cpes)
        way = f"{plan.ap_bb.name} → {cpes}" if direction == "dl" else f"{cpes} → {plan.ap_bb.name}"
        self.traffic.log("info", f"{trf.label(direction, proto)}: {way}")
        self.traffic.diagram.set_flow(direction, proto, [c.name for c in plan.cpes], note="preparing…")
        self.ctx.traffic.prepare(plan)

    def _traffic_plan_for(self, direction: str, proto: str):
        """A Plan from Mappings + the Traffic tab picks, or an error text."""
        devs = [n for n in self.ctx.registry.nodes() if isinstance(n.config, DeviceConfig)
                and n.config.backbone and n.config.backbone.backbone_id]
        ap = next((n for n in devs if n.config.role == "ap"), None)
        if ap is None:
            return "The AP has no backbone – set it in ✎ Mappings."
        ap_bb = self.ctx.registry.get(ap.config.backbone.backbone_id)
        if ap_bb is None:
            return f"{ap.name}'s backbone is not in the lab."
        v = quick.bb_values(ap.name, ap.config.backbone.interfaces, self._bb_actual.get(ap_bb.id), "ap")
        plan = trf.Plan(direction, proto, ap_bb, v["ctrl"], v["data"], ap_wanted=self.traffic.selected(ap.id))
        for i, n in enumerate([n for n in devs if n.config.role == "cpe" and self.traffic.is_run_ticked(n.id)]):
            bb = self.ctx.registry.get(n.config.backbone.backbone_id)
            if bb is None:
                continue
            data = quick.bb_values(n.name, n.config.backbone.interfaces, self._bb_actual.get(bb.id), "cpe")["data"]
            plan.cpes.append(trf.CpePlan(n.id, n.name, bb, data, self.traffic.selected(n.id), trf.BASE_PORT + i,
                                         rate=self.traffic.manual_rate(n.id), manual=self.traffic.is_manual(n.id)))
        if not plan.cpes:
            return "No CPE has a backbone – set them in ✎ Mappings."
        return plan

    def _traffic_start(self, plan) -> None:
        """Checks passed: a server shell per CPE on the receiving side (downlink: the CPE's BB,
        uplink: the AP BB), then (2 s later) a client shell per CPE on the sending side."""
        self._traffic_plan = plan
        names = ", ".join(c.name for c in plan.cpes)
        way = f"{plan.ap_bb.name} → {names}" if plan.direction == "dl" else f"{names} → {plan.ap_bb.name}"
        self.traffic.graph.reset(f"{trf.label(plan.direction, plan.proto)}  ·  {way}", [c.name for c in plan.cpes],
                                 plan.direction, sent=plan.proto != "tcp")
        cmds = trf.COMMANDS[(plan.direction, plan.proto)]
        ap_serves = cmds["server_on"] == "ap"

        def sides(c):
            """(server node, client node, server title, client title) for one CPE."""
            if ap_serves:
                return (plan.ap_bb, c.bb, f"{plan.ap_bb.name} · iperf3 server for {c.name} :{c.port}",
                        f"{c.bb.name} → {plan.ap_bb.name} · iperf3 client :{c.port}")
            return (c.bb, plan.ap_bb, f"{c.bb.name} · iperf3 server for {c.name} :{c.port}",
                    f"{plan.ap_bb.name} → {c.name} · iperf3 client :{c.port}")

        for i, c in enumerate(plan.cpes):
            values = {"cpe": c.addr, "ap": plan.ap_addr, "port": c.port, "rate": c.rate}
            srv, _cli, srv_title, _ = sides(c)
            self._traffic_shell(f"traffic:{c.id}:srv", srv, srv_title, i, 1, c.name, cmds["server"].format(**values))
        where = plan.ap_bb.name if ap_serves else "the CPE BBs"
        self.traffic.log("ok", f"servers started on {where} – clients in 2 s")

        def clients():
            if self._traffic_plan is not plan:
                return
            for i, c in enumerate(plan.cpes):
                values = {"cpe": c.addr, "ap": plan.ap_addr, "port": c.port, "rate": c.rate}
                _srv, cli, _, cli_title = sides(c)
                self._traffic_shell(f"traffic:{c.id}:cli", cli, cli_title, i, 0, c.name,
                                    cmds["client"].format(**values))
            self.traffic.log("ok", f"running: {len(plan.cpes)} client(s) on "
                                   + ("the CPE BBs" if ap_serves else plan.ap_bb.name))
            self.traffic.diagram.set_flow(plan.direction, plan.proto, [c.name for c in plan.cpes])

        QTimer.singleShot(2000, clients)

    def _traffic_shell(self, key: str, node, title: str, row: int, col: int, cpe: str, command: str) -> None:
        self._traffic_keys[key] = cpe
        self.ctx.shells.close(key)
        err = self.ctx.shells.open(node, key, title)
        s = self.ctx.shells.sessions.get(key)
        if s is None:
            self.traffic.log("fail", f"{title}: shell not opened: {err}")
            return
        self.traffic.add_pane(key, s, title, row, col)
        s.write(command + "\n")       # queued until the login / namespace is ready
        self.traffic.log("info", f"{node.name}: {command}")

    def stop_traffic(self) -> None:
        for key in self._traffic_keys:
            self.ctx.shells.interrupt(key)
        self._traffic_plan = None
        self.traffic.diagram.set_flow(None)
        self.traffic.graph.stop()
        if self._traffic_keys:
            self.traffic.log("info", "stopped: Ctrl+C sent to the iperf3 shells")

    def _close_traffic_shells(self) -> None:
        for key in self._traffic_keys:
            self.ctx.shells.close(key)
        self._traffic_keys.clear()
        self._traffic_carry.clear()
        self._recovering.clear()
        self.traffic.clear_panes()

    def _traffic_rates(self, session, text: str) -> None:
        """[SUM] lines of the iperf3 shells -> the live graph (server = received, client = sent);
        the server's also -> the CPE's rate in the drawing."""
        server = session.node_id.endswith(":srv")
        buf = self._traffic_carry.pop(session.node_id, "") + text
        lines = buf.split("\n")
        self._traffic_carry[session.node_id] = lines[-1][-400:]
        for line in lines[:-1]:
            sm = parsers.parse_iperf_sum(line)
            if sm and not sm.get("final"):
                cpe = self._traffic_keys.get(session.node_id, "")
                self.traffic.graph.add(cpe, "rx" if server else "tx", sm["mbps"], sm.get("per_pct") if server else None)
                if server:
                    loss = f" · {sm['per_pct']:.2f} % loss" if sm.get("per_pct") is not None else ""
                    self.traffic.diagram.set_rate(cpe, f"{fmt_rate(sm['mbps'])}{loss}")

    def _traffic_picked(self, device_id: str, addr: str) -> None:
        self.traffic.saved[device_id] = addr
        self.ctx.user.set("traffic_addrs", dict(self.traffic.saved))

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
            if n.kind == "backbone":
                self.ctx.bbnet.routes(n, "6")      # drawn as the amber route lines
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
        if not self.logs.close_ok() or not self.fw.close_ok():   # a job the user wants to keep
            e.ignore()
            return
        self.discovery.stop()
        self.ctx.shutdown()
        e.accept()


def ev_status(ok: bool):
    from ..core.models import Status
    return Status.CONNECTED if ok else Status.UNAUTHORIZED
