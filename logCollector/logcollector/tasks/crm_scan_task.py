"""Find the latest CRM build in the background (network share may be slow)."""

from PySide6.QtCore import Signal

from ..config import CRM_ROOT
from ..core.crm import find_latest_crm
from .base_task import BaseTask, State, TaskError


class CrmScanTask(BaseTask):
    result = Signal(str)  # full path of the latest CRM directory

    def __init__(self, root: str = CRM_ROOT, parent=None):
        super().__init__(device="-", name="CRM scan", parent=parent)
        self.root = root

    def execute(self):
        try:
            path = find_latest_crm(self.root)
        except FileNotFoundError as e:
            raise TaskError(str(e))
        except OSError:
            raise TaskError(f"Network drive is not available:\n{self.root}")
        self.result.emit(path)
        return State.COMPLETED, "Latest CRM detected"
