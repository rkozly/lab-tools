"""
Quick commands: buttons on each AP / CPE shell pane in the Console, and the
watch commands of the Stats tab (STATS).

kind: check  type the command; PASS/FAIL from the output (CommandCheck)
      qcmap  QCMAP_CLI with these key groups ("1-2-1 4-29-1 4-1-1"); each
             group must print a success word, as in the QCMAP sequences
      send   just type it into the shell (e.g. a watch command)
      exec   run it over a second connection, so it works while the shell is
             busy (e.g. killall dhcpd while dhcpd -d runs in the foreground)
      peer   ping / iperf to the other side: LabHub reads the IPv6 address of
             the peer's data interface on its BB (ifconfig), puts it in {peer}
             and types the command (AP BB -> a CPE, CPE BB -> the AP)
      ui     opens a LabHub window (routing table); not editable

The text of every button can be changed in the GUI (right-click); changes
are kept in the user file under `quick_commands` (key -> text).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

KINDS = ("check", "qcmap", "send", "exec", "peer", "ui")


@dataclass
class QuickCommand:
    key: str        # e.g. "cpe.scan_a"
    label: str
    kind: str
    default: str


def _cpe_scan(freq: int) -> str:
    return (f"cfg80211tool wifi0 freq_scan_num_rf_channels 1\n"
            f"cfg80211tool wifi0 freq_scan_rf_channel_0 {freq}\n"
            f"cfg80211tool wifi0 freq_scan_rf_channel_mask_0 256\n"
            f"cfg80211tool wifi0 hw_ver_override 2")


AP_STATS = r'''watch -n 1 "wifistats wifi0 9 | grep -E 'Legacy OFDM Rates'; wifistats wifi1 9 | grep -E 'Legacy OFDM Rates'; wifistats wifi0 9 | grep -E '^tx_mcs'; wifistats wifi1 9 | grep -E '^tx_mcs'; echo ''; wifistats wifi0 10 | grep -E '^rx_bw|^rx_mcs|^rx_legacy_ofdm_rate|^rssi_in_dbm|pilot_evm_dB\[0\]|pilot_evm_dB\[1\]|la'; echo; wifistats wifi1 10 | grep -E '^rx_bw|^rx_mcs|^rx_legacy_ofdm_rate|^rssi_in_dbm|pilot_evm_dB\[0\]|pilot_evm_dB\[1\]|la'; echo; wlanconfig mld0 list; wlanconfig mld1 list"'''

CPE_STATS = r'''watch -n 1 "wifistats wifi0 9 | grep -E '^tx_mcs'; echo''; wifistats wifi0 10 | grep -E '^rx_bw|^rx_mcs|^rx_legacy_ofdm_rate|^rssi_in_dbm|pilot_evm_dB\[0\]|pilot_evm_dB\[1\]|la' ; echo ''; ifconfig ath0; echo ''; iw ath0 link"'''

# Traffic between the BBs, through AP <-> CPE. {peer} = the other side's address
# (a link-local one gets %<this BB's data iface> added). iperf2 [SUM] lines feed Graphs.
IPERF_SERVER = "iperf -s -V -u -i 1"
IPERF_CLIENT = "iperf -c {peer} -V -u -b 1000M -t 60 -i 1 -P 4"

# Opens the routing table window (add / remove routes are in there).
BB_ROUTES = [QuickCommand("bb.routes", "Routing table", "ui", "show")]

QUICK: dict[str, list[QuickCommand]] = {
    "ap": [
        QuickCommand("ap.scan", "Short scan", "check", "cfg80211tool wifi0 hw_ver_override 2"),
        QuickCommand("ap.qcmap", "QCMAP bridge", "qcmap", "1-2-1 4-29-1 4-1-1"),
    ],
    "cpe": [
        QuickCommand("cpe.scan_a", "Short scan A", "check", _cpe_scan(26660)),
        QuickCommand("cpe.scan_b", "Short scan B", "check", _cpe_scan(27000)),
        QuickCommand("cpe.scan_c", "Short scan C", "check", _cpe_scan(27340)),
        QuickCommand("cpe.qcmap", "QCMAP bridge", "qcmap", "1-2-1 4-33-1 4-1-1"),
    ],
    # Backbones. {ctrl} / {data} / {ifaces} = the device's BB interfaces from
    # Mappings (default <name>.960 control; data <name>.1037 on the AP, <name>.1015 on a CPE).
    "ap-bb": [
        QuickCommand("bb.ifconfig", "ifconfig", "send", "ifconfig"),
        QuickCommand("apbb.dhcpd", "Run DHCP server", "send",
                     "dhcpd -6 -d -cf /etc/dhcp/dhcpd6_combined.conf {ctrl} {data}"),
        QuickCommand("apbb.kill_dhcpd", "Kill DHCP server", "exec", "killall dhcpd"),
        QuickCommand("apbb.ping", "Ping CPE", "peer", "ping -6 -c 20 {peer}"),
        QuickCommand("apbb.iperf_c", "iperf → CPE", "peer", IPERF_CLIENT),
        QuickCommand("apbb.iperf_s", "iperf server", "send", IPERF_SERVER),
        *BB_ROUTES,
    ],
    "cpe-bb": [
        QuickCommand("bb.ifconfig", "ifconfig", "send", "ifconfig"),
        QuickCommand("cpebb.dhclient", "dhclient", "send", "dhclient -6 -v {ctrl} {data}"),
        QuickCommand("cpebb.kill_dhclient", "Kill dhclient", "exec", "killall dhclient"),
        QuickCommand("cpebb.ping", "Ping AP", "peer", "ping -6 -c 20 {peer}"),
        QuickCommand("cpebb.iperf_c", "iperf → AP", "peer", IPERF_CLIENT),
        QuickCommand("cpebb.iperf_s", "iperf server", "send", IPERF_SERVER),
        *BB_ROUTES,
    ],
}

CONTROL_VLAN = "960"
DATA_VLAN = {"ap": "1037", "cpe": "1015"}     # data VLAN per role; anything else -> cpe's


def data_vlan(role: str) -> str:
    return DATA_VLAN.get(role, DATA_VLAN["cpe"])


def default_interfaces(name: str, role: str = "cpe") -> list[str]:
    """BB interfaces of a device when Mappings has none: CPE1 -> cpe1.960, cpe1.1015;
    AP -> ap.960, ap.1037. (Commands use the BB's own spelling once it is known, e.g. CPE1.960.)"""
    return [f"{name.lower()}.{CONTROL_VLAN}", f"{name.lower()}.{data_vlan(role)}"]


def bb_interfaces(device_name: str, interfaces: list[str], role: str = "cpe") -> list[str]:
    """[control, data] of a device on its BB."""
    v = bb_values(device_name, interfaces, role=role)
    return [i for i in (v["ctrl"], v["data"]) if i]


def bb_values(device_name: str, interfaces: list[str], actual: dict[str, str] | None = None,
              role: str = "cpe") -> dict[str, str]:
    """actual: lower-case name -> name as the BB spells it (from `ip link`)."""
    ifaces = list(interfaces) or default_interfaces(device_name, role)
    if actual:
        ifaces = [actual.get(i.lower(), i) for i in ifaces]
    ctrl = next((i for i in ifaces if i.endswith(f".{CONTROL_VLAN}")), ifaces[0])
    rest = [i for i in ifaces if i != ctrl]
    data = next((i for i in rest if i.endswith(f".{data_vlan(role)}")), rest[0] if rest else "")
    return {"ctrl": ctrl, "data": data, "ifaces": " ".join(ifaces), "dev": device_name}

# Stats tab: one extra shell per AP / CPE that is open in the Console, running this.
STATS: dict[str, QuickCommand] = {
    "ap": QuickCommand("ap.stats", "⟳ Restart", "send", AP_STATS),
    "cpe": QuickCommand("cpe.stats", "⟳ Restart", "send", CPE_STATS),
}


def for_role(role: str) -> list[QuickCommand]:
    return list(QUICK.get(role, []))


def find(key: str) -> QuickCommand | None:
    every = [c for cmds in QUICK.values() for c in cmds] + list(STATS.values())
    return next((c for c in every if c.key == key), None)


def text_of(cmd: QuickCommand, overrides: dict | None) -> str:
    return (overrides or {}).get(cmd.key) or cmd.default


def parse_groups(text: str) -> list[list[str]]:
    """'1-2-1 4-29-1 4-1-1' -> [['1','2','1'], ['4','29','1'], ['4','1','1']]."""
    groups = [[k for k in g.split("-") if k] for g in re.split(r"[\s,;]+", text.strip()) if g]
    return [g for g in groups if g]
