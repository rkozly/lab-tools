"""
Attenuators over raw TCP (the MobaXterm sessions 1.1.1.70 / 1.1.1.80).

Reading: sending "G" returns the current attenuation, e.g. b"30.00\\x00".
Setting: sending the number ("12" + Enter) sets it, as typed in MobaXterm.
Only the sweep (AttenSweepTask) sets values; recordings only read.

Several recordings (one per device) read the attenuators. A lock lets
only one connection at a time reach them, and a reading is reused for
CACHE_SECONDS, so devices recording the same round log the same value.
"""

import re
import socket
import threading
import time

from ..config import (ATTEN_MAX_DB, ATTEN_SWEEP_MAX_STEPS, ATTENUATOR_PORT,
                      ATTENUATOR_READ_CMD)

CACHE_SECONDS = 2.0
# A set value counts as confirmed when the readback is this close.
SET_TOLERANCE_DB = 0.05

_lock = threading.Lock()
_cache: tuple[float, tuple, list] | None = None   # (time, attenuators, readings)

# Current sweep step, written into every traffic line while a sweep runs.
_sweep_note = ""


def parse_attenuation(data: bytes) -> float | None:
    """b"30.00\\x00" -> 30.0. None if the reply is not a number."""
    text = data.replace(b"\x00", b" ").decode("ascii", errors="replace").strip()
    try:
        return float(text.split()[0]) if text else None
    except ValueError:
        return None


def parse_attenuators(text: str) -> list[tuple[str, int]]:
    """"1.1.1.70, 1.1.1.80:1234" -> [("1.1.1.70", 1234), ("1.1.1.80", 1234)].

    Raises ValueError with a message for the user.
    """
    result = []
    for item in re.split(r"[,;\s]+", text.strip()):
        if not item:
            continue
        host, _, port = item.partition(":")
        if not re.fullmatch(r"[A-Za-z0-9.\-]+", host):
            raise ValueError(f"Invalid attenuator address: {item}")
        if port and not port.isdigit():
            raise ValueError(f"Invalid port in: {item}")
        result.append((host, int(port) if port else ATTENUATOR_PORT))
    if not result:
        raise ValueError("No attenuator address given.")
    return result


def format_attenuators(attenuators) -> str:
    return ", ".join(h if p == ATTENUATOR_PORT else f"{h}:{p}" for h, p in attenuators)


def parse_steps(text: str) -> list[float]:
    """"0,1,2,1,2" -> [0, 1, 2, 1, 2]. Raises ValueError with a message."""
    items = [i for i in re.split(r"[,;\s]+", text.strip()) if i]
    if not items:
        raise ValueError("The attenuation list is empty.")
    if len(items) > ATTEN_SWEEP_MAX_STEPS:
        raise ValueError(f"Too many attenuations: {len(items)} "
                         f"(at most {ATTEN_SWEEP_MAX_STEPS}).")
    steps = []
    for i in items:
        try:
            value = float(i)
        except ValueError:
            raise ValueError(f"Not a number: {i}")
        if not 0 <= value <= ATTEN_MAX_DB:
            raise ValueError(f"Out of range (0 - {ATTEN_MAX_DB} dB): {i}")
        steps.append(value)
    return steps


def format_db(value: float) -> str:
    """12.0 -> "12", 12.5 -> "12.5" (what is typed into the attenuator)."""
    return f"{value:g}"


def _send(host: str, port: int, command: str, timeout: float) -> bytes:
    """Send one command line and return the reply (up to NUL or timeout)."""
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall(command.encode("ascii") + b"\r\n")
        data = b""
        end = time.monotonic() + timeout
        while b"\x00" not in data and time.monotonic() < end:
            try:
                chunk = s.recv(256)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
        return data


def read_attenuation(host: str, port: int, timeout: float = 3.0) -> float | None:
    """Current attenuation in dB, or None if the attenuator does not answer."""
    try:
        return parse_attenuation(_send(host, port, ATTENUATOR_READ_CMD, timeout))
    except OSError:
        return None


def set_attenuation(host: str, port: int, value: float) -> float | None:
    """Set the attenuation and return the value read back (None: no answer)."""
    global _cache
    with _lock:
        _cache = None
        try:
            _send(host, port, format_db(value), timeout=1.0)
        except OSError:
            return None
        time.sleep(0.2)
        return read_attenuation(host, port)


def confirmed(target: float, readback: float | None) -> bool:
    return readback is not None and abs(readback - target) <= SET_TOLERANCE_DB


def read_all(attenuators) -> list[tuple[str, float | None]]:
    """[(host, dB or None), ...] for every (host, port) in attenuators."""
    global _cache
    key = tuple(attenuators)
    with _lock:
        now = time.monotonic()
        if _cache and _cache[1] == key and now - _cache[0] < CACHE_SECONDS:
            return _cache[2]
        readings = [(host, read_attenuation(host, port)) for host, port in attenuators]
        _cache = (time.monotonic(), key, readings)
        return readings


def format_readings(readings) -> str:
    """'1.1.1.70=30.00 dB 1.1.1.80=30.00 dB' (? for no answer)."""
    return " ".join(f"{host}={'?' if db is None else f'{db:.2f}'} dB"
                    for host, db in readings)


def set_sweep_note(note: str) -> None:
    global _sweep_note
    _sweep_note = note


def sweep_note() -> str:
    return _sweep_note
