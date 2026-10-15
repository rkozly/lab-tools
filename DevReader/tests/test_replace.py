"""Tests for the whole-file replace feature against the simulated adb."""

import pytest

from devreader.config.replace import FileReplaceService, summarize, validate_remote_path
from devreader.core.adb_service import DeviceState
from devreader.core.errors import ConfigReadError, ConfigValidationError, DeviceUnauthorizedError
from devreader.core.logging_service import LogService
from devreader.core.simulator import SAMPLE_WIRELESS, SimulatedAdbService

PATH = "/etc/config/wireless"
NEW = b"""config qcawifi 'qcawifi'
\toption rf_freq1_KHz '27000000'
\toption rf_freq2_KHz '28000000'

config wifi-device 'wifi0'
\toption macaddr 'aa:bb:cc:00:00:01'

config wifi-device 'wifi1'
\toption macaddr 'aa:bb:cc:00:00:02'

config wifi-iface
\toption mld_ssid 'NewMLD'
"""


@pytest.fixture
def env(tmp_path):
    log = LogService()
    adb = SimulatedAdbService(log, {"OK1": DeviceState.CONNECTED, "UNA": DeviceState.UNAUTHORIZED}, latency=0)
    return adb, FileReplaceService(adb, log, backup_dir=tmp_path)


def test_summarize_collects_all_sections():
    values = summarize(NEW)
    assert values == {
        "rf_freq1_KHz": ["27000000"],
        "rf_freq2_KHz": ["28000000"],
        "mld_ssid": ["NewMLD"],
        "macaddr": ["aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"],
    }


def test_summarize_missing_keys():
    assert summarize(b"config foo\n\toption bar '1'\n")["mld_ssid"] == []


def test_read_summary(env):
    _, svc = env
    s = svc.read_summary("OK1", PATH)
    assert s.values["rf_freq1_KHz"] == ["26000000"]
    assert s.values["mld_ssid"] == []


def test_replace_writes_backup_and_verifies(env):
    adb, svc = env
    res = svc.replace("OK1", PATH, NEW)
    assert adb.devices_sim["OK1"].files[PATH] == NEW
    assert res.backup and res.backup.read_bytes() == SAMPLE_WIRELESS.encode()
    assert res.values["macaddr"] == ["aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"]
    assert not res.no_change


def test_replace_identical_is_noop(env):
    _, svc = env
    res = svc.replace("OK1", PATH, SAMPLE_WIRELESS.encode())
    assert res.no_change and res.backup is None


def test_replace_refuses_missing_remote_file(env):
    adb, svc = env
    with pytest.raises(ConfigReadError):
        svc.replace("OK1", "/etc/config/nope", NEW)
    assert "/etc/config/nope" not in adb.devices_sim["OK1"].files


def test_replace_refuses_empty_local(env):
    _, svc = env
    with pytest.raises(ConfigValidationError):
        svc.replace("OK1", PATH, b"  \n")


def test_replace_unauthorized(env):
    _, svc = env
    with pytest.raises(DeviceUnauthorizedError):
        svc.replace("UNA", PATH, NEW)


@pytest.mark.parametrize("bad", ["etc/config/wireless", "/etc/config/", "/etc/x'y", "/tmp/a;rm -rf /", ""])
def test_validate_remote_path_rejects(bad):
    with pytest.raises(ConfigValidationError):
        validate_remote_path(bad)
