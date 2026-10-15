"""Application bootstrap: builds services and the main window."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

from devreader import __app_name__, __org_name__, __version__


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="devreader", description="DevReader - ADB device configuration tool")
    p.add_argument("--adb", metavar="PATH", help="path to the adb executable (overrides PATH/ADB_PATH)")
    p.add_argument(
        "--simulate", action="store_true",
        help="use a built-in simulated adb with fake devices (no hardware required)",
    )
    p.add_argument("--version", action="version", version=f"{__app_name__} {__version__}")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    from PySide6.QtCore import QSettings
    from PySide6.QtGui import QFont, QIcon
    from PySide6.QtWidgets import QApplication

    from devreader.config.service import ConfigService
    from devreader.core.adb_service import AdbService, DeviceState
    from devreader.core.discovery import DiscoveryService
    from devreader.core.logging_service import LogService
    from devreader.core.paths import backups_dir, logs_dir
    from devreader.core.preparation import PreparationService
    from devreader.gui.main_window import MainWindow
    from devreader.gui.theme import apply_theme

    QApplication.setApplicationName(__app_name__)
    QApplication.setOrganizationName(__org_name__)
    QApplication.setApplicationVersion(__version__)
    app = QApplication(sys.argv[:1])
    app.setFont(QFont("Segoe UI", 10))
    apply_theme(app)
    icon_path = os.path.join(os.path.dirname(__file__), "resources", "devreader.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    log_file = logs_dir() / f"devreader_{datetime.now():%Y%m%d}.log"
    log = LogService(log_file)
    log.info(f"{__app_name__} {__version__} started (log: {log_file})")

    if args.simulate:
        from devreader.core.simulator import SimulatedAdbService

        _install_simulated_env()
        adb: AdbService = SimulatedAdbService(log, {
            "SIMAP0001": DeviceState.CONNECTED,
            "SIMCPE0001": DeviceState.CONNECTED,
            "SIMCPE0002": DeviceState.UNAUTHORIZED,
            "SIMEXTRA99": DeviceState.CONNECTED,
        })
        log.warning("Running in SIMULATION mode - no real devices are used")
    else:
        stored = QSettings().value("adb_path", "") or None
        adb = AdbService(log, adb_path=args.adb or stored)

    window = MainWindow(
        log=log,
        adb=adb,
        discovery=DiscoveryService(adb, log),
        preparation=PreparationService(adb, log),
        config=ConfigService(adb, log, backup_dir=backups_dir()),
        simulated=args.simulate,
    )
    window.show()
    return app.exec()


def _install_simulated_env() -> None:
    os.environ.setdefault("adb_device_id_ap", "SIMAP0001")
    os.environ.setdefault("adb_device_id_cpe1", "SIMCPE0001")
    os.environ.setdefault("adb_device_id_cpe2", "SIMCPE0002")
    os.environ.setdefault("adb_device_id_cpe3", "SIMCPE0003")
