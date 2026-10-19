"""Background device discovery: `adb devices` + environment role mapping."""

from PySide6.QtCore import Signal

from ..core.adb import build_device_rows, find_adb, load_role_map, parse_devices
from .base_task import BaseTask, State


class DeviceScanTask(BaseTask):
    # list of (role, serial, state)
    result = Signal(list)

    def __init__(self, parent=None):
        super().__init__(device="-", name="Device scan", parent=parent)

    def execute(self):
        role_map = load_role_map()
        connected = {}
        error = ""

        if find_adb():
            rc, out, err = self.run_cmd([self.adb_path(), "devices"], timeout=20)
            if rc == 0:
                connected = parse_devices(out)
            else:
                error = self.describe_failure(["adb", "devices"], rc, err or out)
        else:
            error = "ADB is not installed or not in PATH."

        rows = build_device_rows(role_map, connected)
        if error:
            # adb itself failed: the devices may well be connected, so do not
            # claim "Not connected" - show that the scan failed instead.
            rows = [(role, serial, "scan failed") for role, serial, _ in rows]
        self.result.emit(rows)
        if error:
            return State.ERROR, error
        n = len(connected)
        return State.COMPLETED, f"{n} device{'s' if n != 1 else ''} connected"
