# Log Collector

Windows GUI tool for collecting WLAN debug logs from AP / CPE devices over ADB:

- **CNSS logs** (`cnss_diag -c`), one independent collection per device
- **PKTLOG** captures (AP PHY0 / PHY1, CPE radio0), which are pulled to the PC automatically
- **PKTLOG translation** of many files at once, in parallel, with `pktlog_postproc.py` from the latest CRM build

Works with any setup size: 1 AP plus 1, 2, 4, 8, 16 ... CPEs.

Built with Python and PySide6. All external commands run hidden: no console windows appear.

## Requirements

- Windows 10/11
- Python 3.10 or newer
- `adb.exe` (Android SDK platform-tools) in `PATH`
- `pyserial` for serial logs (`Log Collector.bat` installs it automatically)
- Access to `\\bulk\si\...` (output) and `\\bulk\nsid-hfa-01\` (CRM builds)

## Installation and launch

Install PySide6 once per PC. Use `python -m pip` so it goes into the same Python that runs the app:

```bat
python -m pip install -r \\bulk\si\Users\rkozly\rozaline\tools\logCollector\requirements.txt
```

Start the app. This works from any folder, so no `cd` is needed:

```bat
rem normal start - errors are printed in the console
python \\bulk\si\Users\rkozly\rozaline\tools\logCollector\logCollector.py

rem start without a console window
pythonw \\bulk\si\Users\rkozly\rozaline\tools\logCollector\logCollector.py
```

If nothing appears, run it with `python` (not `pythonw`) to see the error. `No module named 'PySide6'` means the install step above is missing for that Python.

## Device setup

Device roles map to serial numbers through environment variables. Only the devices you configure are shown, so define one variable per device in your setup:

| Variable              | Role  |
|-----------------------|-------|
| `adb_device_id_ap`    | AP    |
| `adb_device_id_cpe1`  | CPE1  |
| `adb_device_id_cpe2`  | CPE2  |
| ...                   | ...   |
| `adb_device_id_cpe16` | CPE16 |

Set them with `setx adb_device_id_cpe1 <serial>` or through *System Properties → Environment Variables*. **Refresh** reads them from the registry, so you do not need to restart the app after changing them.

- **What gets rebuilt:** every Refresh rebuilds the device table, the CNSS check boxes and the CPE Collection check boxes from these variables. A 2-CPE setup shows only CPE1 and CPE2.
- **Removing a device:** delete its variable (`reg delete HKCU\Environment /v adb_device_id_cpe3 /f`) and press Refresh.
- **Order:** devices are sorted by number, so CPE2 comes before CPE10. With many devices the check boxes wrap onto several lines.
- **Running collections:** a device that is collecting stays in the list until it finishes, even if its variable is removed.
- **No variable:** a device that is connected but has no variable is listed as "(unassigned)".

## Layout

```
+-----------------------------------------------------------------------------+
| Connected Devices                                                [Refresh]  |
|  Role   | Serial              | Status                                      |
|  AP     | 1a2b3c4d            | Connected        (green)                    |
|  CPE1   | 5e6f7a8b            | Connected        (green)                    |
|  CPE2   | 9c0d1e2f            | Not connected    (red)                      |
+-----------------------------------------------------------------------------+
| CNSS Logging                                                                |
|  [x] AP  [x] CPE1  [ ] CPE2  [ ] Select All                                 |
|  AP    Output Path: [\\bulk\si\Users\me\ap_cnss.txt  ][Browse][Stop] Collecting...
|  CPE1  Output Path: [\\bulk\si\Users\me\cpe1_cnss.txt][Browse][Stop] Stopped
|                                               [Start Collection] [Stop All] |
+-----------------------------------------------------------------------------+
| PKTLOG Logging                                                              |
|  AP Collection                                                              |
|   [x] PHY0  [ ] PHY1  [ ] Select All                                        |
|   PHY0 Folder: [\\bulk\si\Users\me] File: [ap_pktlog_radio0.dat] [Browse] 21s left
|   Duration: [30 s]                                          [Start] [Stop]  |
|  CPE Collection                                                             |
|   [ ] CPE1  [x] CPE2  [ ] Select All                                        |
|   CPE2 Folder: [\\bulk\si\Users\me] File: [cpe2_pktlog_radio0.dat] [Browse] Completed
|   Duration: [30 s]                                          [Start] [Stop]  |
|  PKTLOG Translation                                                         |
|   CRM path: [\\bulk\nsid-hfa-01\WLAN.SDX_MRR.1.0-00374-...] [Browse] [Re-detect]
|   [Add Files...] [Output Folder...] [Remove] [Clear]   up to 8 in parallel  |
|   Input file (.dat)              | Output file (.txt)        | Status       |
|   [x] ...\ap_pktlog_radio0.dat   | ...\wifi0_pktlog_ap.txt   | Completed    |
|   [x] ...\cpe1_pktlog_radio0.dat | ...\wifi0_pktlog_cpe1.txt | Translating  |
|   [x] ...\cpe2_pktlog_radio0.dat | ...\wifi0_pktlog_cpe2.txt | Queued       |
|   [Translate] [Cancel]  Translating 2 file(s), 1 queued                     |
+-----------------------------------------------------------------------------+
| Translating 2 file(s), 1 queued                   Log: ...\logCollector.log |
+-----------------------------------------------------------------------------+
```

The window uses a light, off-white theme. Status colors: **blue** = collecting / waiting / stopping, **orange** = translating, **green** = completed / connected, **red** = error / not connected, gray = idle / stopped.

## Usage

### CNSS
1. Tick the devices. Each ticked device shows its output path field.
2. Click **Start Collection**. Each device runs
   `adb -s <serial> wait-for-device shell cnss_diag -c > <file>`.
3. Use the **Stop** button on a row to stop only that device. **Stop All** stops every device.

### CONGCTRL (congestion control)
Works exactly like CNSS: tick AP and/or CPEs, check the output paths, **Start Collection**, then **Stop** per row or **Stop All**.

Default output files: `\bulk\si\Users\rkozly\ap_congctrl.txt`, `cpe1_congctrl.txt`, `cpe2_congctrl.txt`, ... (editable per device).

For each device the tool runs:
1. `adb -s <serial> root`
2. enable CONGCTRL logging:
   - AP: `wifitool mld0 link_id 0 setUnitTestCmd 0x48 3 537 1 1` and `wifitool mld1 link_id 1 setUnitTestCmd 0x48 3 537 1 1`
   - CPE: `wifitool ath0 link_id 0 setUnitTestCmd 0x48 3 537 1 1`
3. `adb -s <serial> shell "cnss_diag -c | grep CONGCTRL" > <file>` until stopped

### Serial (dmesg / console)
Saves each device's serial console to a file. This is the same output MobaXterm shows: kernel (dmesg) messages, boot and crash output.

1. **Close the device's session in MobaXterm.** Windows lets only one program open a COM port at a time. If the session is still open, the row shows *COMxx is in use by another program*.
2. Pick the baud rate (default 115200). Optionally tick **Add PC timestamps** to prefix every line with the PC time.
3. Tick the devices, check the COM port and output path, and click **Start Collection**.
4. **Stop** on a row, or **Stop All**, closes the port. The file keeps everything received. New data is appended, so an existing file is not overwritten.

Default output files: `\\bulk\si\Users\rkozly\ap_serial.txt`, `cpe1_serial.txt`, ... (editable per device).

**COM port per device.** Ports are read from the MobaXterm sessions named `<ROLE> - COM<N>` (e.g. `CPE2 - COM12`) in `MobaXterm.ini`. The tool looks for it in `%APPDATA%\MobaXterm\`, `Documents\MobaXterm\` and next to `MobaXterm.exe`. If yours is somewhere else (for example, a portable MobaXterm), set `mobaxterm_ini=<full path of MobaXterm.ini>`. A variable `serial_port_<role>=COM<N>` overrides a session, and you can also change the port in the row. **Reload Ports** reads everything again.

### WPA Supplicant (CPE) and Hostapd (AP)
WPA Supplicant Logging lists only the CPEs; Hostapd Logging lists only the AP.

1. **Tick a device.** The tool checks it right away:
   - the start-up files contain the debug changes, and
   - the running daemon (the global one, started with `-g`) was started with the debug flags, i.e. the device really uses the changed files.
2. If something is missing, one dialog lists every ticked device that needs changes and offers **Apply + Restart service** or **Apply + Reboot device**:
   - WPA, `/etc/init.d/qca-wpa-supplicant`: `-tddd -f /tmp/wpa_supplicant.log` is added to both `wpa_supplicant -g ... -P $WPAD_VARRUN/wpa_supplicant-global.pid` commands.
   - Hostapd, `/etc/init.d/qca-hostapd`: `-dddKt` is added after `/usr/sbin/hostapd`.
   - Hostapd, `/lib/wifi/hostapd.sh`: the `run_hostapd_cli ... "close_log"` line is commented out with `##`.

   The first original of each file is kept as `<file>.orig` on the device. *Restart service* runs `/etc/init.d/<service> restart` and then `wifi`. Afterwards the device is checked again (for up to 5 minutes), and the row shows **Verified**. If a firmware does not contain the expected lines, nothing is changed and the row says so.
3. **Start Collection** only runs on verified devices. The log is copied to the PC **live** while it is written (`tail -f` over adb), not only when you press Stop. If the device crashes or reboots, everything logged until then is already in the PC file. The tool waits for the device, adds a `device rebooted` marker and continues in the same file.
4. **Stop** on a row, or **Stop All**, ends the copy. The file changes stay on the device (also after a reboot), so the daemon keeps logging to `/tmp`; restore `<file>.orig` to undo them. Press **Check** to check a device again at any time.

Default output files: `\\bulk\si\Users\rkozly\wpa_supplicant_cpe1.log`, ... and `hostapd_ap.txt` (editable per device).

### PKTLOG
1. Tick PHY0 / PHY1 (AP) or any of the configured CPEs, set the duration, and check the destination folder and file name.
2. Click **Start**. For each capture the tool runs, in order:
   1. `adb root`
   2. `pktlogconf -r/-a`
   3. the three `wifitool ... setUnitTestCmd` commands
   4. a wait of *duration* seconds
   5. `pktlogconf -d`
   6. `cp /proc/ath_pktlog/wifiN /tmp/...`
   7. `adb pull` to the destination
3. **Stop** ends the wait early. pktlog is still disabled on the device, and no file is pulled.
4. If the capture finished but `cp` or `adb pull` failed (e.g. *Command timed out.*), a **Retry Copy** button appears on that row. It redoes only the copy and pull of the buffer that is already on the device, without a new capture. The `cp` timeout is 120 s.

The wait is timed on the PC instead of running `sleep` on the device, so Stop reacts immediately.

Each CPE gets its own default file (`cpe1_pktlog_radio0.dat`, ...), so captures from several CPEs at once do not overwrite each other.

### Translation
The translation group is part of the PKTLOG Logging block.

- **CRM path:** at startup the tool picks the highest `WLAN.SDX_MRR.1.0-<n>-QCAHKSWPL_SILICONZ-1` build in `\\bulk\nsid-hfa-01\`. You can override it by typing or browsing.
- **Adding files:** **Add Files...** lets you select any number of `.dat` files at once (Ctrl/Shift-click in the dialog). Every finished PKTLOG capture is also added automatically.
- **Output names:** each file gets an output name next to it:
  - `ap_pktlog_radio0.dat` → `wifi0_pktlog_ap.txt`
  - `ap_pktlog_radio1.dat` → `wifi1_pktlog_ap.txt`
  - `cpeN_pktlog_radio0.dat` → `wifi0_pktlog_cpeN.txt`
  - anything else → `<name>.txt`

  Double-click an output cell to change it. **Output Folder...** moves the outputs of the selected rows (or all rows) to one folder.
- **Translate:** runs every checked file from the translator folder:
  `python pktlog_postproc.py -d pktlog_defs_wkk -t "<in>" -w text > "<out>"`

  Up to 8 translations run in parallel (`TRANSLATE_MAX_PARALLEL` in `config.py`). The rest wait as "Queued" and start as soon as a slot is free.
- **Status and Cancel:** every row shows its own status; hover over a failed status to see the translator's error. **Cancel** drops the queued files and stops the running ones.
- **Remembered:** the file list is kept between sessions.

## Logging

`logCollector.log` is written next to `logCollector.py`. It rotates at 5 MB and keeps 3 backups. Each line has a timestamp, level, device, command, OK/FAIL with exit code, and any error output:

```
2026-10-07 09:47:48,008 | INFO    | AP PHY0    | OK   rc=0 | adb -s 1a2b wait-for-device shell pktlogconf -d wifi0
2026-10-07 09:48:02,113 | ERROR   | CPE1       | FAIL rc=1 | adb -s 5e6f pull ... | adb: error: device '5e6f' not found
```

## Architecture

```
logCollector/
  logCollector.py           entry point: logging, job object, theme, main window
  logcollector/
    config.py               roles, default paths, device command templates, CRM pattern
    app_logging.py          log file setup + log_command()
    core/                   no Qt here - plain Python, unit-testable
      process.py            hidden subprocesses, process registry, Windows Job Object
      adb.py                find adb, parse `adb devices`, env/registry role mapping
      crm.py                latest CRM detection
      serial_ports.py       COM port per role (MobaXterm sessions, serial_port_<role>)
      daemon_config.py      wpa_supplicant / hostapd file edits and process flag checks
      paths.py              output/input path validation (network share, permissions)
    tasks/                  background jobs (QThread subclasses)
      base_task.py          BaseTask: stop handling, run_cmd(), adb(), signals
      device_scan_task.py   adb devices
      cnss_task.py          cnss_diag streaming
      serial_task.py        COM port -> file streaming
      daemon_log_task.py    wpa / hostapd: check + fix, live log streaming across reboots
      pktlog_task.py        pktlog configure / wait / copy / pull
      translate_task.py     one pktlog_postproc.py run
      translate_queue.py    runs many TranslateTasks in parallel (max N at once)
      crm_scan_task.py      CRM share listing
    ui/                     widgets only, no subprocess calls
      theme.py              off-white Fusion palette, status colors
      widgets.py            StatusLabel, PathEdit, FolderFileEdit,
                            CheckGrid / CheckedRows (dynamic device check boxes)
      devices_panel.py      device table, emits roles_changed after each scan
      cnss_panel.py         CNSS section
      serial_panel.py       Serial (dmesg / console) section
      daemon_panel.py       WPA Supplicant (CPE) and Hostapd (AP) sections
      pktlog_panel.py       PKTLOG block: AP group, CPE group, translation group
      translate_panel.py    translation group (multi-file list)
      main_window.py        layout, status bar, settings, shutdown
  tests/test_parsers.py     parser / command template tests
```

**Threads.** The GUI thread never runs a subprocess or touches a network path. Each job is a `BaseTask` (a `QThread`). It reports progress with `status(state, message)` and its result with `done(state, message)`. Qt delivers these signals to the GUI thread.

**Subprocesses.** `BaseTask.run_cmd()` starts commands through `ManagedProcess`:
- They start with `CREATE_NO_WINDOW` and `SW_HIDE`, and never use `shell=True`.
- `>` redirection is done by passing an open file as stdout.
- The command is polled every 250 ms, so Stop or a timeout can kill it at once.

**No orphaned processes.** Three layers:
1. On close, every task is asked to stop.
2. `process.kill_all()` terminates every tracked child.
3. At startup the app puts itself in a Windows **Job Object** with `KILL_ON_JOB_CLOSE`. If the app crashes or is killed from Task Manager, Windows kills every adb and translator child process.

The shared adb server (`adb fork-server`) is not stopped, because other tools may use it.

**Dynamic devices.** After each scan `DevicesPanel` emits `roles_changed` with the configured roles. The CNSS panel and the CPE group pass them to `CheckedRows`, which adds or removes check boxes and detail rows. Each new row reads its saved path from settings when it is created. Nothing in the code assumes a fixed number of CPEs.

**Parallel translation.** `TranslateQueue` keeps up to `TRANSLATE_MAX_PARALLEL` `TranslateTask` threads running. Each thread runs its own hidden `pktlog_postproc.py` process. When one finishes, the next queued file starts.

**Settings.** Paths, check boxes, durations, the translation file list and window size are saved per device with `QSettings` (registry `HKCU\Software\logCollector`). Saved paths come back when the device appears again.

### Adding a new collection type

1. Add any command templates and defaults to `config.py`.
2. Create `tasks/my_task.py` with a `BaseTask` subclass. Implement `execute()` using `self.adb(serial, ...)` / `self.run_cmd(...)`, `self.check_stop()` and `self.sleep_interruptible()`. Return `(State.COMPLETED, "message")` or raise `TaskError("friendly message")`.
3. Create a panel in `ui/` (copy `cnss_panel.py` as a template). Emit `status_message` and add the panel to `MainWindow`.

## Error handling

| Problem                 | What you see |
|-------------------------|--------------|
| adb not installed       | Dialog at startup, red status. Translation still works |
| Device not connected    | Red row in the table, "Device not connected" on start |
| Device lost mid-run     | "Device disconnected or not found." |
| Network drive down      | "Network drive is not available: \\\\server\\share" |
| Invalid path / no access| "Invalid output folder" / "Permission denied writing to ..." |
| Translator failure      | "Translation failed (exit code N)" + last error line |
| Unexpected crash        | Task ends in Error state; traceback goes to the log file |

Hover over a status text to see the full message.

## Tests

```bat
python -m pip install pytest
python -m pytest tests
```
