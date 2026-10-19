"""ver_info.txt (JSON) table: build IDs per device."""

import pytest

from devreader.config.definitions import VER_INFO_TABLE
from devreader.config.service import ConfigService
from devreader.config.table import read_table
from devreader.core.adb_service import DeviceState
from devreader.core.errors import ConfigParseError
from devreader.core.logging_service import LogService
from devreader.core.simulator import SimulatedAdbService

PATH = VER_INFO_TABLE.remote_path

VER_INFO = b"""{
    "Image_Build_IDs": {
        "aop": "AOP.HO.5.0.1-00105-KOBUK_E-1",
        "apps": "OWRT.PRODUCT.3.1-34500-sdx85.1-1",
        "modem": "MPSS.DE.9.0.c4-00055-TARANG_GEN_TEST-1",
        "tz_apps": "TZ.APPS.1.28-00353-KOBUKLENAZT-1",
        "wlan_wkk": "lbr_images_ci"
    },
    "Metabuild_Info": {
        "Meta_Build_ID": "Tarang.LE.1.0-00111-EBOOT.CPE.WKK-1",
        "Product_Flavor": null,
        "Time_Stamp": "2026-09-27 20:53:08"
    }
}
"""


@pytest.fixture
def svc(tmp_path):
    log = LogService()
    adb = SimulatedAdbService(log, {"AP1": DeviceState.CONNECTED}, latency=0)
    adb.devices_sim["AP1"].files[PATH] = VER_INFO
    return adb, ConfigService(adb, log, backup_dir=tmp_path)


def test_read_build_ids(svc):
    _, s = svc
    r = read_table(s, "AP1", VER_INFO_TABLE, is_ap=True)
    assert {k: c.value for k, c in r.cells.items()} == {
        "meta_build": "Tarang.LE.1.0-00111-EBOOT.CPE.WKK-1",
        "apps": "OWRT.PRODUCT.3.1-34500-sdx85.1-1",
        "modem": "MPSS.DE.9.0.c4-00055-TARANG_GEN_TEST-1",
        "wlan_wkk": "lbr_images_ci",
        "tz_apps": "TZ.APPS.1.28-00353-KOBUKLENAZT-1",
    }


def test_missing_key_is_na(svc):
    adb, s = svc
    adb.devices_sim["AP1"].files[PATH] = b'{"Image_Build_IDs": {"apps": "x"}}'
    r = read_table(s, "AP1", VER_INFO_TABLE, is_ap=True)
    assert r.cells["apps"].value == "x"
    assert r.cells["modem"].param is None and r.cells["meta_build"].param is None


def test_invalid_json_fails(svc):
    adb, s = svc
    adb.devices_sim["AP1"].files[PATH] = b"not json"
    with pytest.raises(ConfigParseError):
        read_table(s, "AP1", VER_INFO_TABLE, is_ap=True)
