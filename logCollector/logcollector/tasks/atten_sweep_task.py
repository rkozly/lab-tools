"""
Attenuation sweep: sets every attenuator to each value of a list, holding
each value for a fixed time, then ends (the attenuation stays at the last
value).

The traffic recordings running at the same time log the attenuation they
read back plus the current step (attenuator.sweep_note), so each traffic
line shows which step it belongs to.
"""

from PySide6.QtCore import Signal

from ..core import attenuator
from .base_task import BaseTask, State, TaskError, TaskStopped


class AttenSweepTask(BaseTask):
    # (step number, step count, value dB, seconds left in the step)
    progress = Signal(int, int, float, int)

    def __init__(self, attenuators, steps: list[float], step_seconds: int,
                 parent=None):
        super().__init__(device="ATTEN", name="Attenuation sweep", parent=parent)
        self.attenuators = list(attenuators)
        self.steps = list(steps)
        self.step_seconds = max(1, int(step_seconds))

    def execute(self):
        n = len(self.steps)
        try:
            for i, value in enumerate(self.steps, 1):
                self.check_stop()
                db = attenuator.format_db(value)
                self.status.emit(State.WAITING, f"Step {i}/{n}: setting {db} dB...")
                problems = []
                for host, port in self.attenuators:
                    readback = attenuator.set_attenuation(host, port, value)
                    if not attenuator.confirmed(value, readback):
                        got = "no answer" if readback is None else f"reads {readback:.2f} dB"
                        problems.append(f"{host}: {got}")
                    self.log.info("Set %s:%d to %s dB -> %s", host, port, db, readback,
                                  extra={"device": self.device})
                if problems:
                    raise TaskError(f"Step {i}/{n}: could not set {db} dB\n"
                                    + "\n".join(problems)
                                    + "\nIs the MobaXterm session of that attenuator open?")
                attenuator.set_sweep_note(f"step {i}/{n} {db} dB")

                def tick(left, i=i, value=value, db=db):
                    self.progress.emit(i, n, value, left)
                    self.status.emit(State.COLLECTING,
                                     f"Step {i}/{n}: {db} dB - {left} s left")
                self.sleep_interruptible(self.step_seconds, on_tick=tick)
        except TaskStopped:
            return State.STOPPED, "Sweep stopped - attenuation left at the current step"
        finally:
            attenuator.set_sweep_note("")
        last = attenuator.format_db(self.steps[-1])
        return State.COMPLETED, f"Sweep done: {n} step{'s' if n != 1 else ''}, left at {last} dB"
