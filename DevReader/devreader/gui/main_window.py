"""Main application window: wires panels to services via background tasks."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from devreader import __app_name__, __version__
from devreader.config.definitions import TABLES
from devreader.config.replace import FileReplaceService, FileSummary, ReplaceResult, validate_remote_path
from devreader.config.service import ConfigService
from devreader.config.table import AP_NAME, TableDef, TableRead, read_table, write_table
from devreader.core.adb_service import AdbService
from devreader.core.discovery import ENV_PREFIX, Device, DiscoveryResult, DiscoveryService
from devreader.core.errors import CancelledError, ConfigValidationError, DevReaderError
from devreader.core.firmware import (
    VER_INFO_PATH, WLAN_FILES, WLAN_TARGET_DIR, FirmwareService, FlashResult, missing_files, role_of,
)
from devreader.core.logging_service import LogService
from devreader.core.paths import app_data_dir, backups_dir
from devreader.core.preparation import PrepareReport, PreparationService
from devreader.gui.config_panel import ConfigPanel
from devreader.gui.device_panel import DevicePanel
from devreader.gui.widgets import ToastManager
from devreader.gui.workers import TaskContext, TaskRunner


class MainWindow(QMainWindow):
    def __init__(
        self,
        log: LogService,
        adb: AdbService,
        discovery: DiscoveryService,
        preparation: PreparationService,
        config: ConfigService,
        *,
        simulated: bool = False,
    ) -> None:
        super().__init__()
        self.log, self.adb, self.discovery = log, adb, discovery
        self.preparation, self.config_service = preparation, config
        self.replace_service = FileReplaceService(adb, log, backup_dir=backups_dir())
        self.firmware_service = FirmwareService(adb, log, backup_dir=backups_dir())
        self.simulated = simulated
        self._server_started = False
        self.settings = QSettings()
        self.runner = TaskRunner(self)
        self.runner.busy_changed.connect(self._on_busy_changed)

        title = f"{__app_name__} {__version__}" + ("  [SIMULATION]" if simulated else "")
        self.setWindowTitle(title)
        self.resize(1280, 900)
        self.setMinimumSize(900, 640)

        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(18, 14, 18, 10)
        outer.setSpacing(12)
        outer.addLayout(self._build_header())

        self.device_panel = DevicePanel()
        self.config_panel = ConfigPanel(TABLES)

        # Devices on top (compact), configuration tables below.
        self.v_split = QSplitter(Qt.Vertical)
        self.v_split.addWidget(self.device_panel)
        self.v_split.addWidget(self.config_panel)
        self.v_split.setStretchFactor(0, 1)
        self.v_split.setStretchFactor(1, 3)
        self.v_split.setChildrenCollapsible(False)
        outer.addWidget(self.v_split, 1)

        self._build_status_bar()
        self._build_menu()
        self.toasts = ToastManager(root)

        # Signals
        self.device_panel.refresh_requested.connect(self.refresh)
        self.device_panel.prepare_requested.connect(self.prepare_selected)
        self.device_panel.cancel_requested.connect(self.cancel)
        self.device_panel.selection_changed.connect(self._sync_config_devices)
        for panel in self.config_panel.tables.values():
            panel.read_requested.connect(self.read_table)
            panel.write_requested.connect(self.write_table)
            panel.flash_requested.connect(self.flash_wlan)
        self.config_panel.replace_panel.read_requested.connect(self.read_replace_values)
        self.config_panel.replace_panel.replace_requested.connect(self.replace_file)

        self._restore_state()
        QTimer.singleShot(0, self.refresh)

    # ------------------------------------------------------------------
    # Layout helpers
    # ------------------------------------------------------------------
    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(0)
        t = QLabel("DevReader")
        t.setObjectName("AppTitle")
        s = QLabel("ADB device discovery • preparation • configuration")
        s.setObjectName("AppSubtitle")
        titles.addWidget(t)
        titles.addWidget(s)
        row.addLayout(titles)
        row.addStretch(1)
        self.adb_label = QLabel("")
        self.adb_label.setObjectName("Muted")
        self.adb_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row.addWidget(self.adb_label, 0, Qt.AlignBottom)
        return row

    def _build_status_bar(self) -> None:
        sb = self.statusBar()
        self.status_text = QLabel("Ready")
        sb.addWidget(self.status_text, 1)
        self.busy_bar = QProgressBar()
        self.busy_bar.setRange(0, 0)
        self.busy_bar.setFixedWidth(160)
        self.busy_bar.setFixedHeight(10)
        self.busy_bar.setTextVisible(False)
        self.busy_bar.hide()
        sb.addPermanentWidget(self.busy_bar)

    def _build_menu(self) -> None:
        mb = self.menuBar()
        file_menu = mb.addMenu("&File")
        file_menu.addAction(self._action("Open log folder", self._open_log_folder))
        file_menu.addAction(self._action("Open backups folder", self._open_backups_folder))
        file_menu.addSeparator()
        file_menu.addAction(self._action("Exit", self.close, QKeySequence.Quit))

        dev_menu = mb.addMenu("&Devices")
        dev_menu.addAction(self._action("Refresh", self.refresh, QKeySequence.Refresh))
        dev_menu.addAction(self._action("Prepare selected", self.prepare_selected, QKeySequence("Ctrl+P")))
        dev_menu.addAction(self._action("Cancel running operations", self.cancel, QKeySequence("Esc")))

        settings = mb.addMenu("&Settings")
        settings.addAction(self._action("Set adb path…", self._choose_adb))
        settings.addAction(self._action("Use adb from PATH", lambda: self._set_adb(None)))

        help_menu = mb.addMenu("&Help")
        help_menu.addAction(self._action("About", self._about))

    def _action(self, text, slot, shortcut=None) -> QAction:
        act = QAction(text, self)
        act.triggered.connect(slot)
        if shortcut is not None:
            act.setShortcut(shortcut)
        return act

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    def refresh(self) -> None:
        self._status("Discovering devices…")
        self.device_panel.refresh_btn.setEnabled(False)

        def task(ctx: TaskContext):
            info = ""
            if self.adb.is_available():
                if not self._server_started:
                    # Start the daemon without pipes first; see AdbService.start_server.
                    self.adb.start_server()
                    self._server_started = True
                try:
                    info = f"{self.adb.version()} • {self.adb.adb_path}"
                except DevReaderError:
                    info = self.adb.adb_path
            return self.discovery.discover(), info

        self.runner.submit(task, on_result=self._on_discovered, on_error=self._on_task_error)

    def _on_discovered(self, payload) -> None:
        result, adb_info = payload  # type: DiscoveryResult, str
        self.device_panel.set_devices(result.devices, result.warnings)
        self._sync_config_devices()
        self.adb_label.setText(adb_info or "⚠ adb not found")
        connected = sum(d.is_ready for d in result.devices)
        configured = sum(d.configured for d in result.devices)
        self._status(f"{configured} configured device(s), {connected} connected")
        if not result.adb_available:
            self.toasts.show("error", "ADB not available", result.warnings[-1] if result.warnings else "")
        elif configured == 0:
            self.toasts.show("warning", "No devices configured",
                             f"Set {ENV_PREFIX}<name>=<serial> environment variables, then Refresh.")

    def _sync_config_devices(self) -> None:
        self.config_panel.set_devices(self.device_panel.model.devices(), self.device_panel.checked_devices())

    # ------------------------------------------------------------------
    # Preparation
    # ------------------------------------------------------------------
    def prepare_selected(self) -> None:
        devices = self.device_panel.checked_devices()
        if not devices:
            self.toasts.show("warning", "No devices selected", "Tick the devices to prepare.")
            return
        not_ready = [d for d in devices if not d.is_ready]
        names = ", ".join(d.name for d in devices)
        msg = (f"Prepare {len(devices)} device(s): {names}?\n\n"
               "This runs root, disable-verity, REBOOT, root, remount, setenforce 0 and "
               "mount -o rw,remount / on each device.")
        if not_ready:
            msg += ("\n\n⚠ Not currently connected: " + ", ".join(f"{d.name} ({d.status.value})" for d in not_ready)
                    + " - these will fail fast.")
        if QMessageBox.question(self, "Prepare devices", msg) != QMessageBox.Yes:
            return

        self._pending_prepare = {d.serial for d in devices}
        self._prepare_results: list[PrepareReport] = []
        total = len(self.preparation.steps)
        for dev in devices:
            self.device_panel.model.set_progress(dev.serial, 0, total, "Queued")
            self.device_panel.model.set_last_action(dev.serial, "Preparing…", None)
            serial = dev.serial

            def task(ctx: TaskContext, serial=serial):
                return self.preparation.prepare(
                    serial,
                    cancel=ctx.cancel,
                    on_progress=lambda s, i, n, title: ctx.progress(s, i, n, f"{min(i + 1, n)}/{n}  {title}"
                                                                       if i < n else "Done"),
                )

            self.runner.submit(
                task,
                on_result=self._on_prepared,
                on_error=lambda e, tb, s=serial: self._on_prepare_error(s, e, tb),
                on_progress=lambda k, v, m, t: self.device_panel.model.set_progress(k, v, m, t),
            )
        self._status(f"Preparing {len(devices)} device(s)…")

    def _on_prepared(self, report: PrepareReport) -> None:
        model = self.device_panel.model
        model.clear_progress(report.serial)
        model.set_last_action(report.serial, report.summary, report.ok)
        self._prepare_results.append(report)
        self._finish_prepare(report.serial)

    def _on_prepare_error(self, serial: str, exc: BaseException, tb: str) -> None:
        self.device_panel.model.clear_progress(serial)
        self.device_panel.model.set_last_action(serial, f"Prepare failed: {exc}", False)
        self.log.error(f"Unexpected error during preparation: {exc}", device=serial, error=tb)
        self._prepare_results.append(PrepareReport(serial, [], False, error=str(exc)))
        self._finish_prepare(serial)

    def _finish_prepare(self, serial: str) -> None:
        self._pending_prepare.discard(serial)
        if self._pending_prepare:
            return
        ok = [r for r in self._prepare_results if r.ok]
        failed = [r for r in self._prepare_results if not r.ok]
        name = lambda r: (self.device_panel.model.device(r.serial).name  # noqa: E731
                          if self.device_panel.model.device(r.serial) else r.serial)
        if failed:
            self.toasts.show("error", f"Preparation: {len(ok)} ok, {len(failed)} failed",
                             "\n".join(f"{name(r)}: {r.error}" for r in failed), msecs=8000)
        else:
            self.toasts.show("success", "Preparation complete",
                             ", ".join(f"{name(r)}: {r.summary}" for r in ok))
        self._status("Preparation finished")
        QTimer.singleShot(500, self.refresh)

    # ------------------------------------------------------------------
    # Configuration tables
    # ------------------------------------------------------------------
    def read_table(self, table: TableDef, devices: list[Device]) -> None:
        panel = self.config_panel.table(table.id)
        if not devices:
            self.toasts.show("warning", "No connected devices", "Connect a device, then Refresh.")
            return
        pending = {d.serial for d in devices}
        failures: list[str] = []
        panel.set_summary(f"Reading {table.remote_path}…", "info")

        def finish(serial: str) -> None:
            pending.discard(serial)
            if pending:
                return
            ok = len(devices) - len(failures)
            if failures:
                panel.set_summary(f"Read: {ok} ok, {len(failures)} failed", "error")
                self.toasts.show("error", f"Read failed on {len(failures)} device(s)", "\n".join(failures),
                                 msecs=8000)
            else:
                panel.set_summary(f"Read from {ok} device(s)", "success")
            self._status(f"{table.remote_path} read from {ok} device(s)")

        for dev in devices:
            panel.set_row_status(dev.serial, "Reading…", None)
            self.device_panel.model.set_busy(dev.serial, True)

            def task(ctx: TaskContext, dev=dev):
                return read_table(self.config_service, dev.serial, table,
                                  is_ap=dev.name == AP_NAME, cancel=ctx.cancel)

            def done(result: TableRead, dev=dev):
                panel.set_read(result)
                self.device_panel.model.set_last_action(dev.serial, f"Read {table.remote_path}: OK", True)
                finish(dev.serial)

            def failed(exc, tb, dev=dev):
                panel.set_row_status(dev.serial, f"Read failed: {exc}", False)
                self.device_panel.model.set_last_action(dev.serial, f"Read failed: {exc}", False)
                failures.append(f"{dev.name}: {exc}")
                self._on_task_error(exc, tb, title=None, device=dev.serial)
                finish(dev.serial)

            self.runner.submit(task, on_result=done, on_error=failed,
                               on_finished=lambda s=dev.serial: self.device_panel.model.set_busy(s, False))
        self._status(f"Reading {table.remote_path} from {len(devices)} device(s)…")

    def write_table(self, table: TableDef, edits) -> None:
        panel = self.config_panel.table(table.id)
        if isinstance(edits, ConfigValidationError):
            self.toasts.show("error", "Invalid values", str(edits), msecs=8000)
            self.log.error(f"Validation failed: {edits}")
            return
        if not edits:
            self.toasts.show("info", "Nothing to write", "No value has been modified.")
            return
        if QMessageBox.question(
            self, "Write configuration",
            f"Write these changes to {table.remote_path}?\n\n{panel.describe(edits)}\n\n"
            "Only these options are modified; the rest of each file is preserved byte-for-byte.",
        ) != QMessageBox.Yes:
            return

        devices = {d.serial: d for d in self.device_panel.model.devices()}
        pending = set(edits)
        failures: list[str] = []

        def finish(serial: str) -> None:
            pending.discard(serial)
            if pending:
                return
            if failures:
                panel.set_summary("Write failed", "error")
                self.toasts.show("error", "Write failed", "\n".join(failures), msecs=9000)
                self._status("Write failed")
            else:
                panel.set_summary(f"Written & verified on {len(edits)} device(s)", "success")
                self.toasts.show("success", "Write successful", "Values written and verified.")
                self._status(f"{table.remote_path} written and verified")

        for serial, values in edits.items():
            dev = devices.get(serial)
            name = dev.name if dev else serial
            panel.set_row_status(serial, "Writing…", None)
            self.device_panel.model.set_busy(serial, True)

            def task(ctx: TaskContext, serial=serial, values=values, is_ap=name == AP_NAME):
                write_table(self.config_service, serial, table, values, cancel=ctx.cancel)
                return read_table(self.config_service, serial, table, is_ap=is_ap, cancel=ctx.cancel)

            def done(result: TableRead, serial=serial):
                panel.set_read(result)
                panel.set_row_status(serial, "Written & verified", True)
                self.device_panel.model.set_last_action(serial, f"Write {table.remote_path}: verified", True)
                finish(serial)

            def failed(exc, tb, serial=serial, name=name):
                panel.set_row_status(serial, f"Write failed: {exc}", False)
                self.device_panel.model.set_last_action(serial, f"Write failed: {exc}", False)
                failures.append(f"{name}: {exc}")
                self._on_task_error(exc, tb, title=None, device=serial)
                finish(serial)

            self.runner.submit(task, on_result=done, on_error=failed,
                               on_finished=lambda s=serial: self.device_panel.model.set_busy(s, False))
        self._status(f"Writing {table.remote_path}…")

    # ------------------------------------------------------------------
    # Replace file
    # ------------------------------------------------------------------
    def read_replace_values(self, remote_path: str, devices: list[Device]) -> None:
        panel = self.config_panel.replace_panel
        try:
            remote_path = validate_remote_path(remote_path)
        except ConfigValidationError as exc:
            self.toasts.show("error", "Invalid path", str(exc))
            return
        if not devices:
            self.toasts.show("warning", "No connected devices", "Connect a device, then Refresh.")
            return
        pending = {d.serial for d in devices}
        failures: list[str] = []
        panel.set_summary(f"Reading {remote_path}…", "info")

        def finish(serial: str) -> None:
            pending.discard(serial)
            if pending:
                return
            if failures:
                panel.set_summary(f"Read: {len(devices) - len(failures)} ok, {len(failures)} failed", "error")
            else:
                panel.set_summary(f"Read from {len(devices)} device(s)", "success")
            self._status(f"{remote_path} read from {len(devices) - len(failures)} device(s)")

        for dev in devices:
            panel.set_values(dev.serial, None, "Reading…", None)
            self.device_panel.model.set_busy(dev.serial, True)

            def task(ctx: TaskContext, serial=dev.serial):
                return self.replace_service.read_summary(serial, remote_path, cancel=ctx.cancel)

            def done(result: FileSummary, dev=dev):
                panel.set_values(dev.serial, result.values, "Read OK", True)
                finish(dev.serial)

            def failed(exc, tb, dev=dev):
                panel.set_values(dev.serial, None, f"Read failed: {exc}", False)
                failures.append(dev.name)
                self._on_task_error(exc, tb, title=None, device=dev.serial)
                finish(dev.serial)

            self.runner.submit(task, on_result=done, on_error=failed,
                               on_finished=lambda s=dev.serial: self.device_panel.model.set_busy(s, False))
        self._status(f"Reading {remote_path} from {len(devices)} device(s)…")

    # ------------------------------------------------------------------
    # Flash wlan_wkk
    # ------------------------------------------------------------------
    def flash_wlan(self, table: TableDef) -> None:
        targets = [d for d in self.device_panel.checked_devices() if d.is_ready]
        if not targets:
            self.toasts.show("warning", "No devices selected", "Tick the connected devices to flash.")
            return
        start = self.settings.value("flash/wlan_folder", "") or ""
        folder = QFileDialog.getExistingDirectory(
            self, "Choose the WLAN build folder (e.g. WLAN.SDX_MRR.1.0-...)", start)
        if not folder:
            return
        folder_path = Path(folder)
        self.settings.setValue("flash/wlan_folder", str(folder_path.parent))

        problems = []
        for role in sorted({role_of(d.name == AP_NAME) for d in targets}):
            missing = missing_files(folder_path, role)
            if missing:
                problems.append(f"{role.upper()} images missing: {', '.join(missing)}")
        if problems:
            QMessageBox.warning(self, "Flash wlan_wkk", f"{folder_path}\n\n" + "\n".join(problems))
            return

        lines = [f"  {d.name}: {', '.join(WLAN_FILES[role_of(d.name == AP_NAME)])}" for d in targets]
        if QMessageBox.question(
            self, "Flash wlan_wkk",
            f"Flash WLAN firmware from\n{folder_path}\n\nto {WLAN_TARGET_DIR} on:\n" + "\n".join(lines)
            + "\n\nEach device runs root, setenforce 0, mount -o remount,rw /firmware, "
              f"pushes the files, sets wlan_wkk in {VER_INFO_PATH} to\n{folder_path.name}\n"
              "(backup saved first), runs mount -o remount,ro /firmware and then REBOOTS.",
        ) != QMessageBox.Yes:
            return

        panel = self.config_panel.table(table.id)
        pending = {d.serial for d in targets}
        failures: list[str] = []

        def finish(serial: str) -> None:
            pending.discard(serial)
            if pending:
                return
            if failures:
                panel.set_summary("Flash failed", "error")
                self.toasts.show("error", "Flash failed", "\n".join(failures), msecs=9000)
                self._status("Flash failed")
            else:
                panel.set_summary(f"Flashed {len(targets)} device(s) - rebooting", "success")
                self.toasts.show("success", "wlan_wkk flashed",
                                 f"{folder_path.name} pushed to {', '.join(d.name for d in targets)}. "
                                 "Devices are rebooting - press Read All when they are back.")
                self._status("wlan_wkk flashed - devices rebooting")
            QTimer.singleShot(5000, self.refresh)

        for dev in targets:
            panel.set_row_status(dev.serial, "Flashing…", None)
            self.device_panel.model.set_busy(dev.serial, True)

            def task(ctx: TaskContext, dev=dev):
                return self.firmware_service.flash_wlan(
                    dev.serial, folder_path, is_ap=dev.name == AP_NAME, cancel=ctx.cancel,
                    on_progress=lambda s, i, n, title: ctx.progress(s, i, n, f"{min(i + 1, n)}/{n}  {title}"),
                )

            def done(result: FlashResult, dev=dev):
                panel.set_row_status(dev.serial, f"Flashed {folder_path.name} - rebooting", True)
                self.device_panel.model.set_last_action(dev.serial, f"Flashed {folder_path.name}", True)
                finish(dev.serial)

            def failed(exc, tb, dev=dev):
                panel.set_row_status(dev.serial, f"Flash failed: {exc}", False)
                self.device_panel.model.set_last_action(dev.serial, f"Flash failed: {exc}", False)
                failures.append(f"{dev.name}: {exc}")
                self._on_task_error(exc, tb, title=None, device=dev.serial)
                finish(dev.serial)

            def cleanup(s=dev.serial):
                self.device_panel.model.set_busy(s, False)
                self.device_panel.model.clear_progress(s)

            self.runner.submit(task, on_result=done, on_error=failed,
                               on_progress=lambda k, v, m, t: self.device_panel.model.set_progress(k, v, m, t),
                               on_finished=cleanup)
        self._status(f"Flashing wlan_wkk on {len(targets)} device(s)…")

    def replace_file(self, remote_path: str, local_path: str, targets: list[Device]) -> None:
        panel = self.config_panel.replace_panel
        try:
            remote_path = validate_remote_path(remote_path)
            data = Path(local_path).read_bytes()
        except ConfigValidationError as exc:
            self.toasts.show("error", "Invalid path", str(exc))
            return
        except OSError as exc:
            self.toasts.show("error", "Cannot read local file", str(exc))
            return
        if not targets:
            self.toasts.show("warning", "No devices selected", "Tick the connected devices to update.")
            return
        names = ", ".join(d.name for d in targets)
        if QMessageBox.question(
            self, "Replace file",
            f"Replace {remote_path} on {names}\nwith {local_path} ({len(data)} bytes)?\n\n"
            "The whole file is overwritten. The current device file is backed up locally first.",
        ) != QMessageBox.Yes:
            return

        pending = {d.serial for d in targets}
        failures: list[str] = []

        def finish(serial: str) -> None:
            pending.discard(serial)
            if pending:
                return
            if failures:
                panel.set_summary("Replace failed", "error")
                self.toasts.show("error", "Replace failed", "\n".join(failures), msecs=9000)
                self._status("Replace failed")
            else:
                panel.set_summary(f"Replaced on {len(targets)} device(s)", "success")
                self.toasts.show("success", "File replaced", f"{remote_path} replaced and verified on {names}.")
                self._status(f"{remote_path} replaced and verified")

        for dev in targets:
            panel.set_values(dev.serial, None, "Replacing…", None)
            self.device_panel.model.set_busy(dev.serial, True)

            def task(ctx: TaskContext, serial=dev.serial):
                return self.replace_service.replace(serial, remote_path, data, cancel=ctx.cancel)

            def done(result: ReplaceResult, dev=dev):
                text = "Already identical" if result.no_change else "Replaced & verified"
                if result.backup:
                    text += f" • backup {result.backup.name}"
                panel.set_values(dev.serial, result.values, text, True)
                self.device_panel.model.set_last_action(dev.serial, f"Replace {remote_path}: OK", True)
                finish(dev.serial)

            def failed(exc, tb, dev=dev):
                panel.set_values(dev.serial, None, f"Replace failed: {exc}", False)
                self.device_panel.model.set_last_action(dev.serial, f"Replace failed: {exc}", False)
                failures.append(f"{dev.name}: {exc}")
                self._on_task_error(exc, tb, title=None, device=dev.serial)
                finish(dev.serial)

            self.runner.submit(task, on_result=done, on_error=failed,
                               on_finished=lambda s=dev.serial: self.device_panel.model.set_busy(s, False))
        self._status(f"Replacing {remote_path} on {names}…")

    # ------------------------------------------------------------------
    # Generic
    # ------------------------------------------------------------------
    def cancel(self) -> None:
        if self.runner.busy:
            self.runner.cancel_all()
            self.log.warning("Cancellation requested")
            self._status("Cancelling…")

    def _on_task_error(self, exc: BaseException, tb: str, title: str | None = "Operation failed",
                       device: str | None = None) -> None:
        if isinstance(exc, CancelledError):
            self.log.warning("Operation cancelled", device=device)
            return
        if isinstance(exc, DevReaderError):
            self.log.error(str(exc), device=device or exc.serial, command=exc.command, error=exc.output)
        else:
            self.log.error(f"Unexpected error: {exc!r}", device=device, error=tb)
        if title:
            self.toasts.show("error", title, str(exc), msecs=8000)
        self._status(str(exc))

    def _on_busy_changed(self, busy: bool) -> None:
        self.busy_bar.setVisible(busy)
        self.device_panel.set_busy(busy)
        self.config_panel.set_busy(busy)
        if not busy:
            self._status("Ready")

    def _status(self, text: str) -> None:
        self.status_text.setText(text)

    def _choose_adb(self) -> None:
        exe = "adb.exe (adb.exe);;All files (*)" if os.name == "nt" else "adb (adb);;All files (*)"
        path, _ = QFileDialog.getOpenFileName(self, "Locate adb", "", exe)
        if path:
            self._set_adb(path)

    def _set_adb(self, path: str | None) -> None:
        self.adb.set_adb_path(path)
        self.settings.setValue("adb_path", path or "")
        self.log.info(f"adb path set to {path or '(auto-detect)'}")
        self.refresh()

    def _open_log_folder(self) -> None:
        if self.log.log_file:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self.log.log_file).parent)))

    def _open_backups_folder(self) -> None:
        path = app_data_dir() / "backups"
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _about(self) -> None:
        QMessageBox.about(
            self, f"About {__app_name__}",
            f"<h3>{__app_name__} {__version__}</h3>"
            "<p>Internal engineering tool for ADB device discovery, preparation and "
            "configuration management.</p>"
            f"<p>Log file: {self.log.log_file}</p>",
        )

    # ------------------------------------------------------------------
    # Window state
    # ------------------------------------------------------------------
    def _restore_state(self) -> None:
        geo = self.settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        state = self.settings.value("v_split3")
        if state is None or not self.v_split.restoreState(state):
            # Room for ~9 device rows; the configuration table gets the rest.
            self.v_split.setSizes([190, max(self.height() - 310, 300)])

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "toasts"):
            self.toasts.reposition()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.runner.busy:
            if QMessageBox.question(
                self, "Operations running",
                "Operations are still running. Cancel them and exit?",
            ) != QMessageBox.Yes:
                event.ignore()
                return
            self.runner.cancel_all()
            self.runner.wait(8000)
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("v_split3", self.v_split.saveState())
        super().closeEvent(event)
