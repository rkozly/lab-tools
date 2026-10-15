from devreader.core.adb_service import DeviceState, parse_devices_output
from devreader.core.discovery import ConnectionStatus, find_env_devices, friendly_name


def test_friendly_name():
    assert friendly_name("ap") == "AP"
    assert friendly_name("cpe1") == "CPE1"


def test_find_env_devices_sorted_and_filtered():
    env = {
        "adb_device_id_cpe2": "S2",
        "ADB_DEVICE_ID_AP": "SAP",
        "adb_device_id_cpe10": "S10",
        "adb_device_id_cpe1": ' "S1" ',
        "adb_device_id_empty": "",
        "PATH": "x",
    }
    devices, warnings = find_env_devices(env)
    assert [d.name for d in devices] == ["AP", "CPE1", "CPE2", "CPE10"]
    assert devices[1].serial == "S1"
    assert any("EMPTY" in w.upper() for w in warnings)


def test_parse_devices_output():
    out = (
        "* daemon not running; starting now at tcp:5037\n"
        "* daemon started successfully\n"
        "List of devices attached\n"
        "AAA\tdevice\n"
        "BBB\tunauthorized\n"
        "CCC\toffline\n"
        "DDD\tno permissions (user in plugdev group); see [http://developer.android.com/tools/device.html]\n"
        "\n"
    )
    devs = parse_devices_output(out)
    assert devs == {
        "AAA": DeviceState.CONNECTED,
        "BBB": DeviceState.UNAUTHORIZED,
        "CCC": DeviceState.OFFLINE,
        "DDD": DeviceState.NO_PERMISSIONS,
    }
    assert ConnectionStatus.from_state(devs["DDD"]) == ConnectionStatus.UNAUTHORIZED
