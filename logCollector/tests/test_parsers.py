"""Unit tests for the pure parsing helpers (no devices needed).

    python -m pytest tests
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from logcollector.core.adb import (build_device_rows, order_roles, parse_devices,
                                   roles_from_env)
from logcollector.core.crm import pick_latest
from logcollector.config import AP_PHY_PROFILES, cpe_profile, translated_name_for


def test_parse_devices():
    text = ("* daemon not running; starting now at tcp:5037\n"
            "* daemon started successfully\n"
            "List of devices attached\n"
            "1a2b3c4d\tdevice\n"
            "5e6f7a8b\tunauthorized\n"
            "\n")
    assert parse_devices(text) == {"1a2b3c4d": "device", "5e6f7a8b": "unauthorized"}


def test_parse_devices_empty():
    assert parse_devices("List of devices attached\n\n") == {}


def test_roles_from_env():
    env = {"ADB_DEVICE_ID_AP": "AAA", "adb_device_id_cpe1": " BBB ",
           "adb_device_id_cpe2": "", "PATH": "x"}
    assert roles_from_env(env) == {"AP": "AAA", "CPE1": "BBB"}


def test_build_device_rows():
    rows = build_device_rows({"AP": "AAA", "CPE1": "BBB", "CPE2": "CCC"},
                             {"AAA": "device", "ZZZ": "device"})
    assert rows == [("AP", "AAA", "device"),
                    ("CPE1", "BBB", "missing"),
                    ("CPE2", "CCC", "missing"),
                    ("(unassigned)", "ZZZ", "device")]


def test_only_configured_cpes_are_listed():
    # A 2-CPE setup must not show CPE3; a 16-CPE setup shows all 16 in order.
    two = [r[0] for r in build_device_rows({"AP": "a", "CPE1": "b", "CPE2": "c"}, {})]
    assert two == ["AP", "CPE1", "CPE2"]
    env = {f"adb_device_id_cpe{i}": f"s{i}" for i in range(16, 0, -1)}
    sixteen = [r[0] for r in build_device_rows(roles_from_env(env), {})]
    assert sixteen == [f"CPE{i}" for i in range(1, 17)]


def test_order_roles():
    assert order_roles(["CPE10", "LAB", "CPE2", "AP", "CPE1"]) == \
        ["AP", "CPE1", "CPE2", "CPE10", "LAB"]


def test_translated_names():
    assert translated_name_for("ap_pktlog_radio0.dat") == "wifi0_pktlog_ap.txt"
    assert translated_name_for("ap_pktlog_radio1.dat") == "wifi1_pktlog_ap.txt"
    assert translated_name_for("cpe1_pktlog_radio0.dat") == "wifi0_pktlog_cpe1.txt"
    assert translated_name_for("CPE16_pktlog_radio0.dat") == "wifi0_pktlog_cpe16.txt"
    assert translated_name_for("my_capture.dat") == "my_capture.txt"


def test_pick_latest_crm():
    names = ["WLAN.SDX_MRR.1.0-00099-QCAHKSWPL_SILICONZ-1",
             "WLAN.SDX_MRR.1.0-00123-QCAHKSWPL_SILICONZ-1",
             "WLAN.SDX_MRR.1.0-1000-QCAHKSWPL_SILICONZ-1",
             "WLAN.SDX_MRR.1.0-99999-QCAHKSWPL_SILICONZ-2",
             "other"]
    assert pick_latest(names) == "WLAN.SDX_MRR.1.0-1000-QCAHKSWPL_SILICONZ-1"
    assert pick_latest(["nothing"]) is None


def test_pktlog_commands():
    phy1 = AP_PHY_PROFILES["PHY1"]
    assert phy1.setup_commands() == [
        "pktlogconf -r wifi1",
        "pktlogconf -a wifi1 -elite,rcu,rcf -s 30000000",
        "wifitool mld1 link_id 1 setUnitTestCmd 0x48 2 323 1",
        "wifitool mld1 link_id 1 setUnitTestCmd 0x48 2 322 2",
        "wifitool mld1 link_id 1 setUnitTestCmd 0x48 2 322 7",
    ]
    assert phy1.copy_command() == "cp /proc/ath_pktlog/wifi1 /tmp/pktlog_radio1_ap.dat"
    cpe = cpe_profile("CPE2")
    assert cpe.setup_commands()[2] == "wifitool ath0 link_id 0 setUnitTestCmd 0x48 2 323 1"
    assert cpe.remote_file == "/tmp/pktlog_radio0_cpe.dat"


def test_parse_mobaxterm_sessions():
    from logcollector.core.serial_ports import parse_mobaxterm_sessions
    ini = ("[Misc]\n"
           "Something=COM99\n"
           "[Bookmarks]\n"
           "SubRep=\n"
           "AP - COM33=#131#8%-2%115200%3%0%0%1%2%COM33  (USB Serial Port (COM33))%0\n"
           "CPE2 - COM12  (USB Serial Port (COM12))=#131#8%-2%115200\n"
           "[Bookmarks_1]\n"
           "cpe10 - console=#131#8%-2%115200%3%0%0%1%2%COM7  (USB)%0\n"
           "myserver=#109#0%host%22\n")
    assert parse_mobaxterm_sessions(ini) == {"AP": "COM33", "CPE2": "COM12",
                                             "CPE10": "COM7"}


def test_serial_ports_from_env():
    from logcollector.core.serial_ports import ports_from_env
    env = {"SERIAL_PORT_AP": "com3", "serial_port_cpe1": " COM8 ",
           "serial_port_cpe2": "", "PATH": "x"}
    assert ports_from_env(env) == {"AP": "COM3", "CPE1": "COM8"}


def test_serial_timestamps_split_crlf():
    from logcollector.tasks.serial_task import SerialTask
    t = SerialTask.__new__(SerialTask)
    t._line_start = True
    out = b"".join(t._stamp(d) for d in (b"line one\r", b"\nline ", b"two\r\n", b"x"))
    lines = out.split(b"\n")
    assert [l[22:] for l in lines] == [b"line one\r", b"line two\r", b"x"]
    assert all(l.startswith(b"[") for l in lines)


# ----- wpa_supplicant / hostapd -------------------------------------------------

_WPA_INIT = (
    "start_service() {\n"
    "\tprocd_open_instance\n"
    "\tprocd_set_param command /usr/sbin/wpa_supplicant -g $WPAD_VARRUN/wpa_supplicantglobal "
    "-B -P $WPAD_VARRUN/wpa_supplicant-global.pid\n"
    "\t# wpa_supplicant -g $WPAD_VARRUN/wpa_supplicantglobal -B -P $WPAD_VARRUN/wpa_supplicant-global.pid\n"
    "}\n"
    "reload() {\n"
    "    wpa_supplicant  -g $WPAD_VARRUN/wpa_supplicantglobal -B -P $WPAD_VARRUN/wpa_supplicant-global.pid\n"
    "}\n")


def test_wpa_edit_applied_once():
    from logcollector.config import WPA_PROFILE
    from logcollector.core.daemon_config import DONE, NEEDED, apply_edits, edit_status
    (edit,) = WPA_PROFILE.edits
    assert edit_status(_WPA_INIT, edit) == NEEDED
    new = apply_edits(_WPA_INIT, WPA_PROFILE.edits)
    lines = new.splitlines()
    assert lines[2].endswith("wpa_supplicant-global.pid -tddd -f /tmp/wpa_supplicant.log")
    assert lines[2].startswith("\tprocd_set_param command /usr/sbin/")
    assert lines[3] == _WPA_INIT.splitlines()[3]          # comment untouched
    assert lines[6].endswith("-tddd -f /tmp/wpa_supplicant.log")
    assert edit_status(new, edit) == DONE
    assert apply_edits(new, WPA_PROFILE.edits) == new     # re-run: no change


def test_hostapd_edits():
    from logcollector.config import HOSTAPD_PROFILE
    from logcollector.core.daemon_config import DONE, MISSING, NEEDED, apply_edits, edit_status
    init_edit, sh_edit = HOSTAPD_PROFILE.edits
    init = ("procd_set_param command /usr/sbin/hostapd -g $WPAD_VARRUN/hostapd/global -B "
            "-P $WPAD_VARRUN/hostapd-global.pid -f /tmp/hostapd.txt\r\n")
    sh = ('\t\t[ -n "$ifname" ] && run_hostapd_cli $ifname "$WPAD_VARRUN/hostapd-$device" '
          '"close_log"\n')
    assert edit_status(init, init_edit) == NEEDED
    assert edit_status(sh, sh_edit) == NEEDED
    new_init = apply_edits(init, [init_edit])
    assert "/usr/sbin/hostapd -dddKt -g $WPAD_VARRUN/hostapd/global -B" in new_init
    assert new_init.endswith("\r\n")
    new_sh = apply_edits(sh, [sh_edit])
    assert new_sh.startswith('\t\t##[ -n "$ifname" ]')
    assert edit_status(new_init, init_edit) == DONE
    assert edit_status(new_sh, sh_edit) == DONE
    assert apply_edits(new_sh, [sh_edit]) == new_sh
    assert edit_status("something else\n", init_edit) == MISSING


def test_daemon_process_flags():
    from logcollector.core.daemon_config import has_flags, pick_global_process
    out = ("/usr/sbin/wpa_supplicant -i ath0 -c /var/run/x.conf \n"
           "/usr/sbin/wpa_supplicant -g /var/run/wpa_supplicantglobal -B -P /var/run/p.pid "
           "-tddd -f /tmp/wpa_supplicant.log \n")
    cmd = pick_global_process(out)
    assert cmd.startswith("/usr/sbin/wpa_supplicant -g")
    assert has_flags(cmd, ["-tddd", "-f /tmp/wpa_supplicant.log"])
    assert not has_flags(cmd, ["-dddKt"])
    assert not has_flags("/usr/sbin/hostapd -g x", ["-dddKt"])
    assert pick_global_process("") is None
