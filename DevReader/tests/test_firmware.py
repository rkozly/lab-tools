"""Flash wlan_wkk: AP vs CPE image sets, missing files, adb command order."""

import pytest

from devreader.core.adb_service import DeviceState
from devreader.config.jsoncfg import JsonDocument, set_string_value
from devreader.core.firmware import VER_INFO_PATH, WLAN_FILES, WLAN_TARGET_DIR, FirmwareError, FirmwareService
from devreader.core.logging_service import LogService
from devreader.core.simulator import SimulatedAdbService


VER_INFO = (
    b'{\r\n    "Image_Build_IDs": {\r\n        "apps": "OWRT.PRODUCT.3.1-34500-sdx85.1-1",\r\n'
    b'        "wlan_wkk": "lbr_images_ci"\r\n    },\r\n    "Metabuild_Info": {\r\n'
    b'        "Meta_Build_ID": "Tarang.LE.1.0-00111-EBOOT.CPE.WKK-1",\r\n        "Product_Flavor": null\r\n'
    b'    }\r\n}\r\n'
)
BUILD = "WLAN.SDX_MRR.1.0-00374-QCAHKSWPL_SILICONZ-1"


@pytest.fixture
def env(tmp_path):
    log = LogService()
    adb = SimulatedAdbService(log, {"AP1": DeviceState.CONNECTED, "CPE1": DeviceState.CONNECTED}, latency=0)
    calls = []
    real_run = adb.run

    def run(args, **kw):
        calls.append((kw.get("serial"), list(args)))
        return real_run(args, **kw)

    adb.run = run
    for dev in adb.devices_sim.values():
        dev.files[VER_INFO_PATH] = VER_INFO
    return adb, FirmwareService(adb, log, backup_dir=tmp_path / "backups"), calls


def make_build(tmp_path, names):
    folder = tmp_path / BUILD
    folder.mkdir()
    for n in names:
        (folder / n).write_bytes(n.encode())
    return folder


@pytest.mark.parametrize("serial,is_ap,role", [("AP1", True, "ap"), ("CPE1", False, "cpe")])
def test_flash_pushes_role_files_then_reboots(env, tmp_path, serial, is_ap, role):
    adb, svc, calls = env
    folder = make_build(tmp_path, WLAN_FILES["ap"] + WLAN_FILES["cpe"])
    steps = []
    svc.flash_wlan(serial, folder, is_ap=is_ap, on_progress=lambda s, i, n, t: steps.append(t))

    cmds = [a for s, a in calls if s == serial and a != ["devices"]]
    assert cmds[0] == ["wait-for-device", "root"]
    assert cmds[1] == ["wait-for-device", "shell", "setenforce 0"]
    assert cmds[2] == ["wait-for-device", "shell", "mount -o remount,rw /firmware"]
    pushes = [a for a in cmds if a[0] == "push" and a[2] == WLAN_TARGET_DIR]
    assert [a[1].rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for a in pushes] == list(WLAN_FILES[role])
    assert all(a[2] == WLAN_TARGET_DIR for a in pushes)
    assert cmds[-2] == ["wait-for-device", "shell", "mount -o remount,ro /firmware"]
    assert cmds[-1] == ["reboot"]
    assert steps[-1] == "Rebooting"
    assert "Update ver_info.txt" in steps

    data = adb.devices_sim[serial].files[VER_INFO_PATH]
    assert data == VER_INFO.replace(b'"lbr_images_ci"', f'"{BUILD}"'.encode())
    assert JsonDocument.from_bytes(data).get("Image_Build_IDs", "wlan_wkk") == BUILD
    backups = list((tmp_path / "backups").rglob("ver_info_*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == VER_INFO


def test_missing_files_abort_before_touching_device(env, tmp_path):
    adb, svc, calls = env
    folder = make_build(tmp_path, WLAN_FILES["cpe"][:2])
    with pytest.raises(FirmwareError, match="m3.bin, bdwlan.b0004"):
        svc.flash_wlan("CPE1", folder, is_ap=False)
    assert not [a for s, a in calls if s == "CPE1"]


def test_set_string_value_only_touches_that_value():
    out = set_string_value(VER_INFO, "Image_Build_IDs", "wlan_wkk", BUILD)
    assert out == VER_INFO.replace(b"lbr_images_ci", BUILD.encode())
    with pytest.raises(KeyError):
        set_string_value(VER_INFO, "Image_Build_IDs", "missing", "x")
    with pytest.raises(KeyError):
        set_string_value(VER_INFO, "Metabuild_Info", "Product_Flavor", "x")  # null, not a string


def test_set_string_value_same_key_in_other_section_is_not_touched():
    data = b'{"A": {"wlan_wkk": "keep"}, "Image_Build_IDs": {"wlan_wkk": "old"}}'
    out = set_string_value(data, "Image_Build_IDs", "wlan_wkk", "new")
    assert out == b'{"A": {"wlan_wkk": "keep"}, "Image_Build_IDs": {"wlan_wkk": "new"}}'


def test_flash_without_ver_info_update(env, tmp_path):
    adb, svc, calls = env
    folder = make_build(tmp_path, WLAN_FILES["cpe"])
    svc.flash_wlan("CPE1", folder, is_ap=False, update_ver_info=False)
    assert adb.devices_sim["CPE1"].files[VER_INFO_PATH] == VER_INFO
