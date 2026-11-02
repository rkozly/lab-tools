# LabHub – Design (Phase 1 + Phase 2)

LabHub is a desktop app for managing and monitoring the APs, CPEs and
backbones (BBs) in a wireless test lab. It replaces the work now done in
many terminals: connecting to devices, sending commands, watching stats,
running scans.

- **Phase 1:** device inventory. adb discovery, device mappings, device/backbone tiles.
- **Phase 2:** daily operation.
  - command execution on many devices
  - preset library
  - QCMAP menu sequences
  - live stats dashboards
  - beacon visualization
- **Later phases** are added as plugins (section 8).

---

## 1. Architecture

```
┌──────────────────────────────── GUI (PySide6) ───────────────────────────┐
│ Device strip · Command bar · Workspace tabs (Console│Monitor│Beacons│…)   │
└──────▲─────────────────────────────────────────────────────────┬─────────┘
       │ Qt bridge: core events -> Qt signals (GUI thread)        │ actions
┌──────┴─────────────────────────────────────────────────────────▼─────────┐
│ CORE (plain Python, no Qt)                                               │
│  Event bus · Lab config · Registry · Selection · Target resolver         │
│  Job runner (stream / stop) · Session manager (expect + delays)          │
│  Preset library · Parser registry · Pollers                              │
│  Stores: output buffers · metric samples · beacon observations · runs    │
│  Plugin host                                                             │
├──────────────────────────────────────────────────────────────────────────┤
│ TRANSPORTS   adb · ssh · local                                           │
│   exec(cmd)  -> one command, streamed output, exit code, stoppable       │
│   session()  -> long-lived interactive shell (send / expect)             │
└──────────────────────────────────────────────────────────────────────────┘
 PLUGINS  Phase 2: commands · presets · sequences · monitor · beacons
          Later:   association · ping · traffic · logs · firmware ·
                   topology · collaboration
```

Rules:

1. **The core has no GUI code.** It can be tested without a GUI and is also used by a CLI (`labhub run role:cpe "iw ath0 link"`).
2. **Configuration and runtime state are separate.** Configuration is what the lab should look like. Runtime state is what is connected now. The registry joins them into one `NodeView` per tile.
3. **Transports hide how a node is reached.** Plugins call `ctx.runner.run(targets, cmd)` and never call adb or SSH directly.
4. **Everything is an event.** Examples: `NodeStatusChanged`, `JobOutput`, `JobFinished`, `SampleAdded`, `BeaconSeen`. Views subscribe to events, and so do other plugins.
5. **Actions are data,** e.g. `{preset: cpe-short-scan, params: {channel: B}, targets: [role:cpe]}`. They can be logged, repeated, and later chained into automation scripts.

## 2. UI

### Main window

```
┌ LabHub ─ Lab: [Rozaline lab 2 ▾]  ⟳  ✎ Mappings  ⚙ ───────────── ● adb OK ┐
│ [●AP│●AP BB] [●CPE1│●CPE1 BB] [●CPE2│○CPE2 BB] [◐CPE3│●CPE3 BB]  +2 unassigned│
├────────────────────────────────────────────────────────────────────────────┤
│ Target: [Selected ▾]  ✕CPE1 ✕CPE2 ✕CPE3      Quick: APs · CPEs · BBs · Group▾ │
│ > iw ath0 link_______________________________________  [▶ Run] [■ Stop all]  │
├──────────────┬─────────────────────────────────────────────────────────────┤
│ PRESETS  🔍  │ [Console] [Monitor] [Beacons] [Overview]                    │
│ ▸ Scan       │ ┌ CPE1 ● running 00:12 ──[■][⌫][⇩]┐┌ CPE2 ✔ exit 0 ─[⌫][⇩]┐  │
│   AP Short   │ │ Connected to 00:03:7f:…         ││ Connected to 00:03:… │  │
│   CPE Short  │ │ freq: 27000                     ││ freq: 27000          │  │
│    [A][B][C] │ │ signal: -48 dBm                 ││ signal: -51 dBm      │  │
│ ▸ QCMAP      │ └─────────────────────────────────┘└──────────────────────┘  │
│   AP Bridge  │ ┌ CPE3 ✖ unauthorized ────────────┐                          │
│   CPE Bridge │ │ adb: device unauthorized        │                          │
│ ▸ Stats      │ └─────────────────────────────────┘                          │
│ + New preset │                                                             │
├──────────────┴─────────────────────────────────────────────────────────────┤
│ 12:01:03 CPE Short Scan [B] → CPE1,CPE2 · 12:00:41 CPE3 unauthorized       │
└────────────────────────────────────────────────────────────────────────────┘
```

- **Device strip (top).**
  - Each device and its backbone form a pair of tiles: [CPE1 | CPE1 BB]. Each half can be selected on its own.
  - Status dot: ● connected, ○ not found, ◐ unauthorized/offline.
  - Plugins add small badges to tiles, e.g. associated, RSSI, running job.
  - Click selects one tile, Ctrl-click toggles, Shift-click selects a range, drag selects a block. The quick links (APs / CPEs / BBs / Group) replace the selection.
- **Command bar.**
  - The target chips show exactly who receives the next command.
  - Command history is available with ↑/↓.
  - Enter runs. Ctrl+Enter runs as a multi-line script.
- **Preset panel (left).**
  - Presets are grouped by category and filtered by the selected roles. Clicking a CPE preset while only APs are selected warns first.
  - Presets with choices show them as buttons, e.g. [A][B][C] for channels.
  - Right-click a preset to edit, duplicate or delete it.
- **Console tab.**
  - One output pane per target, in a grid, with live streaming.
  - Each pane has ■ stop, ⌫ clear, ⇩ export, plus search, pin (keep across runs) and maximize.
  - ANSI colours are stripped. Each buffer has a size cap.
- **Status bar / event log.** Every action, with time and targets.

### Monitor tab

```
┌ CPE1 ● associated → AP ───────────────┐ ┌ AP ─ 3 stations ────────────────┐
│ RSSI  -48 dBm  ▁▂▃▅▆▆▇▇▆▇  ████▏good  │ │ wifi0  TX MCS 11   BW 400 MHz   │
│ TX MCS 9    RX MCS 8    BW 400 MHz    │ │ wifi1  TX MCS 9    BW 400 MHz   │
│ EVM  pilot0 -31.2 dB  pilot1 -30.8 dB │ │ RX RSSI -45 / -47  EVM -30 / -29│
│ Link freq 27000 · BSSID 00:03:7f:…    │ │ mld0: CPE1 CPE2  │  mld1: CPE3   │
│ ath0 RX 1.2 GB  TX 0.8 GB             │ │                                 │
└───────────────────────────────────────┘ └─────────────────────────────────┘
 [▶ Start CPE stats on selection] [▶ Start AP stats] [■ Stop]   interval [1 s]
 Click a card: history charts (RSSI, MCS, EVM over time) + raw output
```

- Signal quality is rated from RSSI and EVM. The good/fair/poor thresholds are set in the lab file.
- Values show the trend over the last N samples.
- Sample history is kept so a chart and a CSV export can be made from it.

### Beacons tab

```
 Observers [All CPEs ▾]  Channel [All ▾]  ☑ lab devices only  ⏵ auto-scan 10 s
 ┌ Name   Type  Ch  Freq   RSSI  Assoc   Seen by     Last seen ┐   RSSI
 │ AP     AP    B   27000  -46   ● 3     CPE1,2,3    12:01:02  │   -40 ┤   ● AP
 │ ?00:…  ?     C   27340  -71   –       CPE2        12:00:55  │   -70 ┤        ○ ?
 └──────────────────────────────────────────────────────────────┘       └─A───B───C─▶
```

- Rows can be grouped by channel or by observer.
- Unknown BSSIDs are shown and can be assigned to a lab device in one click.
- Later views can show the same data differently: topology graph, channel occupancy, RF heatmap.

## 3. Workflow

1. Choose a lab. The device strip fills in automatically from `adb devices` and the lab file.
2. Select targets: tiles, quick links, or a group.
3. Run a typed command or a preset. Each target gets its own output pane.
4. To work with the output:
   - Stop one device or all.
   - Clear panes.
   - Export: writes `exports/<time>_<name>/<device>.log` plus `run.json`. `run.json` records the command, targets, parameters, start/end time and exit code per device.
5. Start monitoring with the stats presets. Cards update every second. Raw output stays available on each card.
6. Beacons collects scan results continuously while auto-scan is on, or on demand.
7. Every action is recorded as data (section 1, rule 5). Later phases use these records to chain actions into scripts.

## 4. Data model

### Configuration

```python
ConnectSpec(type: "adb"|"ssh"|"local", params: dict)
BackboneLink(backbone_id, interfaces: list[str])          # CPE1.960, CPE1.1015
DeviceConfig(id, name, role: "ap"|"cpe"|"other", serial,  # serial may be ${env:...}
             backbone: BackboneLink|None, tags, macs, extra)
BackboneConfig(id, name, connect: ConnectSpec, extra)
Lab(name, version, devices, backbones, groups: dict[str, list[Target]], features: dict)
```

### Runtime

```python
Status = connected | disconnected | unauthorized | offline | unknown
NodeState(status, since, info, error)
NodeView(id, kind: device|backbone|unassigned, name, state, config, linked)
```

### Targets

```text
cpe1              one node by id
role:cpe          all CPEs        role:ap       all APs
kind:backbone     all backbones   group:<name>  user-defined group
tag:<tag>         nodes with a tag
all               every configured node
selected          the current tile selection
```

The resolver returns nodes in display order, without duplicates. Nodes that are not connected are not run; they are reported as skipped.

### Execution

```python
Run(id, label, command|preset_id, params, targets, started, jobs)
Job(id, run_id, node_id, status: queued|running|done|failed|stopped|skipped,
    exit_code, started, ended, output: OutputBuffer)
OutputBuffer: capped buffer of lines (default 20,000 lines per job)
```

### Presets

```python
Preset(id, name, category, roles, kind: exec|sequence|monitor,
       script, params: dict[str, Param], sequence: SequenceSpec|None,
       monitor: MonitorSpec|None, source: builtin|lab|user)
Param(choices: dict[label, value] | None, default)
SequenceSpec(start, steps, prompt, delay_ms, step_timeout_s, exit)
MonitorSpec(command, interval_s, parser)
```

Placeholders in scripts: `{param}`, `{name}` (CPE1), `{n}` (1), `{id}`, `{serial}`.

### Monitoring and scan

```python
Sample(node_id, time, metrics: dict)   # rssi_dbm, tx_mcs, rx_mcs, rx_bw_mhz,
                                       # evm_pilot0_db, evm_pilot1_db, ...
Association(node_id, peer_mac, peer_node_id, freq_mhz, signal_dbm, state, time)
BeaconObservation(observer_id, time, bssid, ssid, channel, freq_mhz, rssi_dbm,
                  matched_node_id, device_type, assoc_state)
```

Matching a BSSID to a device uses `macs:` in the lab file, or MACs learned from `ifconfig ath0`.

## 5. Files

| File | Location | Content |
|---|---|---|
| Lab file | `labs/<lab>.yaml` on the share | devices, backbones, groups, feature settings |
| Preset files | built-in, `presets:` in the lab file, user file | preset library |
| User file | `%APPDATA%\LabHub\user.yaml` | selected lab, personal presets, UI preferences |
| Exports | `%USERPROFILE%\LabHub\exports` (configurable) | run logs, CSVs |

Presets with the same id override each other: user beats lab, lab beats built-in. Editing a built-in preset in the GUI saves a copy to the user file.

### Lab file example

```yaml
version: 1
lab: Rozaline lab 2

backbones:
  ap-bb:  { name: AP BB,  connect: { type: ssh, host: 10.0.0.20, user: root } }
  cpe-bb: { name: CPE BB, connect: { type: ssh, host: 10.0.0.21, user: root } }

devices:
  ap:
    name: AP
    role: ap
    serial: ${env:adb_device_id_ap}
    backbone: { id: ap-bb, interfaces: [AP.960, v10371015] }
  cpe1:
    name: CPE1
    role: cpe
    serial: ${env:adb_device_id_cpe1}
    backbone: { id: cpe-bb, interfaces: [CPE1.960, CPE1.1015] }

groups:
  first-four: [cpe1, cpe2, cpe3, cpe4]

features:
  monitor:  { interval_s: 1, rssi_good_dbm: -55, rssi_poor_dbm: -70 }
  beacons:  { interval_s: 10 }
```

### Preset examples

```yaml
presets:
  - id: ap-short-scan
    name: AP Short Scan
    category: Scan
    roles: [ap]
    script: cfg80211tool wifi0 hw_ver_override 2

  - id: cpe-short-scan
    name: CPE Short Scan
    category: Scan
    roles: [cpe]
    params:
      channel: { choices: { A: 26660, B: 27000, C: 27340 } }
    script: |
      cfg80211tool wifi0 freq_scan_num_rf_channels 1
      cfg80211tool wifi0 freq_scan_rf_channel_0 {channel}
      cfg80211tool wifi0 freq_scan_rf_channel_mask_0 256
      cfg80211tool wifi0 hw_ver_override 2

  - id: ap-bridge-mode
    name: AP Bridge Mode
    category: QCMAP
    roles: [ap]
    kind: sequence
    sequence:
      start: QCMAP_CLI
      steps: [1, 2, 1, 4, 29, 1, 4, 1, 1]
      prompt: '(?i)(option|choice|select|enter).*[:>]\s*$'   # to be confirmed
      delay_ms: 500
      step_timeout_s: 10

  - id: cpe-bridge-mode
    name: CPE Bridge Mode
    category: QCMAP
    roles: [cpe]
    kind: sequence
    sequence:
      start: QCMAP_CLI
      steps: [1, 2, 1, 4, 33, 1, 4, 1, 1]
      prompt: '(?i)(option|choice|select|enter).*[:>]\s*$'
      delay_ms: 500
      step_timeout_s: 10

  - id: cpe-stats
    name: CPE Stats
    category: Stats
    roles: [cpe]
    kind: monitor
    monitor:
      interval_s: 1
      parser: wifistats.cpe
      command: >-
        wifistats wifi0 9 | grep -E '^tx_mcs'; echo '';
        wifistats wifi0 10 | grep -E '^rx_bw|^rx_mcs|^rx_legacy_ofdm_rate|^rssi_in_dbm|pilot_evm_dB\[0\]|pilot_evm_dB\[1\]|la';
        echo ''; ifconfig ath0; echo ''; iw ath0 link
  # ap-stats: same idea with the AP command, parser wifistats.ap
  # The original "watch -n 1 ..." commands are kept as console presets too.
```

## 6. Phase 2 modules in detail

### 6.1 Commands (job runner)

- For each target, the job runner starts one worker that runs `transport.exec(node, cmd)`.
  - adb runs `adb -s <serial> shell <cmd>`. ssh and local use their own client.
- Output is read line by line, published as `JobOutput` events, and added to the job's buffer.
- **Stop:** ends the process tree. For adb it also sends Ctrl+C first, so `watch` and similar commands exit cleanly on the device.
- **Concurrency limit:** 16 jobs by default.
- Multi-line scripts run as one shell script per device, so `cd` and variables carry over between lines.

### 6.2 Sequences (QCMAP)

A sequence runs in an interactive session (`adb -s <serial> shell`, with `-tt` if the menu only shows on a real terminal):

```
send(start)                         # QCMAP_CLI
for step in steps:
    wait until output matches prompt (or step_timeout_s → fail)
    sleep(delay_ms)
    send(step + "\n")
wait for final prompt → send(exit) → close
```

- The output pane shows `[step 3/9] sent "1"` between menu screens.
- One device failing does not stop the others.
- The delay can be changed per run from the preset button's menu.

### 6.3 Monitor

- Every interval, a poller per node runs the preset's snapshot command, without `watch`.
  - `watch` needs a real terminal and redraws the screen, so its output cannot be parsed reliably.
- A registered parser turns the text into a `Sample` (and an `Association` for CPEs).
- Parsers are plain functions, e.g. `wifistats.cpe`, `wifistats.ap`, `iw.link`, `ifconfig`. Each has unit tests against captured real output, so a firmware change in the output format causes a failing test, not a silently blank widget.
- Samples are kept in a fixed-size history per node; 1 h at 1 s by default.

### 6.4 Beacons

- A poller per observer starts a scan when auto-scan is on, then runs the scan-results command and parses it into `BeaconObservation`s.
- Observations go into a store. Entries older than N scans are marked "stale" instead of being removed.
- Association state comes from the Monitor/Association data.
- The table and the RSSI/channel chart are two views of the same store. Topology, occupancy and heatmap views are added the same way later.

## 7. Plugin interface

```python
class Plugin:
    id: str
    title: str
    requires: list[str] = []          # other plugin ids
    def activate(self, ctx: AppContext) -> None: ...
    def deactivate(self) -> None: ...
```

`AppContext` gives a plugin:

| Member | Use |
|---|---|
| `ctx.bus` | subscribe / publish events |
| `ctx.registry`, `ctx.selection`, `ctx.targets` | nodes, selection, target resolution |
| `ctx.runner`, `ctx.sessions` | exec jobs, interactive sessions |
| `ctx.presets` | register / look up presets |
| `ctx.parsers` | register named output parsers |
| `ctx.pollers` | periodic per-node tasks with start/stop |
| `ctx.stores` | outputs, samples, observations, runs |
| `ctx.settings(plugin_id)` | the plugin's `features:` section |
| `ctx.ui` | `add_tab`, `add_tile_badge`, `add_action`, `add_preset_kind` (GUI only) |

Plugins live in `labhub/plugins/<name>/` and are found at startup. A failing plugin is disabled and reported in the log; the rest of the app keeps working.

## 8. Future plugins

| Plugin | Builds on |
|---|---|
| Association tracking | Monitor samples; adds association lost/changed events, a history, and alerts |
| Ping automation | runner + backbone interfaces, e.g. ping from CPE1 BB over CPE1.1015; results table |
| Traffic automation | iperf server on AP BB, clients on CPE BBs; uses interfaces and routes from the lab file |
| Log collection | ports the logCollector tasks (CNSS, pktlog) into jobs |
| Firmware management | long-running jobs with progress events; flash/verify presets |
| Topology | association + beacon stores → AP-to-CPE graph |
| Channel occupancy / RF heatmap | beacon store over time |
| Multi-user collaboration | lab file on the share + reservation file (`CPE3 used by X until …`), shared presets |
| Automation | records actions as data and replays them as scripts, from the GUI or CLI |

## 9. Technology

- Python 3.11+, PySide6 (Qt 6, Fusion style + dark QSS), PyYAML.
- `subprocess` for adb.
- `paramiko` for SSH backbones, added when needed.
- pytest for tests.
- Launcher: `LabHub.bat` creates a venv in `%LOCALAPPDATA%\LabHub` and installs the requirements, the same way DevReader does. A PyInstaller exe can be built later.

## 10. Project structure

```
labhub/
├─ LabHub.bat · requirements.txt · run_labhub.py · README.md
├─ docs/DESIGN.md
├─ labs/example.yaml
├─ labhub/
│  ├─ app.py                 entry point
│  ├─ core/                  models · events · config · registry · targets ·
│  │                         runner · sessions · presets · stores · plugins · context
│  ├─ transports/            base · adb · ssh · local
│  ├─ discovery/             adb_discovery
│  ├─ parsers/               wifistats · iw · ifconfig · scan
│  ├─ plugins/               commands · monitor · beacons · (future …)
│  ├─ presets/builtin.yaml
│  └─ ui/                    main_window · device_strip · command_bar · console ·
│                            preset_panel · mapping_editor · qt_bridge · theme
└─ tests/                    core + parsers (sample outputs in tests/data/)
```

## 11. Open questions

1. How are backbones reached: SSH (host/user), adb, or the PC itself?
2. One snapshot of real output for the AP stats and CPE stats commands, to build the parsers.
3. What QCMAP_CLI prints when it waits for input, to set the prompt pattern.
4. Which command shows the scan results after `hw_ver_override 2`, for Beacons.

## 12. Build order

1. Core: config, registry, targets, events, adb discovery, adb/local transports, job runner.
2. GUI: dark main window, device strip, command bar, console panes, mapping editor.
3. Presets with parameter buttons, preset editor.
4. Sessions and QCMAP sequences.
5. Monitor: parsers and dashboard cards.
6. Beacons.
