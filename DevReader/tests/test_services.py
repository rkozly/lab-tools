"""End-to-end tests of the services against the simulated adb."""

import pytest

from devreader.config.definitions import WIRELESS
from devreader.config.service import ConfigService
from devreader.core.adb_service import DeviceState
from devreader.core.errors import (
    ConfigParseError,
    ConfigReadError,
    ConfigVerificationError,
    ConfigWriteError,
    DeviceUnauthorizedError,
)
from devreader.core.logging_service import LogService
from devreader.core.preparation import PreparationService
from devreader.core.simulator import SAMPLE_WIRELESS, SimulatedAdbService

F1, F2 = "qcawifi.rf_freq1_KHz", "qcawifi.rf_freq2_KHz"
PATH = "/etc/config/wireless"


@pytest.fixture
def env(tmp_path):
    log = LogService()
    adb = SimulatedAdbService(log, {"OK1": DeviceState.CONNECTED, "UNA": DeviceState.UNAUTHORIZED}, latency=0)
    return log, adb, ConfigService(adb, log, backup_dir=tmp_path)


def test_read(env):
    _, _, svc = env
    r = svc.read("OK1", WIRELESS)
    assert r.values == {F1: "26000000", F2: "26000000", "@wifi-iface[0].ssid": "Rozaline Lab"}
    assert r.ok


def test_write_preserves_file(env, tmp_path):
    _, adb, svc = env
    res = svc.write("OK1", WIRELESS, {F1: "27000000", F2: "28000000"})
    assert set(res.changed) == {F1, F2}
    assert res.verified == {F1: "27000000", F2: "28000000"}
    expected = (SAMPLE_WIRELESS
                .replace("rf_freq1_KHz '26000000'", "rf_freq1_KHz '27000000'")
                .replace("rf_freq2_KHz '26000000'", "rf_freq2_KHz '28000000'"))
    assert adb.devices_sim["OK1"].files[PATH].decode() == expected
    assert res.backup and res.backup.read_bytes() == SAMPLE_WIRELESS.encode()
    assert "/tmp/.devreader_wireless" not in " ".join(adb.devices_sim["OK1"].files)  # staging removed


def test_write_no_change(env):
    _, adb, svc = env
    res = svc.write("OK1", WIRELESS, {F1: "26000000"})
    assert res.no_change
    assert adb.devices_sim["OK1"].files[PATH] == SAMPLE_WIRELESS.encode()


def test_write_preserves_crlf(env):
    _, adb, svc = env
    adb.devices_sim["OK1"].files[PATH] = SAMPLE_WIRELESS.replace("\n", "\r\n").encode()
    svc.write("OK1", WIRELESS, {F1: "1"})
    data = adb.devices_sim["OK1"].files[PATH]
    assert data == SAMPLE_WIRELESS.replace("\n", "\r\n").replace("rf_freq1_KHz '26000000'", "rf_freq1_KHz '1'").encode()


def test_write_refuses_missing_option(env):
    _, adb, svc = env
    adb.devices_sim["OK1"].files[PATH] = b"config qcawifi 'qcawifi'\n\toption rf_freq2_KHz '5'\n"
    with pytest.raises(ConfigWriteError):
        svc.write("OK1", WIRELESS, {F1: "1"})


def test_read_missing_file(env):
    _, adb, svc = env
    del adb.devices_sim["OK1"].files[PATH]
    with pytest.raises(ConfigReadError):
        svc.read("OK1", WIRELESS)


def test_read_parse_failure(env):
    _, adb, svc = env
    adb.devices_sim["OK1"].files[PATH] = b"config wifi-device 'wifi0'\n\toption channel '36'\n"
    with pytest.raises(ConfigParseError):
        svc.read("OK1", WIRELESS)


def test_unauthorized_fails_fast(env):
    _, _, svc = env
    with pytest.raises(DeviceUnauthorizedError):
        svc.read("UNA", WIRELESS)


def test_verification_failure(env, monkeypatch):
    _, adb, svc = env
    original_dispatch = adb._dispatch

    def corrupting(dev, args, cancel, display):
        rc, out, err = original_dispatch(dev, args, cancel, display)
        if args[0] == "push":  # simulate the device silently mangling the upload
            remote = args[2]
            dev.files[remote] = dev.files[remote].replace(b"27000000", b"99")
        return rc, out, err

    monkeypatch.setattr(adb, "_dispatch", corrupting)
    with pytest.raises(ConfigVerificationError):
        svc.write("OK1", WIRELESS, {F1: "27000000"})


def test_prepare_success_and_failure(env):
    log, adb, _ = env
    prep = PreparationService(adb, log)
    adb.devices_sim["OK1"].booting_until = 0
    progress = []
    report = prep.prepare("OK1", on_progress=lambda s, i, n, t: progress.append((i, t)))
    assert report.ok, report.error
    assert len(report.steps) == 7
    assert progress[-1] == (7, "Done")

    bad = prep.prepare("UNA")
    assert not bad.ok and "unauthorized" in bad.error.lower()
    missing = prep.prepare("NOPE")
    assert not missing.ok and "not connected" in missing.error.lower()


def test_devices_failure_retries_and_reports_detail(env, monkeypatch):
    _, adb, _ = env
    calls = {"devices": 0, "start_server": 0}
    real_execute = adb._execute

    def failing(full, timeout, cancel, display, serial):
        if full[1:] == ["devices"]:
            calls["devices"] += 1
            return 1, b"", b"adb server version (41) doesn't match this client (39); killing...\n"
        return real_execute(full, timeout, cancel, display, serial)

    monkeypatch.setattr(adb, "_execute", failing)
    monkeypatch.setattr(adb, "start_server", lambda timeout=30.0: calls.__setitem__("start_server", calls["start_server"] + 1))
    with pytest.raises(Exception) as exc_info:
        adb.devices()
    assert calls == {"devices": 2, "start_server": 1}
    assert "doesn't match this client" in str(exc_info.value)
