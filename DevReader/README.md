# DevReader

DevReader is an internal engineering desktop tool (Python + PySide6). It can:

* **discover** ADB devices mapped by `adb_device_id_*` environment variables,
* **prepare** them for configuration changes (root / disable-verity / reboot / remount / …),
* **read and write** configuration values in device config files. v1 manages `/etc/config/wireless`.

All long-running work happens on background threads, so the UI never freezes. Every adb command is logged along with its output.

---

## Quick start

```bat
cd \\bulk\si\Users\rkozly\git\rozaline\DevReader
DevReader.bat
```

You can also double-click `DevReader.bat` in Explorer, including on the network share. On first run it creates a Python environment in `%LOCALAPPDATA%\DevReader\venv` on the local disk and installs the requirements, then starts the app. If an earlier run was interrupted and left the environment without pip, the launcher repairs or rebuilds it. When started from a share, cmd first prints a "UNC paths are not supported" notice; you can ignore it.

Manual alternative:

```bat
py -3 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m devreader
```

**Try it without hardware:**

```bat
.venv\Scripts\python -m devreader --simulate
```

This uses a built-in fake adb with simulated AP/CPE devices (connected, unauthorized, disconnected and unmapped).

Command-line options:

| Option | Meaning |
|---|---|
| `--adb PATH` | Use a specific adb executable |
| `--simulate` | Use simulated devices (no adb/hardware needed) |
| `--version` | Print version |

### Requirements

* Windows 10/11 (Linux/macOS also work)
* Python 3.10+
* Android platform-tools (`adb`). DevReader looks for adb in this order:
  1. `--adb`
  2. *Settings → Set adb path…*
  3. `ADB_PATH`
  4. `PATH`
  5. `%ANDROID_SDK_ROOT%` / `%ANDROID_HOME%`
  6. `%LOCALAPPDATA%\Android\Sdk`
  7. `C:\platform-tools`

---

## Device discovery

Define one environment variable per device:

```
adb_device_id_ap   = 1a2b3c4d
adb_device_id_cpe1 = 5e6f7a8b
adb_device_id_cpe2 = 9c0d1e2f
```

The suffix becomes the friendly name (`ap` → `AP`, `cpe1` → `CPE1`). Discovery runs `adb devices` and shows each device with one of these statuses:

| Status | Meaning |
|---|---|
| ✅ Connected | adb state `device` |
| ❌ Disconnected | not listed by adb |
| ⚠ Unauthorized | accept the USB-debugging prompt on the device |
| ⚠ Offline | reconnect the device or restart the adb server |

Devices that adb can see but no variable maps are listed as **(unmapped)**.

DevReader also reads the persisted user and system environment from the registry. Variables you add in *System Properties* are therefore picked up by **Refresh** without restarting the app.

## Device preparation

**Prepare Device** runs this sequence on every ticked device. Devices run in parallel, and each device's steps run in order:

```
adb -s <serial> wait-for-device root
adb -s <serial> wait-for-device disable-verity
adb -s <serial> wait-for-device reboot
adb -s <serial> wait-for-device root
adb -s <serial> wait-for-device remount
adb -s <serial> wait-for-device shell "setenforce 0"
adb -s <serial> wait-for-device shell "mount -o rw,remount /"
```

How it behaves:

* **Progress:** each device row has its own progress bar ("4/7 Root (after reboot)"). All command output goes to the log.
* **Fail fast:** a device that is unauthorized, offline or disconnected fails immediately. Without this check, `wait-for-device` would block until it timed out.
* **Reboot handling:** after `reboot`, DevReader waits for the device to drop off the bus before the next `wait-for-device`. This stops the next step from talking to a device that is still shutting down.
* **Error detection:** a step can fail even when adb exits with code 0, because adb sometimes prints the error and still returns 0. DevReader treats messages like `cannot run as root` or `remount failed` as failures.
* **Fatal vs. non-fatal steps:** if `disable-verity` or `setenforce 0` fails, DevReader logs a warning and continues. If root or remount fails, it aborts that device.
* **Multiple devices:** one device failing never stops the others.
* **Timeouts and Cancel:** every step has a timeout (180 s; 300 s for the root after reboot). **Cancel** (or `Esc`) stops all running operations.

## Configuration read / write

1. Choose the device in the configuration panel and click **Read Configuration**. DevReader reads the file byte-exact with `adb exec-out cat` and falls back to `adb pull` if that fails. It then fills the fields.
2. Edit the values. Modified fields get an amber border and invalid fields a red one; hints show the value from the device.
3. Click **Write Configuration**. For each target device DevReader:
   1. re-reads the current file from the device;
   2. replaces **only** the value text of the targeted options. Comments, blank lines, indentation, quoting style, line endings (LF/CRLF), the trailing newline and non-UTF-8 bytes are all left exactly as they were;
   3. runs an internal safety check that no other line changed;
   4. saves a backup of the original to `%LOCALAPPDATA%\DevReader\backups\<serial>\`;
   5. pushes the new file to a staging path (`/tmp`, falling back to `/data/local/tmp`) and copies it over the target with `cat > target`. This keeps the file's owner, permissions and SELinux label;
   6. reads the file back and checks it is **byte-identical** to what was pushed;
   7. re-parses the file and checks every written value.

Tick **Write to all selected devices** to apply the same values to every checked device.

DevReader refuses to *add* an option that does not already exist in the file. This protects against writing to the wrong section by mistake. DevReader also never restarts services by itself; see `post_write_commands` below.

## Logging

The log panel shows the timestamp, level, device, message, command and result/error for every operation.

* **Filter:** by level or by text. Choose *Debug+* to also see polling commands such as `adb devices`.
* **Copy:** use the button, `Ctrl+C` or the context menu to copy selected entries.
* **Export:** `.log`/`.txt` for plain text, or `.csv`.
* **Clear:** empties the panel.

Every entry is also written to `%LOCALAPPDATA%\DevReader\logs\devreader_YYYYMMDD.log` (rotating). *File → Open log folder* opens that location.

---

## Extending DevReader

### Add a parameter (no UI changes needed)

Edit [devreader/config/definitions.py](devreader/config/definitions.py):

```python
WIRELESS_PARAMETERS = [
    {"section": "qcawifi", "key": "rf_freq1_KHz", "label": "RF Frequency 1 (KHz)", "type": "int", "min": 0},
    {"section": "qcawifi", "key": "rf_freq2_KHz", "label": "RF Frequency 2 (KHz)", "type": "int", "min": 0},
    # new:
    {"section": "qcawifi", "key": "rf_bw", "label": "RF Bandwidth", "type": "choice",
     "choices": ["20", "40", "80", "160"], "group": "RF"},
    {"section": "qcawifi", "key": "rf_tx_gain", "label": "RF TX Gain (dB)", "type": "int",
     "min": 0, "max": 63, "group": "Gain"},
    {"section": "wifi0", "section_type": "wifi-device", "key": "country",
     "label": "Country Code", "pattern": "[A-Z]{2}", "group": "Regulatory"},
]
```

The editor builds its controls from these definitions. Supported fields:

| Field | Meaning |
|---|---|
| `section` | Section name (`config <type> '<name>'`), a type, or `@type[index]` (e.g. `@wifi-iface[0]`) |
| `section_type` | Restrict matching to sections of this type |
| `key`, `label` | Option name and UI label |
| `type` | `int`, `float`, `string`, `choice`, `bool` |
| `min` / `max` | Numeric range |
| `choices` | For `choice` |
| `pattern` | Regex the value must fully match |
| `group` | Groups fields under a heading |
| `unit`, `description`, `placeholder` | UI hints |
| `read_only` | Show the value but don't write it |
| `required` | If `False`, a missing option is not reported |

### Add a configuration file

```python
NETWORK = ConfigFileDef(
    id="network",
    title="Network Configuration",
    remote_path="/etc/config/network",
    parameters=params_from_dicts(NETWORK_PARAMETERS),
    post_write_commands=(),          # e.g. ("/etc/init.d/network reload",)
)
CONFIG_FILES = (WIRELESS, NETWORK)
```

Each file automatically gets its own tab. Any OpenWrt-style UCI file works as is. For other formats, add a document class with the same `get`/`set`/`to_bytes` interface as `UciDocument`.

### Add a preparation step or workflow

Preparation is plain data (`PREPARE_STEPS` in [devreader/core/preparation.py](devreader/core/preparation.py)). To add a step, append a `PrepStep`. To build another workflow, pass a different list to `PreparationService(adb, log, steps=...)`.

### Add new device actions (diagnostics, log collection, …)

1. Add a service in `devreader/core/` that takes `AdbService` and `LogService`. Use only `AdbService.run/shell/read_file/write_file/pull/push`.
2. Run it from the GUI with `self.runner.submit(fn, on_result=..., on_error=..., on_progress=...)` in `MainWindow`. This keeps the UI responsive and handles cancellation (`ctx.cancel`).
3. Add a panel or tab under `devreader/gui/`.

---

## Architecture

```
DevReader/
├── devreader/
│   ├── app.py                 # bootstrap: CLI args, services, theme, main window
│   ├── core/                  # UI-independent (no Qt imports)
│   │   ├── adb_service.py     # the ONLY place adb is executed; timeouts, cancel, error classification
│   │   ├── discovery.py       # adb_device_id_* env/registry → Device list + adb state
│   │   ├── preparation.py     # data-driven prepare workflow + readiness checks
│   │   ├── logging_service.py # thread-safe log hub → GUI listeners + rotating file
│   │   ├── errors.py          # typed exception hierarchy
│   │   ├── simulator.py       # fake adb backend (--simulate, tests)
│   │   └── paths.py           # %LOCALAPPDATA%\DevReader\{logs,backups}
│   ├── config/
│   │   ├── parameters.py      # ParameterDef / ConfigFileDef + validation
│   │   ├── definitions.py     # ← add files & parameters here
│   │   ├── uci.py             # formatting-preserving UCI parser/editor
│   │   └── service.py         # read / patch / backup / push / verify pipeline
│   └── gui/
│       ├── main_window.py     # orchestration, menus, confirmations, notifications
│       ├── device_panel.py    # device table, filter, inline progress
│       ├── config_panel.py    # dynamic form generated from definitions
│       ├── log_panel.py       # log table, filter, copy/export
│       ├── workers.py         # QThreadPool task runner with signals & cancellation
│       ├── widgets.py         # Card, Badge, Toast notifications
│       └── theme.py           # dark theme
├── tests/                     # pytest suite (parser, discovery, services via simulator)
├── DevReader.bat              # one-click launcher
├── build.bat                  # one-click exe build
├── devreader.spec             # PyInstaller spec
├── pyproject.toml / requirements*.txt
```

Design principles:

* **Layers:** `core` and `config` never import Qt. They can be reused from scripts, CLI tools or CI.
* **One ADB layer:** `AdbService` is the single choke point. Logging, timeouts and error mapping are therefore consistent everywhere. The simulator overrides only the process call, so the rest of the code path is the same as with real devices.
* **Threading:** each operation runs on a pool thread. Results go back to the UI thread through Qt signals.

### Error handling

| Situation | Handling |
|---|---|
| adb not installed | Toast + log; header shows "adb not found"; *Settings → Set adb path…* |
| No `adb_device_id_*` variables | Warning in the device panel with an example |
| Disconnected / offline / unauthorized | Status icon; operations fail fast with a specific message |
| Timeout waiting for device | `TimeoutError_`; the adb process is killed; other devices continue |
| Root / remount failure | Detected from the exit code and from the output text; aborts that device |
| Read failure | `ConfigReadError` (missing file, permission, empty file) |
| Parse failure | `ConfigParseError` (no sections, or none of the managed options found) |
| Push failure | `ConfigPushError`; tries both staging dirs; original file untouched |
| Verification failure | `ConfigVerificationError` with checksums; the local backup path is logged |

---

## Development

```bat
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q
```

## Packaging

The easiest way is one click:

```bat
build.bat
```

`build.bat` creates the venv, runs the tests and builds `dist\DevReader\DevReader.exe` with PyInstaller.

Manual build:

```bat
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\pyinstaller devreader.spec --noconfirm --clean
```

To distribute, copy the whole `dist\DevReader\` folder (for example zip it or put it on a share). Python is not needed on the target machine, but adb is. You can also place an icon at `devreader/resources/devreader.ico` and it will be used automatically.

To install as a Python package instead (adds a `devreader` command):

```bat
pip install .
devreader --simulate
```
