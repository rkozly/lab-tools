"""
Main window: puts the panels together, owns the status bar and handles
clean shutdown.

Data flow:
    DevicesPanel  -> roles_changed     -> CNSS / CONGCTRL / Serial / WPA / hostapd / PKTLOG
                  -> serial_for(role)  -> used when a collection starts
    PKTLOG groups -> capture_completed -> translation file list
    every panel   -> status_message    -> status bar
"""

import os

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import (QLabel, QMainWindow, QMessageBox, QScrollArea,
                               QStatusBar, QVBoxLayout, QWidget)

from .. import __version__
from ..app_logging import get_logger
from ..config import APP_NAME, DEFAULT_OUTPUT_DIR, OLD_DEFAULT_OUTPUT_DIR, ORG_NAME
from ..core import process
from ..core.adb import find_adb
from ..tasks.base_task import BaseTask, State
from .cnss_panel import CnssPanel, CongctrlPanel
from .daemon_panel import HostapdPanel, WpaPanel
from .devices_panel import DevicesPanel
from .pktlog_panel import PktlogPanel
from .serial_panel import SerialPanel
from .theme import COLOR_GRAY, dark_title_bar
from .widgets import StatusLabel

log = get_logger("ui")


class MainWindow(QMainWindow):
    def __init__(self, log_path: str):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1250, 900)
        self.settings = QSettings(ORG_NAME, APP_NAME)
        self._migrate_settings()

        # --- panels ---
        self.devices = DevicesPanel()
        self.cnss = CnssPanel(self.devices, self.settings)
        self.congctrl = CongctrlPanel(self.devices, self.settings)
        self.pktlog = PktlogPanel(self.devices, self.settings)
        self.serial = SerialPanel(self.devices, self.settings)
        self.wpa = WpaPanel(self.devices, self.settings)
        self.hostapd = HostapdPanel(self.devices, self.settings)
        self.translate = self.pktlog.translate

        for panel in (self.devices, self.cnss, self.congctrl, self.serial, self.wpa,
                      self.hostapd, self.translate,
                      *self.pktlog.groups):
            panel.status_message.connect(self.set_status)

        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(16, 4, 16, 16)
        lay.setSpacing(6)
        lay.addWidget(self.devices)
        lay.addWidget(self.cnss)
        lay.addWidget(self.congctrl)
        lay.addWidget(self.serial)
        lay.addWidget(self.wpa)
        lay.addWidget(self.hostapd)
        lay.addWidget(self.pktlog)
        lay.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(body)
        self.setCentralWidget(scroll)

        # --- status bar ---
        self.status = StatusLabel()
        bar = QStatusBar()
        bar.addWidget(self.status, 1)
        log_label = QLabel(f"Log: {log_path}")
        log_label.setStyleSheet(f"color: {COLOR_GRAY};")
        bar.addPermanentWidget(log_label)
        self.setStatusBar(bar)

        dark_title_bar(self)
        self._load_settings()
        # Start background work after the window is shown.
        QTimer.singleShot(0, self._startup)

    def _startup(self):
        if not find_adb():
            log.error("adb not found in PATH")
            self.set_status(State.ERROR, "ADB not found")
            QMessageBox.critical(
                self, APP_NAME,
                "ADB is not installed or not in PATH.\n\n"
                "Install Android SDK platform-tools and add the folder that "
                "contains adb.exe to PATH, then restart the application.\n\n"
                "PKTLOG translation is still available.")
        self.devices.refresh()
        self.translate.detect_crm()

    def set_status(self, state: str, message: str):
        self.status.set_state(state, message)

    # ----- settings ---------------------------------------------------------

    def _migrate_settings(self):
        """Point saved paths from the old per-user default folder to the new one.

        Only paths inside the old default folder are changed; anything the
        user picked by hand somewhere else stays as it is.
        """
        old = OLD_DEFAULT_OUTPUT_DIR
        if os.path.normcase(old) == os.path.normcase(DEFAULT_OUTPUT_DIR):
            return
        for key in self.settings.allKeys():
            if not key.endswith(("/path", "/dest")):
                continue
            value = str(self.settings.value(key, ""))
            prefix, rest = value[:len(old)], value[len(old):]
            if os.path.normcase(prefix) == os.path.normcase(old) and rest[:1] in ("", "\\"):
                new = DEFAULT_OUTPUT_DIR + rest
                self.settings.setValue(key, new)
                log.info("Settings: %s moved to %s", key, new)

    def _load_settings(self):
        # Device-dependent rows read their saved paths when they are created
        # (after each device scan); only window state and the translation
        # list are restored here.
        geo = self.settings.value("window/geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        self.translate.load_settings(self.settings)

    def _save_settings(self):
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.cnss.save_settings(self.settings)
        self.congctrl.save_settings(self.settings)
        self.serial.save_settings(self.settings)
        self.wpa.save_settings(self.settings)
        self.hostapd.save_settings(self.settings)
        self.pktlog.save_settings(self.settings)

    # ----- shutdown ---------------------------------------------------------

    def closeEvent(self, event):
        # Background scans are not worth asking about; only real work is.
        busy = [t for t in BaseTask.active_tasks()
                if t.name not in ("Device scan", "CRM scan")
                and not t.name.endswith(" check")]
        if busy:
            names = "\n".join(f"  {t.name} ({t.device})" for t in busy)
            answer = QMessageBox.question(
                self, APP_NAME,
                f"The following jobs are still running:\n{names}\n\n"
                "Stop them and exit?")
            if answer != QMessageBox.Yes:
                event.ignore()
                return

        self.set_status(State.STOPPING, "Stopping...")
        self._save_settings()
        log.info("Shutting down: %d task(s), %d process(es)",
                 len(BaseTask.active_tasks()), process.running_count())
        # 1. ask every task to stop (kills its current command)
        for t in list(BaseTask._active):
            t.stop()
        # 2. kill anything still running
        process.kill_all()
        # 3. wait for worker threads to finish their cleanup
        BaseTask.stop_all(wait_ms=5000)
        log.info("Shutdown complete")
        event.accept()
