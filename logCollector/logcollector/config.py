"""
Central configuration: device roles, default paths and command templates.

Everything a user might reasonably want to tweak lives here so the rest of
the code does not hard-code paths or device commands.
"""

import getpass
import os
import re

# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------

# Devices are not hard-coded: every environment variable named
# ENV_PREFIX + <role> (adb_device_id_ap, adb_device_id_cpe1 ... cpe16)
# becomes a device. Only configured devices are shown in the UI.
ENV_PREFIX = "adb_device_id_"
AP_ROLE = "AP"

# ---------------------------------------------------------------------------
# Output locations
# ---------------------------------------------------------------------------

# Fixed shared folder, the same on every PC (not based on who is logged in).
DEFAULT_OUTPUT_DIR = r"\\bulk\si\Users\rkozly"

# Old default (per Windows user). Saved paths that still point there are
# moved to DEFAULT_OUTPUT_DIR on startup, see MainWindow._migrate_settings.
OLD_DEFAULT_OUTPUT_DIR = rf"\\bulk\si\Users\{getpass.getuser()}"


def default_cnss_path(role: str) -> str:
    return rf"{DEFAULT_OUTPUT_DIR}\{role.lower()}_cnss.txt"


def default_congctrl_path(role: str) -> str:
    return rf"{DEFAULT_OUTPUT_DIR}\{role.lower()}_congctrl.txt"


def default_serial_path(role: str) -> str:
    return rf"{DEFAULT_OUTPUT_DIR}\{role.lower()}_serial.txt"


# ---------------------------------------------------------------------------
# Serial (console / dmesg) logs
# ---------------------------------------------------------------------------

# COM port per role is taken from the MobaXterm sessions named
# "<ROLE> - COM<N>" (e.g. "CPE2 - COM12"). An environment variable
# SERIAL_ENV_PREFIX + <role> (serial_port_cpe2=COM12) overrides it.
SERIAL_ENV_PREFIX = "serial_port_"
# Optional: full path of MobaXterm.ini when it is not in a standard place
# (portable MobaXterm keeps it next to MobaXterm.exe).
MOBAXTERM_INI_ENV = "mobaxterm_ini"
DEFAULT_BAUD_RATE = 115200
BAUD_RATES = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]


# ---------------------------------------------------------------------------
# wpa_supplicant (CPE) and hostapd (AP) debug logs
# ---------------------------------------------------------------------------

class FileEdit:
    """One change in a device file, applied by line, safe to re-run.

    kind "replace": on every line that contains `old` (and not yet `new`),
                    `old` is replaced by `new`.
    kind "comment": the line containing `old` gets "##" in front of it.
    Whitespace in old/new matches any run of spaces/tabs on the device.
    """

    def __init__(self, path: str, kind: str, old: str, new: str = ""):
        self.path = path
        self.kind = kind
        self.old = old
        self.new = new


class DaemonLogProfile:
    """Describes how to enable, check and collect one daemon's debug log."""

    def __init__(self, key, title, label, binary, process_flags, remote_log,
                 edits, restart_commands, side):
        self.key = key                    # settings prefix
        self.title = title                # panel title
        self.label = label                # prefix for status messages
        self.binary = binary              # process name on the device
        # Strings the running global process (the one started with -g) must
        # contain, i.e. proof that it was restarted with the new flags.
        self.process_flags = process_flags
        self.remote_log = remote_log      # log file written by the daemon
        self.edits = edits
        # Run on the device after the files are changed ("Restart service").
        # Restarting the global daemon drops its interfaces, so wifi is
        # reloaded too. "Reboot" is the safe alternative.
        self.restart_commands = restart_commands
        self.side = side                  # "ap" or "cpe": which roles are shown

    def files(self) -> list[str]:
        return list(dict.fromkeys(e.path for e in self.edits))


_WPA_INIT = "/etc/init.d/qca-wpa-supplicant"
_WPA_CMD = ("wpa_supplicant -g $WPAD_VARRUN/wpa_supplicantglobal -B "
            "-P $WPAD_VARRUN/wpa_supplicant-global.pid")
_WPA_LOG = "/tmp/wpa_supplicant.log"

WPA_PROFILE = DaemonLogProfile(
    key="wpa", title="WPA Supplicant Logging (CPE)", label="WPA",
    binary="wpa_supplicant",
    process_flags=["-tddd", f"-f {_WPA_LOG}"],
    remote_log=_WPA_LOG,
    # Both commands in the file (procd_set_param ... and the plain one)
    # contain _WPA_CMD, so one edit covers both lines.
    edits=[FileEdit(_WPA_INIT, "replace", _WPA_CMD, f"{_WPA_CMD} -tddd -f {_WPA_LOG}")],
    restart_commands=[f"{_WPA_INIT} restart", "wifi"],
    side="cpe",
)

_HOSTAPD_INIT = "/etc/init.d/qca-hostapd"
_HOSTAPD_LOG = "/tmp/hostapd.txt"

HOSTAPD_PROFILE = DaemonLogProfile(
    key="hostapd", title="Hostapd Logging (AP)", label="Hostapd",
    binary="hostapd",
    process_flags=["-dddKt"],
    remote_log=_HOSTAPD_LOG,
    edits=[
        FileEdit(_HOSTAPD_INIT, "replace",
                 "/usr/sbin/hostapd -g $WPAD_VARRUN/hostapd/global",
                 "/usr/sbin/hostapd -dddKt -g $WPAD_VARRUN/hostapd/global"),
        # Otherwise hostapd closes its log file when an interface goes down.
        FileEdit("/lib/wifi/hostapd.sh", "comment",
                 '[ -n "$ifname" ] && run_hostapd_cli $ifname '
                 '"$WPAD_VARRUN/hostapd-$device" "close_log"'),
    ],
    restart_commands=[f"{_HOSTAPD_INIT} restart", "wifi"],
    side="ap",
)


def default_wpa_path(role: str) -> str:
    return rf"{DEFAULT_OUTPUT_DIR}\wpa_supplicant_{role.lower()}.log"


def default_hostapd_path(role: str) -> str:
    return rf"{DEFAULT_OUTPUT_DIR}\hostapd_{role.lower()}.txt"


# ---------------------------------------------------------------------------
# CONGCTRL (congestion control) logs
# ---------------------------------------------------------------------------

CONGCTRL_UNIT_CMD = "setUnitTestCmd 0x48 3 537 1 1"
# Interfaces that enable CONGCTRL logging: (interface, link_id).
CONGCTRL_AP_LINKS = [("mld0", 0), ("mld1", 1)]
CONGCTRL_CPE_LINKS = [("ath0", 0)]
# Remote command; its output is written to the output file.
CONGCTRL_COMMAND = "cnss_diag -c | grep CONGCTRL"


def congctrl_setup_commands(role: str) -> list[str]:
    links = CONGCTRL_AP_LINKS if role == AP_ROLE else CONGCTRL_CPE_LINKS
    return [f"wifitool {iface} link_id {link} {CONGCTRL_UNIT_CMD}"
            for iface, link in links]


# ---------------------------------------------------------------------------
# PKTLOG
# ---------------------------------------------------------------------------

DEFAULT_PKTLOG_DURATION = 30
PKTLOG_BUFFER_SIZE = 30000000


def pktlog_setup_commands(radio: str, iface: str, link_id: int) -> list[str]:
    """Shell commands run on the device before the capture window.

    radio  - pktlog radio name (wifi0 / wifi1)
    iface  - interface passed to wifitool (mld0 / mld1 / ath0)
    """
    unit = f"wifitool {iface} link_id {link_id} setUnitTestCmd 0x48 2"
    return [
        f"pktlogconf -r {radio}",
        f"pktlogconf -a {radio} -elite,rcu,rcf -s {PKTLOG_BUFFER_SIZE}",
        f"{unit} 323 1",
        f"{unit} 322 2",
        f"{unit} 322 7",
    ]


class PktlogProfile:
    """Describes one pktlog capture type (radio, interface, remote file)."""

    def __init__(self, key, radio, iface, link_id, remote_file, default_name,
                 translated_name):
        self.key = key                  # short id used in UI / settings
        self.radio = radio              # wifi0 / wifi1
        self.iface = iface              # mld0 / mld1 / ath0
        self.link_id = link_id
        self.remote_file = remote_file  # file created on the device
        self.default_name = default_name        # pulled file name on the PC
        self.translated_name = translated_name  # translator output name

    def setup_commands(self) -> list[str]:
        return pktlog_setup_commands(self.radio, self.iface, self.link_id)

    def disable_command(self) -> str:
        return f"pktlogconf -d {self.radio}"

    def copy_command(self) -> str:
        return f"cp /proc/ath_pktlog/{self.radio} {self.remote_file}"


AP_PHY_PROFILES = {
    "PHY0": PktlogProfile("ap_phy0", "wifi0", "mld0", 0,
                          "/tmp/pktlog_radio0_ap.dat", "ap_pktlog_radio0.dat",
                          "wifi0_pktlog_ap.txt"),
    "PHY1": PktlogProfile("ap_phy1", "wifi1", "mld1", 1,
                          "/tmp/pktlog_radio1_ap.dat", "ap_pktlog_radio1.dat",
                          "wifi1_pktlog_ap.txt"),
}


def cpe_profile(role: str) -> PktlogProfile:
    # One file name per CPE so parallel captures do not overwrite each other.
    name = role.lower()
    return PktlogProfile(f"{name}_pktlog", "wifi0", "ath0", 0,
                         "/tmp/pktlog_radio0_cpe.dat",
                         f"{name}_pktlog_radio0.dat",
                         f"wifi0_pktlog_{name}.txt")


# ---------------------------------------------------------------------------
# Translator
# ---------------------------------------------------------------------------

CRM_ROOT = r"\\bulk\nsid-hfa-01"
CRM_PATTERN = re.compile(r"^WLAN\.SDX_MRR\.1\.0-(\d+)-QCAHKSWPL_SILICONZ-1$",
                         re.IGNORECASE)
TRANSLATOR_SUBDIR = r"wlan_proc\wlan\mac_core\src\debug_tools\pktlog"
TRANSLATOR_SCRIPT = "pktlog_postproc.py"
TRANSLATOR_DEFS = "pktlog_defs_wkk"
# Interpreter used to run the translator. "python" means the one on PATH.
TRANSLATOR_PYTHON = "python"

# How many translator processes may run at the same time. More files
# than this are queued and started as soon as a slot is free.
TRANSLATE_MAX_PARALLEL = min(8, os.cpu_count() or 4)


def translated_name_for(filename: str) -> str:
    """Suggested translator output name for a pktlog file name.

    Known capture names map to the standard names
    (ap_pktlog_radio0.dat -> wifi0_pktlog_ap.txt,
     cpe3_pktlog_radio0.dat -> wifi0_pktlog_cpe3.txt);
    anything else becomes <name>.txt.
    """
    low = filename.lower()
    for profile in AP_PHY_PROFILES.values():
        if low == profile.default_name:
            return profile.translated_name
    m = re.match(r"^(cpe\d+)_pktlog_radio0\.dat$", low)
    if m:
        return cpe_profile(m.group(1).upper()).translated_name
    stem, ext = os.path.splitext(filename)
    return f"{stem}_translated.txt" if ext.lower() == ".txt" else f"{stem}.txt"

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

APP_NAME = "Log Collector"
ORG_NAME = "logCollector"
LOG_FILE_NAME = "logCollector.log"
