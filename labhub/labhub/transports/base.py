"""
Transports: how a command reaches a node.

exec_argv() returns the local command line that runs one shell command on
the node and streams its output. Phase 2 sessions (QCMAP) use
session_argv(), an interactive shell the job runner can type into.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys

from ..core.models import BackboneConfig, ConnectSpec, DeviceConfig

LAB_PASS_ENV = "LABHUB_SSH_PASS"   # the BB password from Mappings, for the ssh helper
_lab_password = ""


def set_ssh_password(password: str) -> None:
    """BB password from the lab file (Mappings); empty = $ssh_pass, else the default."""
    global _lab_password
    _lab_password = password or ""


APP_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class TransportError(Exception):
    pass


ADB_OVERRIDE: str | None = None  # set from user settings ("adb_path")


def _adb_candidates() -> list[str]:
    """Places adb.exe usually lives, besides this process's PATH.

    A double-clicked .bat gets Explorer's PATH from login time, so adb added
    to PATH later (or only in a PowerShell profile) is missed; the registry
    PATH and the usual install folders cover that.
    """
    out: list[str] = []
    env = os.environ
    for var in ("ADB", "ADB_PATH"):
        if env.get(var):
            out.append(env[var])
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if env.get(var):
            out.append(os.path.join(env[var], "platform-tools", "adb.exe"))
    try:
        from ..core.config import load_env
        reg_path = load_env().get("Path") or load_env().get("PATH") or ""
    except Exception:
        reg_path = ""
    for d in reg_path.split(os.pathsep):
        d = os.path.expandvars(d.strip().strip('"'))
        if d:
            out.append(os.path.join(d, "adb.exe"))
    local = env.get("LOCALAPPDATA", "")
    for base in (os.path.join(local, "Android", "Sdk", "platform-tools"),
                 r"C:\platform-tools", r"C:\adb", r"C:\Android\platform-tools",
                 r"C:\Program Files\platform-tools", r"C:\Program Files (x86)\platform-tools",
                 os.path.join(os.path.expanduser("~"), "platform-tools"),
                 os.path.join(local, "Microsoft", "WinGet", "Links")):
        out.append(os.path.join(base, "adb.exe"))
    return out


_found: str | None = None


def find_adb() -> str | None:
    global _found
    if ADB_OVERRIDE and os.path.isfile(ADB_OVERRIDE):
        return ADB_OVERRIDE
    if _found and os.path.isfile(_found):
        return _found
    hit = shutil.which("adb")
    if not hit:
        hit = next((c for c in _adb_candidates() if os.path.isfile(c)), None)
        if not hit:  # wrappers such as adb.bat / adb.cmd in a registry PATH folder
            hit = next((c[:-4] + ext for c in _adb_candidates() for ext in (".bat", ".cmd")
                        if os.path.isfile(c[:-4] + ext)), None)
    _found = hit
    return hit


def _adb(serial: str) -> list[str]:
    adb = find_adb()
    if not adb:
        raise TransportError("adb.exe not found in PATH (install Android platform-tools)")
    if not serial:
        raise TransportError("no serial configured")
    return [adb, "-s", serial]


def _ssh(params: dict, mode: str) -> list[str]:
    """Runs ssh_exec.py (paramiko) so the password can come from $ssh_pass."""
    host = params.get("host")
    if not host:
        raise TransportError("ssh: no host configured")
    py = sys.executable
    if py.lower().endswith("pythonw.exe"):
        py = py[:-len("pythonw.exe")] + "python.exe"
    return [py, "-m", "labhub.transports.ssh_exec", str(host), str(params.get("user") or "root"),
            str(params.get("port") or 22), mode, str(params.get("netns") or "-")]


_rooted: set[str] = set()


def ensure_root(serial: str) -> None:
    """`adb -s <serial> wait-for-device root` once per device per session
    (cfg80211tool and friends need root)."""
    if not serial or serial in _rooted:
        return
    try:
        argv = _adb(serial)
        subprocess.run(argv + ["wait-for-device", "root"], capture_output=True, timeout=60,
                       stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW, cwd=os.path.expanduser("~"))
        subprocess.run(argv + ["wait-for-device"], capture_output=True, timeout=30,
                       stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW, cwd=os.path.expanduser("~"))
        _rooted.add(serial)
    except (OSError, subprocess.TimeoutExpired, TransportError):
        pass


def forget_root(serial: str) -> None:
    """After a reboot adbd runs unprivileged again; root it on next use."""
    _rooted.discard(serial)


def connect_spec(config, serial: str = "") -> ConnectSpec:
    """Devices are always adb; backbones use their own connect spec."""
    if isinstance(config, DeviceConfig):
        return ConnectSpec("adb", {"serial": serial})
    if isinstance(config, BackboneConfig):
        return config.connect
    raise TransportError("node has no configuration")


def exec_argv(spec: ConnectSpec, command: str) -> list[str]:
    """Command line that runs `command` (a shell script) on the node."""
    if spec.type == "adb":
        return _adb(spec.params.get("serial", "")) + ["shell", command]
    if spec.type == "ssh":
        return _ssh(spec.params, "exec") + [command]
    if spec.type == "local":
        if sys.platform == "win32":
            return ["cmd.exe", "/d", "/c", command]
        return ["/bin/sh", "-c", command]
    raise TransportError("connection not configured (Mappings → set how to reach it)")


def session_argv(spec: ConnectSpec, tty: bool = False) -> list[str]:
    """Interactive shell; input is written to stdin."""
    if spec.type == "adb":
        return _adb(spec.params.get("serial", "")) + (["shell", "-tt"] if tty else ["shell"])
    if spec.type == "ssh":
        return _ssh(spec.params, "tty" if tty else "shell")
    if spec.type == "local":
        return ["cmd.exe", "/q", "/k"] if sys.platform == "win32" else ["/bin/sh", "-i"]
    raise TransportError("connection not configured (Mappings → set how to reach it)")


def popen(argv: list[str], stdin: bool = False) -> subprocess.Popen:
    """Start a hidden process with merged stdout/stderr, read in binary."""
    return subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=CREATE_NO_WINDOW, bufsize=0, cwd=os.path.expanduser("~"), env=child_env())


def child_env() -> dict:
    """Environment for child processes; lets the ssh helper import labhub."""
    env = dict(os.environ)
    env["PYTHONPATH"] = APP_ROOT + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if _lab_password:
        env[LAB_PASS_ENV] = _lab_password
    else:
        env.pop(LAB_PASS_ENV, None)
    return env


def describe(argv: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)
