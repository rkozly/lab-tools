"""
Serial console collection: reads a COM port (the same console MobaXterm
shows: kernel / dmesg messages, boot and crash output) and writes everything
to a file until stopped.

Windows lets only one program open a COM port, so the device's session must
be closed in MobaXterm while collecting.
"""

import time

from ..core.paths import check_output_file
from .base_task import BaseTask, State, TaskError

# Seconds a read waits for data; also how fast Stop is noticed.
READ_TIMEOUT = 0.2
# Flush the output file at least this often (seconds).
FLUSH_INTERVAL = 1.0


class SerialTask(BaseTask):
    def __init__(self, role: str, port: str, baud: int, output_path: str,
                 timestamps: bool = False, parent=None):
        super().__init__(device=role, name="Serial collection", parent=parent)
        self.role = role
        self.port = port
        self.baud = baud
        self.output_path = output_path
        self.timestamps = timestamps
        self._line_start = True

    def execute(self):
        try:
            import serial
        except ImportError:
            raise TaskError("pyserial is not installed.\n"
                            "Start the app with 'Log Collector.bat' to install "
                            "it, or run: python -m pip install pyserial")

        self.status.emit(State.WAITING, "Checking output path...")
        check_output_file(self.output_path)
        self.check_stop()

        self.status.emit(State.WAITING, f"Opening {self.port}...")
        try:
            port = serial.Serial(self.port, self.baud, timeout=READ_TIMEOUT)
        except serial.SerialException as e:
            raise TaskError(self._open_error(e))
        self.log.info("Opened %s at %d baud -> %s", self.port, self.baud,
                      self.output_path, extra={"device": self.device})

        total = 0
        try:
            with open(self.output_path, "ab") as f:
                self.status.emit(State.COLLECTING, f"Collecting from {self.port}...")
                last_flush = time.monotonic()
                while not self.stop_requested:
                    try:
                        data = port.read(max(1, port.in_waiting))
                    except serial.SerialException as e:
                        raise TaskError(f"{self.port} disconnected "
                                        f"(USB cable unplugged?)\n{e}")
                    if data:
                        f.write(self._stamp(data) if self.timestamps else data)
                        total += len(data)
                    now = time.monotonic()
                    if now - last_flush >= FLUSH_INTERVAL:
                        f.flush()
                        last_flush = now
                        self.status.emit(State.COLLECTING,
                                         f"Collecting from {self.port}... {_size(total)}")
        except PermissionError:
            raise TaskError(f"Permission denied writing to:\n{self.output_path}")
        except OSError as e:
            raise TaskError(f"Cannot write output file:\n{e}")
        finally:
            port.close()

        return State.STOPPED, f"Stopped - {_size(total)} saved"

    def _stamp(self, data: bytes) -> bytes:
        """Prefix every line with the PC time.

        Split on \\n only: consoles send \\r\\n, and the two bytes may arrive
        in different reads, which would otherwise create empty stamped lines.
        """
        out = bytearray()
        parts = data.split(b"\n")
        for i, chunk in enumerate(parts):
            last = i == len(parts) - 1
            if last and not chunk:
                break  # data ended with \n
            if self._line_start:
                out += time.strftime("[%Y-%m-%d %H:%M:%S] ").encode()
            out += chunk if last else chunk + b"\n"
            self._line_start = not last
        return bytes(out)

    def _open_error(self, e) -> str:
        text = str(e)
        low = text.lower()
        if "access is denied" in low or "permissionerror" in low:
            return (f"{self.port} is in use by another program.\n"
                    f"Close the {self.role} session in MobaXterm and press Start again.")
        if "filenotfound" in low or "cannot find" in low:
            return f"{self.port} does not exist (USB serial cable not connected?)"
        return f"Cannot open {self.port}:\n{text}"


def _size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"
