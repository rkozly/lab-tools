"""Registry of managed configuration files and their parameters.

To manage a new parameter, append an entry to the relevant ``*_PARAMETERS``
list. To manage a new file, create a :class:`ConfigFileDef` and add it to
:data:`CONFIG_FILES` - the GUI adds a tab for it automatically.

Example future additions (uncomment / adapt once the section/keys are known)::

    {"section": "wifi0", "section_type": "wifi-device", "key": "channel",
     "label": "Channel", "type": "int", "min": 0, "max": 233, "group": "Radio"},
    {"section": "wifi0", "section_type": "wifi-device", "key": "country",
     "label": "Country Code", "type": "string", "pattern": "[A-Z]{2}|[0-9]{3}"},
"""

from __future__ import annotations

from devreader.config.parameters import ConfigFileDef, params_from_dicts

WIRELESS_PARAMETERS = [
    {
        "section": "qcawifi",
        "section_type": "qcawifi",
        "key": "rf_freq1_KHz",
        "label": "RF Frequency 1 (KHz)",
        "type": "int",
        "unit": "KHz",
        "min": 0,
        "group": "RF",
        "description": "option rf_freq1_KHz in config qcawifi 'qcawifi'",
    },
    {
        "section": "qcawifi",
        "section_type": "qcawifi",
        "key": "rf_freq2_KHz",
        "label": "RF Frequency 2 (KHz)",
        "type": "int",
        "unit": "KHz",
        "min": 0,
        "group": "RF",
        "description": "option rf_freq2_KHz in config qcawifi 'qcawifi'",
    },
    {
        # First wifi-iface section in the file; the first match wins.
        "section": "@wifi-iface[0]",
        "key": "ssid",
        "label": "SSID",
        "type": "string",
        "group": "Interface",
        "read_only": True,
        "required": False,
        "description": "option ssid in the first config wifi-iface section",
    },
]

WIRELESS = ConfigFileDef(
    id="wireless",
    title="Wireless Configuration",
    remote_path="/etc/config/wireless",
    parameters=params_from_dicts(WIRELESS_PARAMETERS),
    description="UCI wireless configuration (/etc/config/wireless)",
)

# scan_freq inside the network={...} blocks of the supplicant script.
# Absent values are expected and shown as N/A.
WPA_SUPPLICANT_PARAMETERS = [
    {
        "section": f"network[{i}]",
        "key": "scan_freq",
        "label": f"Network {i + 1} scan_freq",
        "type": "string",
        "group": "Network blocks",
        "read_only": True,
        "required": False,
        "placeholder": "N/A",
        "description": f"scan_freq= in network block {i + 1}",
    }
    for i in range(2)
]

WPA_SUPPLICANT = ConfigFileDef(
    id="wpa_supplicant",
    title="WPA Supplicant",
    remote_path="/lib/wifi/wpa_supplicant.sh",
    parameters=params_from_dicts(WPA_SUPPLICANT_PARAMETERS),
    format="wpa_networks",
    read_only=True,
    description="scan_freq values from the network={...} blocks",
)

CONFIG_FILES: tuple[ConfigFileDef, ...] = (WIRELESS, WPA_SUPPLICANT)


def get_config_file(file_id: str) -> ConfigFileDef:
    for cfg in CONFIG_FILES:
        if cfg.id == file_id:
            return cfg
    raise KeyError(file_id)


# ----------------------------------------------------------------------
# Per-device tables (one tab per remote file). See devreader.config.table.
# ----------------------------------------------------------------------
from devreader.config.table import MAC_PATTERN, TableDef, column  # noqa: E402

_CHANNEL = {"type": "int", "min": 0, "max": 233}
_MAC = {"pattern": MAC_PATTERN}

WIRELESS_TABLE = TableDef(
    id="wireless_table",
    remote_path="/etc/config/wireless",
    columns=(
        column("rf_freq1", "rf_freq1_KHz",
               {"section": "qcawifi", "section_type": "qcawifi", "key": "rf_freq1_KHz"}, type="int", min=0),
        column("rf_freq2", "rf_freq2_KHz",
               {"section": "qcawifi", "section_type": "qcawifi", "key": "rf_freq2_KHz"}, type="int", min=0),
        # AP: mld_ssid of config wifi-mld 'mld0'; CPEs: ssid of the first wifi-iface.
        column("ssid0", "ssid / mld0",
               {"section": "mld0", "section_type": "wifi-mld", "key": "mld_ssid"},
               {"section": "@wifi-iface[0]", "key": "ssid"}),
        column("ssid1", "mld1 ssid",
               {"section": "mld1", "section_type": "wifi-mld", "key": "mld_ssid"}, ap_only=True),
        column("wifi0_channel", "wifi0 channel",
               {"section": "wifi0", "section_type": "wifi-device", "key": "channel"}, **_CHANNEL),
        column("wifi0_mac", "wifi0 macaddr",
               {"section": "wifi0", "section_type": "wifi-device", "key": "macaddr"}, **_MAC),
        column("wifi1_channel", "wifi1 channel",
               {"section": "wifi1", "section_type": "wifi-device", "key": "channel"}, ap_only=True, **_CHANNEL),
        column("wifi1_mac", "wifi1 macaddr",
               {"section": "wifi1", "section_type": "wifi-device", "key": "macaddr"}, ap_only=True, **_MAC),
    ),
)

WPA_SUPPLICANT_TABLE = TableDef(
    id="wpa_supplicant_table",
    remote_path="/lib/wifi/wpa_supplicant.sh",
    format="wpa_networks",
    read_only=True,
    columns=tuple(
        column(f"scan_freq{i}", f"network {i + 1} scan_freq",
               {"section": f"network[{i}]", "key": "scan_freq"}, read_only=True, required=False)
        for i in range(2)
    ),
)

TABLES: tuple[TableDef, ...] = (WIRELESS_TABLE, WPA_SUPPLICANT_TABLE)
