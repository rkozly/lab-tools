"""Per-device table read/write against realistic AP and CPE wireless files."""

import pytest

from devreader.config.definitions import WIRELESS_TABLE
from devreader.config.service import ConfigService
from devreader.config.table import read_table, write_table
from devreader.core.adb_service import DeviceState
from devreader.core.errors import ConfigValidationError
from devreader.core.logging_service import LogService
from devreader.core.simulator import SimulatedAdbService

PATH = "/etc/config/wireless"

AP = b"""config qcawifi 'qcawifi'
\toption rf_freq1_KHz '26660000'
\toption rf_freq2_KHz '27340000'

config wifi-device 'wifi0'
\toption type 'qcawificfg80211'
\toption macaddr '00:03:7f:12:49:63'
\toption channel '161'

config wifi-device 'wifi1'
\toption type 'qcawificfg80211'
\toption macaddr '00:03:7f:12:8d:47'
\toption channel '36'

config wifi-mld 'mld0'
\toption mld_ssid 'LBR_MSTData0'

config wifi-mld 'mld1'
\toption mld_ssid 'LBR_MSTData1'

config wifi-iface
\toption device 'wifi0'
\toption ssid 'ignored-on-ap'
"""

CPE = b"""config qcawifi 'qcawifi'
\toption rf_freq1_KHz '26660000'
\toption rf_freq2_KHz '26660000'

config wifi-device 'wifi0'
\toption type 'qcawificfg80211'
\toption channel '161'
\toption macaddr '00:03:7f:12:09:08'

config wifi-device 'wifi1'
\toption macaddr '00:03:7f:aa:aa:aa'
\toption channel '1'

config wifi-iface
\toption device 'wifi0'
\toption mode 'sta'
\toption ssid 'LBR_MSTData0'
"""


@pytest.fixture
def env(tmp_path):
    log = LogService()
    adb = SimulatedAdbService(log, {"AP1": DeviceState.CONNECTED, "CPE1": DeviceState.CONNECTED}, latency=0)
    adb.devices_sim["AP1"].files[PATH] = AP
    adb.devices_sim["CPE1"].files[PATH] = CPE
    return adb, ConfigService(adb, log, backup_dir=tmp_path)


def values(r):
    return {k: c.value for k, c in r.cells.items()}


def test_read_ap(env):
    _, svc = env
    r = read_table(svc, "AP1", WIRELESS_TABLE, is_ap=True)
    assert values(r) == {
        "rf_freq1": "26660000", "rf_freq2": "27340000",
        "ssid0": "LBR_MSTData0", "ssid1": "LBR_MSTData1",
        "wifi0_channel": "161", "wifi0_mac": "00:03:7f:12:49:63",
        "wifi1_channel": "36", "wifi1_mac": "00:03:7f:12:8d:47",
    }


def test_read_cpe_hides_ap_only_columns(env):
    _, svc = env
    r = read_table(svc, "CPE1", WIRELESS_TABLE, is_ap=False)
    assert values(r) == {
        "rf_freq1": "26660000", "rf_freq2": "26660000",
        "ssid0": "LBR_MSTData0", "ssid1": None,
        "wifi0_channel": "161", "wifi0_mac": "00:03:7f:12:09:08",
        "wifi1_channel": None, "wifi1_mac": None,
    }
    assert r.cells["ssid0"].param.key == "ssid"


def test_write_ap_both_ssids_and_mac(env):
    adb, svc = env
    r = read_table(svc, "AP1", WIRELESS_TABLE, is_ap=True)
    edits = {r.cells["ssid1"].param: "NewSSID1", r.cells["wifi1_mac"].param: "00:03:7f:00:00:01",
             r.cells["wifi0_channel"].param: "149"}
    res = write_table(svc, "AP1", WIRELESS_TABLE, edits)
    assert len(res.changed) == 3
    expected = (AP.replace(b"'LBR_MSTData1'", b"'NewSSID1'")
                .replace(b"'00:03:7f:12:8d:47'", b"'00:03:7f:00:00:01'")
                .replace(b"channel '161'", b"channel '149'"))
    assert adb.devices_sim["AP1"].files[PATH] == expected


def test_write_cpe_ssid(env):
    adb, svc = env
    r = read_table(svc, "CPE1", WIRELESS_TABLE, is_ap=False)
    write_table(svc, "CPE1", WIRELESS_TABLE, {r.cells["ssid0"].param: "Other"})
    assert adb.devices_sim["CPE1"].files[PATH] == CPE.replace(b"ssid 'LBR_MSTData0'", b"ssid 'Other'")


def test_mac_and_channel_validation(env):
    _, svc = env
    r = read_table(svc, "AP1", WIRELESS_TABLE, is_ap=True)
    with pytest.raises(ConfigValidationError):
        r.cells["wifi0_mac"].param.validate("00:03:7f:12:7e:8G")
    with pytest.raises(ConfigValidationError):
        r.cells["wifi0_channel"].param.validate("abc")


# ---------------------------------------------------------------------------
# "disabled" tab: columns discovered from each device's file
# (same layout as the lab AP / CPEs: anonymous wifi-devices before wifi0/wifi1).
# ---------------------------------------------------------------------------
from devreader.config.definitions import TABLES, WIRELESS_DISABLED_TABLE  # noqa: E402
from devreader.config.table import merge_columns  # noqa: E402

AP_DIS = b"""config wifi-device
\toption type 'qcawificfg80211'
\toption disabled '0'

config wifi-device
\toption type 'qcawificfg80211'
\toption disabled '0'

config wifi-device 'wifi0'
\toption type 'qcawificfg80211'
\toption disabled '0'

config wifi-device 'wifi1'
\toption type 'qcawificfg80211'
\toption disabled '0'

config wifi-iface
\toption device 'wifi0'
\toption mode 'ap'
\toption disabled '0'

config wifi-iface
\toption device 'wifi1'
\toption mode 'ap'
\toption disabled '0'

config wifi-mld 'mld0'
\toption mld_ssid 'LBR_MSTData0'

config qcawifi 'qcawifi'
\toption rf_freq1_KHz '26660000'
"""

CPE_DIS = b"""config wifi-device
\toption type 'qcawificfg80211'
\toption disabled '1'

config wifi-device
\toption type 'qcawificfg80211'
\toption disabled '1'

config wifi-device 'wifi0'
\toption type 'qcawificfg80211'
\t# option disabled '0'
\toption disabled '1'

config wifi-iface
\toption device 'wifi0'
\toption mode 'sta'
\toption disabled '1'

config qcawifi 'qcawifi'
\toption rf_freq1_KHz '26660000'
"""

AP_COLS = ["@wifi-device[0]", "@wifi-device[1]", "wifi0", "wifi1", "@wifi-iface[0]", "@wifi-iface[1]"]
CPE_COLS = ["@wifi-device[0]", "@wifi-device[1]", "wifi0", "@wifi-iface[0]"]


@pytest.fixture
def dis_env(env):
    adb, svc = env
    adb.devices_sim["AP1"].files[PATH] = AP_DIS
    adb.devices_sim["CPE1"].files[PATH] = CPE_DIS
    return adb, svc


def test_disabled_tab_registered_after_wireless():
    ids = [t.id for t in TABLES]
    assert ids.index("wireless_disabled_table") == ids.index("wireless_table") + 1
    assert WIRELESS_DISABLED_TABLE.title != WIRELESS_TABLE.title


def test_disabled_columns_match_uci_show(dis_env):
    """Same names and order as `uci show wireless | grep disabled` on the lab devices."""
    _, svc = dis_env
    ap = read_table(svc, "AP1", WIRELESS_DISABLED_TABLE, is_ap=True)
    cpe = read_table(svc, "CPE1", WIRELESS_DISABLED_TABLE, is_ap=False)
    assert [c.id for c in ap.columns] == AP_COLS
    assert [c.id for c in cpe.columns] == CPE_COLS
    assert values(ap) == dict.fromkeys(AP_COLS, "0")
    assert values(cpe) == dict.fromkeys(CPE_COLS, "1")  # the commented-out line is ignored
    assert "wireless.@wifi-iface[0].disabled" in cpe.cells["@wifi-iface[0]"].info
    assert "mode sta" in cpe.cells["@wifi-iface[0]"].info


def test_disabled_columns_merge_in_uci_order(dis_env):
    _, svc = dis_env
    ap = read_table(svc, "AP1", WIRELESS_DISABLED_TABLE, is_ap=True)
    cpe = read_table(svc, "CPE1", WIRELESS_DISABLED_TABLE, is_ap=False)
    for order in ((cpe.columns, ap.columns), (ap.columns, cpe.columns)):
        assert [c.id for c in merge_columns(*order)] == AP_COLS


def test_disabled_choice_is_0_or_1(dis_env):
    _, svc = dis_env
    p = read_table(svc, "AP1", WIRELESS_DISABLED_TABLE, is_ap=True).cells["wifi0"].param
    assert p.choices == ("0", "1")
    assert p.choice_items() == [("0 (enabled)", "0"), ("1 (disabled)", "1")]
    assert p.validate(" 1 ") == "1"
    for bad in ("2", "true", ""):
        with pytest.raises(ConfigValidationError):
            p.validate(bad)


def test_write_disabled_changes_only_those_lines(dis_env):
    adb, svc = dis_env
    r = read_table(svc, "AP1", WIRELESS_DISABLED_TABLE, is_ap=True)
    edits = {r.cells["@wifi-device[1]"].param: "1", r.cells["wifi1"].param: "1",
             r.cells["@wifi-iface[1]"].param: "1"}
    res = write_table(svc, "AP1", WIRELESS_DISABLED_TABLE, edits)
    assert len(res.changed) == 3

    lines = AP_DIS.split(b"\n")
    for n in (7, 15, 25):  # 1-based line numbers of those three "option disabled" lines
        assert lines[n - 1] == b"\toption disabled '0'"
        lines[n - 1] = b"\toption disabled '1'"
    assert adb.devices_sim["AP1"].files[PATH] == b"\n".join(lines)

    after = read_table(svc, "AP1", WIRELESS_DISABLED_TABLE, is_ap=True)
    assert values(after) == {**dict.fromkeys(AP_COLS, "0"),
                             "@wifi-device[1]": "1", "wifi1": "1", "@wifi-iface[1]": "1"}


def test_disabled_index_counts_named_sections(env):
    """@type[N] counts every section of the type, as libuci and UciDocument.find_section do."""
    adb, svc = env
    data = (b"config wifi-device 'wifi0'\n\toption disabled '0'\n\n"
            b"config wifi-device\n\toption disabled '0'\n")
    adb.devices_sim["AP1"].files[PATH] = data
    r = read_table(svc, "AP1", WIRELESS_DISABLED_TABLE, is_ap=True)
    assert [c.id for c in r.columns] == ["wifi0", "@wifi-device[1]"]
    write_table(svc, "AP1", WIRELESS_DISABLED_TABLE, {r.cells["@wifi-device[1]"].param: "1"})
    assert adb.devices_sim["AP1"].files[PATH] == data[:-4] + b"'1'\n"


def test_no_disabled_options_gives_empty_row(env):
    _, svc = env
    r = read_table(svc, "AP1", WIRELESS_DISABLED_TABLE, is_ap=True)  # AP fixture has no "disabled"
    assert r.columns == () and r.cells == {}
