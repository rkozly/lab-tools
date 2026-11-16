"""
Tabs that run a separate app of ours inside LabHub, loaded from its own folder
(not copied: changes there show up here on the next start):

  Logs         Log Collector   ..\\logCollector   (the folder of "Log Collector.bat")
  FW/Wireless  DevReader       ..\\DevReader      (the folder of "DevReader.bat")

Each is built the first time its tab is opened, keeps its own settings (the same
as when it runs alone), uses the adb LabHub uses (two different adb.exe keep
restarting each other's server) and gets its own look on its page only.
"Choose folder…" changes the folder (saved in the user file).
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent, QColor, QFont, QPalette
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ..transports import base as tr

ROOT = Path(tr.APP_ROOT).parent


def _adb_on_path() -> None:
    adb = tr.find_adb()
    if adb and os.path.dirname(adb) not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = os.path.dirname(adb) + os.pathsep + os.environ.get("PATH", "")


class EmbeddedTab(QWidget):
    NAME = ""                 # shown in messages
    SETTING = ""              # user-file key of the folder
    DEFAULT = ROOT
    NEEDS: tuple[str, ...] = ()   # files / folders that must be in the folder

    def __init__(self, user) -> None:
        super().__init__()
        self.user = user
        self.window = None                     # the app's MainWindow, once loaded
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.msg_box = QWidget()
        v = QVBoxLayout(self.msg_box)
        v.setContentsMargins(20, 20, 20, 20)
        self.msg = QLabel(f"{self.NAME} loads when this tab is opened.")
        self.msg.setWordWrap(True)
        self.msg.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self.msg)
        row = QHBoxLayout()
        b = QPushButton("Choose folder…")
        b.setToolTip(f"The {self.NAME} folder (the one with {', '.join(self.NEEDS)})")
        b.clicked.connect(self._choose)
        row.addWidget(b)
        b = QPushButton("Try again")
        b.clicked.connect(self.load)
        row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        v.addStretch(1)
        self.lay.addWidget(self.msg_box)

    def folder(self) -> Path:
        return Path(self.user.get(self.SETTING) or self.DEFAULT)

    def _choose(self) -> None:
        d = QFileDialog.getExistingDirectory(self, f"{self.NAME} folder", str(self.folder()))
        if d:
            self.user.set(self.SETTING, d)
            self.load()

    def load(self) -> None:
        if self.window is not None:
            return
        folder = self.folder()
        if not all((folder / n).exists() for n in self.NEEDS):
            self.msg.setText(f"{self.NAME} was not found in\n{folder}\n\n"
                             f"Choose the folder that has {', '.join(self.NEEDS)}.")
            return
        try:
            if str(folder) not in sys.path:
                sys.path.insert(0, str(folder))
            _adb_on_path()
            self.window = self._build(folder)
            self.window.setWindowFlags(Qt.Widget)   # a page inside the tab, not a separate window
        except Exception:
            self.window = None
            self.msg.setText(f"{self.NAME} could not be loaded from {folder}:\n\n{traceback.format_exc()}")
            return
        self.msg_box.hide()
        self.lay.addWidget(self.window)

    def _build(self, folder: Path):
        raise NotImplementedError

    def devices_changed(self) -> None:
        """LabHub saw a device connect / disconnect (or the tab is opened): the app looks again."""
        if self.window is not None:
            self._rescan()

    def _rescan(self) -> None:
        pass

    def showEvent(self, e) -> None:
        super().showEvent(e)
        # at LabHub's start the devices briefly drop off (adb root): look again a little later
        QTimer.singleShot(1500, self.devices_changed)

    def close_ok(self) -> bool:
        """LabHub is closing: let the app stop its work and save its settings.
        False if something still runs and the user wants to keep it."""
        if self.window is None:
            return True
        ev = QCloseEvent()
        self.window.closeEvent(ev)
        return ev.isAccepted()


def _palette(window: str, base: str, alt: str, button: str, text: str, disabled: str, accent: str,
             muted: str, tooltip: str) -> QPalette:
    p = QPalette()
    for role, color in ((QPalette.Window, window), (QPalette.Base, base), (QPalette.AlternateBase, alt),
                        (QPalette.Button, button), (QPalette.ToolTipBase, tooltip), (QPalette.Highlight, accent),
                        (QPalette.Link, accent), (QPalette.PlaceholderText, muted)):
        p.setColor(role, QColor(color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText, QPalette.ToolTipText):
        p.setColor(role, QColor(text))
        p.setColor(QPalette.Disabled, role, QColor(disabled))
    p.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
    return p


# --- Logs: Log Collector ---------------------------------------------------------------------

class LogsTab(EmbeddedTab):
    NAME = "Log Collector"
    SETTING = "logcollector_dir"
    DEFAULT = ROOT / "logCollector"
    NEEDS = ("logCollector.py", "logcollector")

    def __init__(self, user, registry=None) -> None:
        super().__init__(user)
        self.registry = registry               # LabHub's devices: names / serials from Mappings

    def _roles_from_mappings(self) -> dict[str, str]:
        """{ROLE: serial} for Log Collector from LabHub's Mappings (AP, CPE1 ... CPE16), devices on
        another PC included - instead of its own adb_device_id_* variables."""
        from ..core.models import DeviceConfig
        out = {}
        for n in self.registry.nodes():
            if isinstance(n.config, DeviceConfig):
                serial = self.registry.serial_of(n)
                if serial:
                    out[n.name.strip().upper()] = serial
        return out

    def _build(self, folder: Path):
        import importlib

        from logcollector.app_logging import setup_logging
        from logcollector.core.process import init_job_object
        from logcollector.ui import main_window as lc_main
        from logcollector.ui import theme as t

        for mod in ("logcollector.core.adb", "logcollector.tasks.base_task",
                    "logcollector.tasks.device_scan_task", "logcollector.ui.main_window"):
            m = importlib.import_module(mod)
            if hasattr(m, "find_adb"):
                m.find_adb = tr.find_adb       # exactly LabHub's adb (it only looks on PATH)
        if self.registry is not None:
            # the devices of LabHub's Mappings; its `adb devices` (all hosts, through LabHub) gives
            # connected / not; devices that are in no mapping are left out
            scan = importlib.import_module("logcollector.tasks.device_scan_task")
            orig_rows = scan.build_device_rows
            scan.load_role_map = self._roles_from_mappings
            scan.build_device_rows = lambda roles, connected: [
                r for r in orig_rows(roles, connected) if r[0] != "(unassigned)"]
        log_path = setup_logging(str(folder))
        init_job_object()                      # its child processes (adb, translator) end with LabHub
        lc_main.dark_title_bar = lambda _w: None   # a tab has no title bar of its own
        win = lc_main.MainWindow(log_path)
        # its own look on its page (LabHub's theme hides e.g. its checkboxes); Fusion is app-wide already
        win.setPalette(_palette(t._WINDOW, t._BASE, t._ALT, t._BUTTON, t._TEXT, t._DISABLED, t.COLOR_BLUE,
                                t._MUTED, "#232328"))
        win.setFont(QFont(t._FONT, 10))
        win.setStyleSheet(t._STYLESHEET)
        if self.registry is not None:
            for lab in win.findChildren(QLabel):
                if lab.text().startswith("Serials are read from environment variables"):
                    lab.setText("Devices from LabHub's Mappings (names, serials; devices on another PC too)")
        return win

    def _rescan(self) -> None:
        self.window.devices.refresh()


# --- FW/Wireless: DevReader -------------------------------------------------------------------

# DevReader's colour names -> LabHub's (night) colours of theme.qss; common.c() gives the day ones
_DR_COLORS = {
    "bg": "#16181d", "surface": "#1c1f26", "surface_alt": "#262a33", "border": "#2c3039",
    "text": "#d8dce4", "muted": "#8a90a0", "accent": "#2f6fed", "accent_hover": "#3d7cf5",
    "success": "#3ccf7a", "warning": "#f0b43c", "error": "#ff6b7a", "info": "#4aa3ff", "debug": "#5b6170",
}
# LabHub colours DevReader's stylesheet has no name for (used through _DR_RULES)
_DR_EXTRA = {"field": "#101216", "button_border": "#333844", "hover": "#2e3340", "hover_border": "#46506a",
             "tile": "#1f232b"}
# colours written as-is in DevReader's stylesheet
_DR_FIXED = {"#20232b": "#1a1d23", "#2a3b5e": "#253354", "#1a2333": "#1d2740",
             "#2b3d63": "#1e2128", "#8ea3c9": "#5b6170"}
# its rules (before .format) -> LabHub's sizes, fields, buttons and tiles
_DR_RULES = (
    ("font-size: 10pt", "font-size: 9pt"),
    ("QLabel#CardTitle {{ font-size: 12pt", "QLabel#CardTitle {{ font-size: 10.5pt"),
    ("background: {surface_alt}; border: 1px solid {border}; border-radius: 6px;\n    padding: 7px 14px; min-height: 18px;",
     "background: {surface_alt}; border: 1px solid {button_border}; border-radius: 6px;\n    padding: 5px 12px; min-height: 16px;"),
    ("QPushButton:hover {{ border-color: {accent}; }}",
     "QPushButton:hover {{ background: {hover}; border-color: {hover_border}; }}"),
    ("background: {bg}; border: 1px solid {border}; border-radius: 6px; padding: 6px 8px;",
     "background: {field}; border: 1px solid {border}; border-radius: 6px; padding: 4px 6px;"),
    ("QFrame#DeviceTile {{ background: {bg};", "QFrame#DeviceTile {{ background: {tile};"),
    ("QFrame#DeviceTile:hover {{ border-color: {accent_hover}; }}",
     "QFrame#DeviceTile:hover {{ border-color: {hover_border}; }}"),
)
# added after its rules: checkboxes that show when unticked (on a card the default box is nearly
# invisible), no white base line next to the config tabs, the status text not on the edge
_DR_AFTER = """
QCheckBox::indicator, QTableView::indicator {{
    border: 1px solid {hover_border}; border-radius: 3px; background: {field}; width: 14px; height: 14px; }}
QCheckBox::indicator:hover, QTableView::indicator:hover {{ border-color: {accent}; }}
QCheckBox::indicator:checked, QTableView::indicator:checked {{
    background: {accent}; border-color: {accent}; image: url({check}); }}
QCheckBox::indicator:disabled {{ border-color: {border}; }}
QTabBar {{ qproperty-drawBase: 0; }}
QTabWidget::pane {{ border-top: 1px solid {border}; }}
QStatusBar QLabel {{ padding-left: 6px; }}
QStatusBar::item {{ border: none; }}
"""


def _check_mark() -> str:
    """A white tick for the ticked checkboxes (a stylesheet image must be a file)."""
    import tempfile
    path = Path(tempfile.gettempdir()) / "labhub_check.svg"
    if not path.exists():
        path.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 14 14">'
                        '<path d="M3 7.2l2.6 2.6L11 4.4" fill="none" stroke="#ffffff" stroke-width="2" '
                        'stroke-linecap="round" stroke-linejoin="round"/></svg>', encoding="utf-8")
    return path.as_posix()


class FwWirelessTab(EmbeddedTab):
    NAME = "DevReader"
    SETTING = "devreader_dir"
    DEFAULT = ROOT / "DevReader"
    NEEDS = ("run_devreader.py", "devreader")

    def __init__(self, user, registry=None) -> None:
        super().__init__(user)
        self.registry = registry               # LabHub's devices: names / serials from Mappings

    def _discovery(self, adb, log):
        """DevReader's device list = LabHub's: names and serials from Mappings, connected or not as
        on LabHub's tiles (devices on another PC too). Not its own adb_device_id_* discovery."""
        from devreader.core.adb_service import DeviceState
        from devreader.core.discovery import Device, DiscoveryResult, DiscoveryService

        from ..core.models import DeviceConfig, Status
        registry = self.registry
        to_state = {Status.CONNECTED: DeviceState.CONNECTED, Status.UNAUTHORIZED: DeviceState.UNAUTHORIZED,
                    Status.OFFLINE: DeviceState.OFFLINE}

        class FromMappings(DiscoveryService):
            def discover(self) -> DiscoveryResult:
                result = DiscoveryResult()
                for n in registry.nodes():     # display order: AP, CPE1, CPE2 ... CPE10
                    if not isinstance(n.config, DeviceConfig):
                        continue
                    serial = registry.serial_of(n)
                    if not serial:
                        result.warnings.append(f"{n.name}: no serial in Mappings")
                        continue
                    host = n.config.extra.get("host")
                    dev = Device(name=n.name, serial=serial,
                                 env_var="LabHub Mappings" + (f" (on {host})" if host else ""),
                                 state=to_state.get(n.state.status, DeviceState.DISCONNECTED))
                    log.set_alias(serial, n.name)
                    result.devices.append(dev)
                if not result.devices:
                    result.warnings.append("No devices in LabHub's Mappings")
                log.info("Devices from LabHub: " + (", ".join(f"{d.name}={d.status.value}" for d in result.devices)
                                                    or "none"))
                return result

        return FromMappings(adb, log) if registry is not None else DiscoveryService(adb, log)

    def _build(self, folder: Path):
        from datetime import datetime

        from PySide6.QtCore import QSettings

        from devreader import __app_name__, __org_name__
        from devreader.config.service import ConfigService
        from devreader.core.adb_service import AdbService
        from devreader.core.discovery import DiscoveryService
        from devreader.core.logging_service import LogService
        from devreader.core.paths import backups_dir, logs_dir
        from devreader.core.preparation import PreparationService
        from devreader.gui import main_window as dr_main
        from devreader.gui import theme as t

        # DevReader's QSettings() would read LabHub's (they follow the running app's name): its own
        settings = lambda *a, **k: QSettings(*a, **k) if (a or k) else QSettings(__org_name__, __app_name__)
        dr_main.QSettings = settings
        self._patch_tiles()
        log = LogService(logs_dir() / f"devreader_{datetime.now():%Y%m%d}.log")
        log.info(f"{__app_name__} started inside LabHub")
        adb = AdbService(log, adb_path=tr.find_adb() or settings().value("adb_path", "") or None)
        win = dr_main.MainWindow(log=log, adb=adb, discovery=self._discovery(adb, log),
                                 preparation=PreparationService(adb, log),
                                 config=ConfigService(adb, log, backup_dir=backups_dir()), simulated=False)
        self._trim(win)
        self._style(win)
        return win

    def theme_changed(self) -> None:
        """LabHub switched Day / Night: DevReader's page follows."""
        if self.window is not None:
            self._style(self.window)

    @staticmethod
    def _style(win) -> None:
        """LabHub's look on DevReader's page: LabHub's colours (Day or Night), font and spacing,
        with DevReader's own rules (tiles, badges, toasts, edited cells) kept."""
        from devreader.gui import device_panel as dp
        from devreader.gui import log_panel as lp
        from devreader.gui import theme as t

        from .common import c as themed
        new = {k: themed(v) for k, v in _DR_COLORS.items()}
        # its code reads COLORS (and two tables made from it) for status / log colours: same dict, new values
        swap = {t.COLORS[k].lower(): v for k, v in new.items()}
        for table in (dp.STATUS_DISPLAY, lp.LEVEL_COLORS):
            for key, val in table.items():
                if isinstance(val, tuple):
                    table[key] = val[:-1] + (swap.get(val[-1].lower(), val[-1]),)
                else:
                    table[key] = swap.get(val.lower(), val)
        t.COLORS.update(new)

        qss = t.STYLESHEET
        for old, rule in _DR_RULES:
            qss = qss.replace(old, rule)
        qss = qss.format(**new, **{k: themed(v) for k, v in _DR_EXTRA.items()})
        for old, night in _DR_FIXED.items():
            qss = qss.replace(old, themed(night))
        # LabHub's `QWidget { background }` would paint a box behind every label on DevReader's cards
        qss = "QWidget { background: transparent; }\n" + qss + _DR_AFTER.format(
            check=_check_mark(), **new, **{k: themed(v) for k, v in _DR_EXTRA.items()})
        win.setPalette(_palette(new["bg"], themed("#101216"), themed("#1a1d23"), new["surface_alt"], new["text"],
                                new["debug"], new["accent"], new["debug"], themed("#2a2e38")))
        win.setFont(QFont("Segoe UI", 9))
        win.setStyleSheet(qss)
        win.device_panel._refresh_tiles()      # their colours are set in code, not in the stylesheet

    @staticmethod
    def _patch_tiles() -> None:
        """Device cards inside LabHub: name + checkbox, progress and the whole last result
        (what failed and where); serial and connection are on LabHub's tiles at the top."""
        from devreader.gui import device_panel as dp
        if getattr(dp.DeviceTile, "_labhub", False):
            return
        orig_init, orig_update = dp.DeviceTile.__init__, dp.DeviceTile.update_from

        def init(self, *a, **k):
            orig_init(self, *a, **k)
            for w in (self.serial_label, self.status, self.dot):
                w.hide()
            self.action.setWordWrap(True)
            self.layout().addStretch(1)                    # name and result at the top of the card

        def update_from(self, *a, **k):
            orig_update(self, *a, **k)
            self.action.setText(self.action.toolTip())     # in full, wrapped (not cut off)

        orig_refresh = dp.DevicePanel._refresh_tiles

        def refresh_tiles(self, *a, **k):
            # the strip is as tall as its tallest card (a long, wrapped result fits; no devices: small)
            orig_refresh(self, *a, **k)
            lay = [t.layout() for t in self.tiles.values()]
            h = max([lt.totalHeightForWidth(dp.TILE_WIDTH) if lt.hasHeightForWidth() else lt.totalSizeHint().height()
                     for lt in lay] or [0])
            self.scroll.setFixedHeight(min(max(h, 40) + 14, 220) if lay else 0)
            self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded if h + 14 > 220 else Qt.ScrollBarAlwaysOff)

        dp.DeviceTile.__init__, dp.DeviceTile.update_from = init, update_from
        dp.DevicePanel._refresh_tiles = refresh_tiles
        dp.DeviceTile._labhub = True

    @staticmethod
    def _trim(win) -> None:
        """No menu bar / app title (LabHub has its own), no Prepare (it runs by itself) and no
        Refresh (the devices come from LabHub; the tab rescans when they change)."""
        win.menuBar().hide()
        for lab in win.findChildren(QLabel):
            if lab.text() == "DevReader" or lab.objectName() == "AppSubtitle" or lab is getattr(win, "adb_label", None):
                lab.hide()
        panel = win.device_panel
        for b in (panel.prepare_btn, panel.refresh_btn):
            b.hide()
        if hasattr(panel, "set_subtitle"):
            panel.set_subtitle("From LabHub's Mappings (names, serials; connected as on the tiles above)")
        # LabHub's spacing: the page sits flush in the tab, cards a little tighter (its header row is empty)
        from devreader.gui.widgets import Card
        outer = win.centralWidget().layout()
        outer.setContentsMargins(10, 8, 10, 6)
        outer.setSpacing(8)
        for card in win.findChildren(Card):
            card.layout().setContentsMargins(12, 10, 12, 10)
            card.layout().setSpacing(8)
        win.statusBar().setSizeGripEnabled(False)   # the tab is not a window that resizes
        # "No devices configured - set adb_device_id_* variables" does not apply (the devices come
        # from Mappings, and the card already says when there are none); the same toast twice is noise
        show = win.toasts.show
        shown: dict[str, float] = {}

        def toast(kind, title, message="", *a, **k):
            import time
            if title == "No devices configured":
                return
            if time.monotonic() - shown.get(title + message, -99) < 5:
                return
            shown[title + message] = time.monotonic()
            show(kind, title, message, *a, **k)

        win.toasts.show = toast

    def _rescan(self) -> None:
        # its own Refresh (cards: Connected / Not connected); not while it reads / writes a device
        if self.window.runner.busy:
            QTimer.singleShot(3000, self.devices_changed)
            return
        self.window.refresh()
