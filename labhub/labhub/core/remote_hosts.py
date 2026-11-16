"""
Devices plugged into another Windows PC (Mappings → "Device host").

For each such host LabHub keeps an SSH tunnel (your Windows user name, the BB
password from Mappings) to that host's adb server:

    127.0.0.1:<15037+n>  ──ssh──►  <host>  127.0.0.1:5037 (its adb server)

so `adb -P <port> -s <serial> ...` here reaches a CPE over there: shells,
commands, push / pull, everything. The adb server on the host is started
through PowerShell (`powershell -NoLogo -Command "adb start-server"`: adb may
be on PATH only in PowerShell there).

Every adb started from this process (LabHub, and the FW/Wireless and Logs
apps inside it) goes through a Popen hook:
  adb -s <serial on a host> ...   gets  -P <that host's port>
  adb devices [-l]                 lists this PC's devices and every host's
so none of them needs to know where a device is plugged in.

The tunnel's local port stays bound while the host is unreachable: otherwise
adb would start a server of its own on that port.
"""

from __future__ import annotations

import getpass
import os
import select
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from .events import Event, EventBus

BASE_PORT = 15037
REMOTE_ADB_PORT = 5037
# On the host, through PowerShell (adb may be on PATH only there): the command that worked.
# (What it prints is kept for the adb diagnostics.)
START_SERVER = 'powershell -NoLogo -Command "adb start-server"'
RETRY_S = 10


@dataclass
class HostState(Event):
    host: str
    up: bool
    text: str


# --- routing: serial -> local tunnel port (read by the Popen hook) ----------------------------

_ports: dict[str, int] = {}        # serial -> port
_all_ports: list[int] = []         # every tunnel port (for `adb devices`)


def set_routes(serial_ports: dict[str, int], ports: list[int]) -> None:
    global _ports, _all_ports
    _ports = dict(serial_ports)
    _all_ports = list(ports)


def _is_adb(exe) -> bool:
    return isinstance(exe, str) and os.path.basename(exe).lower() in ("adb", "adb.exe")


def rewrite(args):
    """The argv to run instead of `args` (None = unchanged)."""
    if not isinstance(args, (list, tuple)) or not args or not _is_adb(args[0]) or not _all_ports:
        return None
    args = list(args)
    if "-P" in args or "-H" in args:
        return None
    if "-s" in args:
        i = args.index("-s")
        port = _ports.get(args[i + 1]) if i + 1 < len(args) else None
        return [args[0], "-P", str(port)] + args[1:] if port else None
    rest = [a for a in args[1:] if a != "-l"]
    if rest == ["devices"]:                     # this PC + every host
        py = sys.executable
        if py.lower().endswith("pythonw.exe"):
            py = py[:-len("pythonw.exe")] + "python.exe"
        return [py, "-m", "labhub.core.remote_hosts", ",".join(map(str, _all_ports))] + args
    return None


_hooked = False


def install_hook() -> None:
    """Route every adb this process starts (see the top of the file). Once."""
    global _hooked
    if _hooked:
        return
    _hooked = True
    orig = subprocess.Popen.__init__

    def init(self, args, *a, **kw):
        new = rewrite(args)
        if new is not None and new[0] != args[0]:     # the devices merger: it imports labhub
            env = dict(kw.get("env") or os.environ)
            from ..transports.base import APP_ROOT
            env["PYTHONPATH"] = APP_ROOT + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            kw["env"] = env
        orig(self, new if new is not None else args, *a, **kw)

    subprocess.Popen.__init__ = init


def merge_devices(ports: list[int], argv: list[str]) -> int:
    """`adb devices [-l]` here and through every tunnel, printed as one list."""
    adb, args = argv[0], argv[1:]
    flags = dict(stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20,
                 creationflags=0x08000000 if sys.platform == "win32" else 0)
    first = subprocess.run([adb] + args, **flags)
    sys.stdout.write(first.stdout)
    sys.stderr.write(first.stderr)
    for port in ports:
        try:
            res = subprocess.run([adb, "-P", str(port)] + args, **flags)
        except subprocess.TimeoutExpired:
            continue
        for line in res.stdout.splitlines():
            if line.strip() and not line.startswith(("List of devices", "*")):
                sys.stdout.write(line + "\n")
    return first.returncode


# --- tunnels ----------------------------------------------------------------------------------

class Tunnel:
    def __init__(self, host: str, port: int, bus: EventBus, password) -> None:
        self.host, self.port, self.bus, self.password = host, port, bus, password
        self.up = False
        self.text = "connecting…"
        self.started = ""                      # what START_SERVER printed on the host (diagnostics)
        self._stop = threading.Event()
        self._client = None
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", port))   # kept bound: adb must never start a server here
        self._sock.listen(16)
        threading.Thread(target=self._keep, name=f"tunnel-{host}", daemon=True).start()
        threading.Thread(target=self._accept, name=f"tunnel-accept-{host}", daemon=True).start()

    def _state(self, up: bool, text: str) -> None:
        if (up, text) != (self.up, self.text):
            self.up, self.text = up, text
            self.bus.publish(HostState(self.host, up, text))

    def _connect(self):
        import paramiko
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(self.host, username=getpass.getuser(), password=self.password(), timeout=8,
                  banner_timeout=8, auth_timeout=8, look_for_keys=True, allow_agent=True)
        c.get_transport().set_keepalive(15)
        _in, out, err = c.exec_command(START_SERVER, timeout=40)
        code = out.channel.recv_exit_status()
        self.started = (f"$ {START_SERVER}   (exit {code})\n"
                        + out.read().decode("utf-8", "replace") + err.read().decode("utf-8", "replace")).strip()
        return c

    def _keep(self) -> None:
        while not self._stop.is_set():
            t = self._client.get_transport() if self._client else None
            if t is None or not t.is_active():
                try:
                    self._client = self._connect()
                    self._state(True, f"tunnel up (127.0.0.1:{self.port} → {self.host}:{REMOTE_ADB_PORT})")
                except Exception as e:
                    self._client = None
                    self._state(False, f"cannot reach {self.host}: {e}")
                    self._stop.wait(RETRY_S)
                    continue
            self._stop.wait(5)

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                return
            t = self._client.get_transport() if self._client else None
            if t is None or not t.is_active():
                conn.close()                   # adb sees a failed connection (and starts nothing)
                continue
            try:
                chan = t.open_channel("direct-tcpip", ("127.0.0.1", REMOTE_ADB_PORT), conn.getpeername())
            except Exception:
                conn.close()
                continue
            threading.Thread(target=self._pump, args=(conn, chan), daemon=True).start()

    @staticmethod
    def _pump(conn, chan) -> None:
        try:
            while True:
                r, _w, _x = select.select([conn, chan], [], [], 30)
                if conn in r:
                    data = conn.recv(65536)
                    if not data:
                        break
                    chan.sendall(data)
                if chan in r:
                    data = chan.recv(65536)
                    if not data:
                        break
                    conn.sendall(data)
        except Exception:
            pass
        finally:
            chan.close()
            conn.close()

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        if self._client:
            self._client.close()


class RemoteHosts:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.tunnels: dict[str, Tunnel] = {}

    def configure(self, device_hosts: dict[str, str], password) -> None:
        """device_hosts: serial -> host (devices on another PC). Starts / stops tunnels."""
        hosts = sorted({h.lower() for h in device_hosts.values() if h})
        for h in [h for h in self.tunnels if h not in hosts]:
            self.tunnels.pop(h).close()
        for h in hosts:
            if h not in self.tunnels:
                port = self._free_port()
                try:
                    self.tunnels[h] = Tunnel(h, port, self.bus, password)
                except OSError as e:
                    self.bus.publish(HostState(h, False, f"local port {port}: {e}"))
        set_routes({s: self.tunnels[h.lower()].port for s, h in device_hosts.items()
                    if s and h and h.lower() in self.tunnels},
                   [t.port for t in self.tunnels.values()])
        if self.tunnels:
            install_hook()

    def _free_port(self) -> int:
        used = {t.port for t in self.tunnels.values()}
        port = BASE_PORT
        while port in used:
            port += 1
        return port

    def states(self) -> list[tuple[str, bool, str]]:
        return [(h, t.up, t.text) for h, t in sorted(self.tunnels.items())]

    def close(self) -> None:
        for t in self.tunnels.values():
            t.close()
        self.tunnels.clear()
        set_routes({}, [])


if __name__ == "__main__":         # the `adb devices` merger: python -m ... <ports> <adb> devices [-l]
    ports = [int(p) for p in sys.argv[1].split(",") if p]
    sys.exit(merge_devices(ports, sys.argv[2:]))
