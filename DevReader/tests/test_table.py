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
