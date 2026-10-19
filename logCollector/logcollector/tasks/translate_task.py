"""Run the Qualcomm pktlog translator (pktlog_postproc.py) on a capture."""

import os
import shutil
import sys

from ..config import TRANSLATOR_DEFS, TRANSLATOR_PYTHON, TRANSLATOR_SCRIPT
from ..core.crm import translator_dir
from ..core.paths import check_input_file, check_output_file
from .base_task import BaseTask, State, TaskError


def find_python() -> str:
    """Interpreter for the translator: configured one on PATH, else our own."""
    found = shutil.which(TRANSLATOR_PYTHON)
    if found:
        return found
    exe = sys.executable
    # pythonw.exe works too, but prefer python.exe next to it if present.
    candidate = os.path.join(os.path.dirname(exe), "python.exe")
    return candidate if os.path.isfile(candidate) else exe


class TranslateTask(BaseTask):
    def __init__(self, crm_path: str, input_file: str, output_file: str,
                 label: str = "-", parent=None):
        super().__init__(device=label, name="PKTLOG translation", parent=parent)
        self.crm_path = crm_path
        self.input_file = input_file
        self.output_file = output_file

    def execute(self):
        tool_dir = translator_dir(self.crm_path)
        script = os.path.join(tool_dir, TRANSLATOR_SCRIPT)
        if not os.path.isdir(self.crm_path):
            raise TaskError(f"CRM path not found:\n{self.crm_path}")
        if not os.path.isfile(script):
            raise TaskError(f"Translator not found:\n{script}")
        check_input_file(self.input_file)
        check_output_file(self.output_file)

        self.status.emit(State.TRANSLATING, "Translating...")
        # python pktlog_postproc.py -d pktlog_defs_wkk -t "<in>" -w text > "<out>"
        args = [find_python(), TRANSLATOR_SCRIPT, "-d", TRANSLATOR_DEFS,
                "-t", self.input_file, "-w", "text"]
        try:
            with open(self.output_file, "wb") as f:
                rc, _, err = self.run_cmd(args, stdout_file=f, cwd=tool_dir)
        except PermissionError:
            raise TaskError(f"Permission denied writing to:\n{self.output_file}")

        if rc != 0:
            last = err.strip().splitlines()[-1] if err.strip() else ""
            raise TaskError(f"Translation failed (exit code {rc})."
                            + (f"\n{last}" if last else ""))
        if os.path.getsize(self.output_file) == 0:
            raise TaskError("Translation produced an empty output file.")
        return State.COMPLETED, f"Completed: {os.path.basename(self.output_file)}"
