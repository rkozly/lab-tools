"""Editing scan_freq in /lib/wifi/wpa_supplicant.sh keeps every other byte."""

import pytest

from devreader.config.definitions import WPA_SUPPLICANT_TABLE
from devreader.config.service import ConfigService
from devreader.config.table import read_table, write_table
from devreader.config.wpa import WpaNetworkDocument
from devreader.core.adb_service import DeviceState
from devreader.core.logging_service import LogService
from devreader.core.simulator import SimulatedAdbService

PATH = WPA_SUPPLICANT_TABLE.remote_path

SCRIPT = (
    b"#!/bin/sh\r\n"
    b"cat > $CONF <<EOF\r\n"
    b"network={\r\n"
    b"\tssid=\"${ssid}\"\r\n"
    b"\tscan_freq=6595\r\n"
    b"}\r\n"
    b"EOF\r\n"
    b"echo \"network={\" >> $CONF\r\n"
    b"echo \"\tscan_freq=6595\" >> $CONF\r\n"
    b"echo \"}\" >> $CONF\r\n"
)


def test_set_replaces_only_value():
    doc = WpaNetworkDocument.from_bytes(SCRIPT)
    assert doc.set("network[0]", "scan_freq", "6755")
    assert doc.set("network[1]", "scan_freq", "5180")
    assert doc.to_bytes() == (SCRIPT.replace(b"\tscan_freq=6595\r\n", b"\tscan_freq=6755\r\n")
                              .replace(b"scan_freq=6595\"", b"scan_freq=5180\""))
    assert doc.get("network[1]", "scan_freq") == "5180"
    assert doc.changed_lines == {4, 8}


def test_set_rejects_shell_characters_and_missing_key():
    doc = WpaNetworkDocument.from_bytes(SCRIPT)
    for bad in ("6595 6755", "1\"; rm", "$X"):
        with pytest.raises(ValueError):
            doc.set("network[0]", "scan_freq", bad)
    with pytest.raises(KeyError):
        doc.set("network[0]", "bssid", "1")
    assert not doc.set("network[0]", "scan_freq", "6595")


@pytest.fixture
def env(tmp_path):
    log = LogService()
    adb = SimulatedAdbService(log, {"CPE1": DeviceState.CONNECTED}, latency=0)
    adb.devices_sim["CPE1"].files[PATH] = SCRIPT
    return adb, ConfigService(adb, log, backup_dir=tmp_path)


def test_write_table_scan_freq(env, tmp_path):
    adb, svc = env
    r = read_table(svc, "CPE1", WPA_SUPPLICANT_TABLE, is_ap=False)
    p = r.cells["scan_freq0"].param
    assert not p.read_only and p.validate("6755") == "6755"
    result = write_table(svc, "CPE1", WPA_SUPPLICANT_TABLE, {p: "6755"})
    assert result.changed and result.backup and result.backup.read_bytes() == SCRIPT
    data = adb.devices_sim["CPE1"].files[PATH]
    assert b"\tscan_freq=6755\r\n" in data and b"scan_freq=6595\"" in data
    assert read_table(svc, "CPE1", WPA_SUPPLICANT_TABLE, is_ap=False).cells["scan_freq0"].value == "6755"


def test_read_only_fs_is_remounted_and_write_retried(env):
    adb, svc = env
    dev = adb.devices_sim["CPE1"]
    dev.read_only = ("/lib/",)
    p = read_table(svc, "CPE1", WPA_SUPPLICANT_TABLE, is_ap=False).cells["scan_freq0"].param
    write_table(svc, "CPE1", WPA_SUPPLICANT_TABLE, {p: "6755"})
    assert dev.rooted and dev.read_only == ()
    assert b"\tscan_freq=6755\r\n" in dev.files[PATH]


def test_read_only_fs_with_verity_asks_for_prepare(env):
    from devreader.core.errors import ConfigPushError

    adb, svc = env
    dev = adb.devices_sim["CPE1"]
    dev.read_only, dev.verity_disabled = ("/lib/",), False
    p = read_table(svc, "CPE1", WPA_SUPPLICANT_TABLE, is_ap=False).cells["scan_freq0"].param
    with pytest.raises(ConfigPushError, match="Run Prepare"):
        write_table(svc, "CPE1", WPA_SUPPLICANT_TABLE, {p: "6755"})
    assert dev.files[PATH] == SCRIPT
