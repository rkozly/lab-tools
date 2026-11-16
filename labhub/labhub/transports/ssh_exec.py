"""
SSH helper for backbones. Used as:

    python -m labhub.transports.ssh_exec <host> <user> <port> <mode> <netns|-> [command]

mode: exec (run command) | shell | tty (interactive, input from stdin).

It does what you would do by hand in PowerShell:

    ssh lx-lbr-10          (the system ssh: your keys / Kerberos / ssh config)
    sudo su                (user "root" in Mappings means: become root)
    <password>             typed when sudo asks for it

The password comes from the ssh_pass environment variable, else the lab
default (DEFAULT_PASSWORD); the login itself needs none (keys / Kerberos,
BatchMode). With a namespace the shell then runs
`ip netns exec <netns> sh -i`. If no ssh.exe is found, paramiko is used.
"""

from __future__ import annotations

import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time

PASS_ENV = "ssh_pass"
DEFAULT_PASSWORD = "Qibylab@Wi9i9Lab"   # lab backbones; Mappings (LABHUB_SSH_PASS), then $ssh_pass win
LAB_PASS_ENV = "LABHUB_SSH_PASS"
_PW_PROMPT = re.compile(rb"(?i)password[^\n]*:\s*$")
_ROOT_PROMPT = re.compile(rb"#\s*$")
_ANY_PROMPT = re.compile(rb"[$#>]\s*$")
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def password() -> str:
    from ..core.config import load_env
    env = {k.lower(): v for k, v in load_env().items()}
    return os.environ.get(LAB_PASS_ENV) or env.get(PASS_ENV) or DEFAULT_PASSWORD


def find_ssh() -> str | None:
    """The ssh PowerShell runs for `ssh lx-lbr-10` (same keys / config), else
    the first on PATH, else the Windows one, else Git's."""
    if sys.platform == "win32":
        try:
            res = subprocess.run(["powershell.exe", "-NoLogo", "-Command", "(Get-Command ssh -ErrorAction Stop).Source"],
                                 capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL,
                                 creationflags=CREATE_NO_WINDOW, cwd=os.path.expanduser("~"))
            lines = [l.strip() for l in res.stdout.splitlines() if l.strip().lower().endswith(".exe")]
            if lines and os.path.isfile(lines[-1]):
                return lines[-1]
        except (OSError, subprocess.TimeoutExpired):
            pass
    hit = shutil.which("ssh")
    if hit:
        return hit
    for c in (r"C:\Windows\System32\OpenSSH\ssh.exe",
              os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Programs\Git\usr\bin\ssh.exe"),
              r"C:\Program Files\Git\usr\bin\ssh.exe"):
        if os.path.isfile(c):
            return c
    return None


def sudo_wrap(command: str, netns: str) -> str:
    inner = f"ip netns exec {shlex.quote(netns)} sh -c {shlex.quote(command)}" if netns else command
    return f"sudo -S -p '' sh -c {shlex.quote(inner)}"


# --- sudo su in an interactive shell -------------------------------------------

def become_root(read, write, out, timeout: float = 20.0) -> None:
    """read(t) -> bytes or b"" (nothing yet) or None (closed); write(bytes)."""
    def pump(until: float, check) -> bool | None:
        buf = b""
        while time.monotonic() < until:
            data = read(0.1)
            if data is None:
                return None
            if data:
                out.write(data)
                out.flush()
                buf += data
                if check(buf):
                    return True
        return False

    if pump(time.monotonic() + 15, lambda b: bool(_ANY_PROMPT.search(b))) is None:
        return
    write(b"sudo su\n")
    sent = [False]

    def check(buf: bytes) -> bool:
        tail = buf[-200:]
        if not sent[0] and _PW_PROMPT.search(tail):
            write((password() + "\n").encode())
            sent[0] = True
            return False
        return bool(_ROOT_PROMPT.search(tail)) and (sent[0] or b"sudo su" in buf)

    if pump(time.monotonic() + timeout, check) is False:
        out.write(b"\n[LabHub] sudo su: no root prompt (wrong password, or sudo not allowed?)\n")
        out.flush()


# --- system ssh ----------------------------------------------------------------

def _ssh_argv(ssh: str, host: str, user: str, port: int, tty: bool) -> list[str]:
    argv = [ssh, "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
            "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30"]
    if port and port != 22:
        argv += ["-p", str(port)]
    if user and user != "root":
        argv += ["-l", user]      # root = log in as yourself (like `ssh host`), then sudo su
    if tty:
        argv.append("-tt")
    return argv + [host]


def run_system(ssh: str, host: str, user: str, port: int, mode: str, netns: str, command: str) -> int:
    out = sys.stdout.buffer
    want_root = user == "root"
    if mode == "exec":
        cmd = sudo_wrap(command, netns) if (want_root or netns) else command
        p = subprocess.Popen(_ssh_argv(ssh, host, user, port, False) + [cmd], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=os.path.expanduser("~"),
                             creationflags=CREATE_NO_WINDOW)
        if want_root or netns:
            try:
                p.stdin.write((password() + "\n").encode())
                p.stdin.flush()
            except OSError:
                pass
        try:
            p.stdin.close()
        except OSError:
            pass
        for chunk in iter(lambda: p.stdout.read1(4096), b""):
            out.write(chunk)
            out.flush()
        return p.wait()

    p = subprocess.Popen(_ssh_argv(ssh, host, user, port, True), stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=os.path.expanduser("~"),
                         creationflags=CREATE_NO_WINDOW, bufsize=0)
    q: queue.Queue = queue.Queue()

    def reader() -> None:
        while True:
            data = p.stdout.read1(4096) if hasattr(p.stdout, "read1") else os.read(p.stdout.fileno(), 4096)
            q.put(data or None)
            if not data:
                return

    threading.Thread(target=reader, daemon=True).start()

    def read(t: float):
        try:
            return q.get(timeout=t)
        except queue.Empty:
            return b""

    def write(data: bytes) -> None:
        try:
            p.stdin.write(data)
            p.stdin.flush()
        except OSError:
            pass

    out.write(f"[LabHub] {os.path.basename(ssh)} {host}\n".encode())
    out.flush()
    if want_root:
        become_root(read, write, out)
    if netns:
        write(f"exec ip netns exec {shlex.quote(netns)} sh -i\n".encode())

    def pump_in() -> None:
        fd = sys.stdin.fileno()
        while True:
            data = os.read(fd, 4096)
            if not data:
                break
            write(data)
        try:
            p.stdin.close()
        except OSError:
            pass

    threading.Thread(target=pump_in, daemon=True).start()
    while True:
        data = q.get()
        if data is None:
            break
        out.write(data)
        out.flush()
    return p.wait()


# --- paramiko fallback (no ssh.exe) --------------------------------------------

def run_paramiko(host: str, user: str, port: int, mode: str, netns: str, command: str) -> int:
    import getpass
    import paramiko
    out = sys.stdout.buffer
    login = getpass.getuser() if user == "root" else user
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(host, port=port, username=login, password=password(), timeout=8, banner_timeout=8,
                  auth_timeout=8, look_for_keys=True, allow_agent=True)
    except Exception as e:
        out.write(f"[LabHub] ssh {login}@{host}:{port} failed: {e}\n".encode())
        out.flush()
        return 255
    try:
        chan = c.get_transport().open_session()
        chan.set_combine_stderr(True)
        if mode == "exec":
            chan.exec_command(sudo_wrap(command, netns) if (user == "root" or netns) else command)
            if user == "root" or netns:
                chan.sendall((password() + "\n").encode())
        else:
            chan.get_pty(width=200, height=50)
            chan.invoke_shell()

            def read(t: float):
                end = time.monotonic() + t
                while time.monotonic() < end:
                    if chan.recv_ready():
                        return chan.recv(4096) or None
                    if chan.exit_status_ready():
                        return None
                    time.sleep(0.02)
                return b""

            if user == "root":
                become_root(read, chan.sendall, out)
            if netns:
                chan.sendall(f"exec ip netns exec {shlex.quote(netns)} sh -i\n".encode())

            def pump_in() -> None:
                fd = sys.stdin.fileno()
                while True:
                    data = os.read(fd, 4096)
                    if not data:
                        break
                    chan.sendall(data)
                chan.shutdown_write()

            threading.Thread(target=pump_in, daemon=True).start()
        while True:
            data = chan.recv(4096)
            if not data:
                break
            out.write(data)
            out.flush()
        return chan.recv_exit_status()
    finally:
        c.close()


def main(argv: list[str]) -> int:
    host, user, port, mode, netns = argv[0], argv[1], int(argv[2]), argv[3], argv[4]
    netns = "" if netns == "-" else netns
    command = argv[5] if len(argv) > 5 else ""
    ssh = find_ssh()
    if ssh:
        return run_system(ssh, host, user, port, mode, netns, command)
    return run_paramiko(host, user, port, mode, netns, command)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
