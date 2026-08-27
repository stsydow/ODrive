# QtGUI — Development Plan

A lightweight native desktop GUI for the ODrive brushless motor controller,
focused on practical control of a single axis (Axis 0).

This document covers the **features and the context for why they exist**:
- Machine / motor / device-config specifics → **`Hardware.md`**
- Implementation details (connection, threading, design decisions) → **`ARCHITECTURE.md`**

---
## Working notes — where I left off
- GPIO curve / filter / dead zone
- friction feed forward
- learn kalman filter
- oscilloscope?

---

## Design Principles

Project-wide rules. Implementation specifics (close behavior, Ctrl+C, threading) live in `ARCHITECTURE.md`; device/motor context lives in `Hardware.md`.

**The GUI is a monitor / settings interface, not a controller.**

- It **never performs realtime control** and never should be required to. Safety (limits, error-stops, velocity/current limiting) is enforced by the **firmware + controller**.
- The UI may write a setpoint or select a mode, but **does not drive or supervise** the control loop. Once a speed is set, **the motor keeps running independently** of the GUI.
- **Closing the GUI, reconnecting, or losing connection never stops the motor.** The GUI commands the device only through explicit user actions: **Run (Closed Loop)**, **Stop (Idle)**, and **Execute State**.
- Connect/disconnect transitions only tear down GUI references — there are **no implicit writes** to `requested_state`.
- **Setpoints are applied only on explicit confirmation** (Apply button or Enter key). Adjusting a setpoint field never commands the motor; only a confirmed apply sends it to the device.
- **Saving configuration requires confirmation and idles the device first.** Any NVM write (`save_configuration`) is an explicit, user-confirmed action that first transitions the axis to **IDLE** before writing — a config/calibration save can never happen while the motor is running.

---

## 1. Context

**Hardware context:** the target machine (ODESC v4.2 board, hall-sensor BLDC-Motor (78W), Pfaff 130 ) and the known-good device
configuration (`sew_config`) are documented in **`Hardware.md`**. None of it is
assumed at runtime (see §4.5 Portability): all parameter values are read from
the connected device, never hard-coded.

**Implementation status:** the running GUI and its design decisions are
documented in **`ARCHITECTURE.md`**; the roadmap below tracks what is done and
what remains.

---

## 2. Target Module Structure

```
QtGUI/
├── main.py              # App entry, main window, connection, menu bar, readings  ✅
├── backend.py           # GuiBackend(QObject): single QML-facing API (device logic)  ✅
├── status_backend.py    # StatusBackend(QObject): status-footer state              ✅
├── configtree.py        # Config Browser tree model over the device graph (§2.4)   ✅
├── errors.py            # Current error decode + dialog (Phase 2)                ✅
├── eventlog.py          # In-memory UI/device event log + viewer (Debug menu)   ✅
├── monitoring.py        # Live plot: sample buffer + pyqtgraph window (Phase 3)   ✅
├── calibration.py       # Calibration wizard + inertia/friction tests (Phase 4)   ⬜ planned
├── ruff.toml            # Lint config                                              ✅
├── check.sh             # Lint/type-check runner                                   ✅
├── Plan.md              # ← this file (features + context)
├── Hardware.md          # Machine / motor / device-config reference
└── ARCHITECTURE.md      # Implementation details
```

---

## 3. Feature Roadmap

### Phase 1: Control Settings ✅ DONE

**Implemented in `controls.py`**: `SettingsTabs(QTabWidget)` with three tabs
(Electrical Limits | Mechanical Limits | Control Parameters) and an
`InputModeSelector(QComboBox)` (moved into the Control Command section).

#### 1.1 Gains & Limits Panel (`controls.py`) ✅

Live-editable spinboxes that read the current device value on connect:

| Parameter                    | Attribute                                        | Units         | Notes                                                                                                                                                                                                                                                                                                                                   |
|------------------------------|--------------------------------------------------|---------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Velocity gain                | `controller.config.vel_gain`                     | N·m/(turn/s)  | Main velocity tuning                                                                                                                                                                                                                                                                                                                    |
| Velocity integrator gain     | `controller.config.vel_integrator_gain`          | N·m/turn      | Removes steady-state error (accumulates `vel_error × dt`)                                                                                                                                                                                                                                                                               |
| Velocity integrator limit    | `controller.config.vel_integrator_limit`         | N·m           | Cap integrator windup. Set to in relation to the motors rated continuous torque — any value above the motor's continuous torque capability is effectively no cap. Community heuristic is ~50 % of peak torque; the tighter rated-continuous value is preferred for the sewing machine so the integrator never exceeds continuous rating |
| Position gain                | `controller.config.pos_gain`                     | (turn/s)/turn | Position mode only                                                                                                                                                                                                                                                                                                                      |
| Current limit                | `motor.config.current_lim`                       | A             | Show continuous / rated / peak context                                                                                                                                                                                                                                                                                                  |
| Current limit margin         | `motor.config.current_lim_margin`                | A             |                                                                                                                                                                                                                                                                                                                                         |
| Velocity limit               | `controller.config.vel_limit`                    | turn/s        | controller error-stops if exceeded                                                                                                                                                                                                                                                                                                      |
| Enable torque-mode vel limit | `controller.config.enable_torque_mode_vel_limit` | bool          |                                                                                                                                                                                                                                                                                                                                         |
| Gain scheduling              | `controller.config.enable_gain_scheduling`       | bool          |                                                                                                                                                                                                                                                                                                                                         |
| Inertia (feed-forward)       | `controller.config.inertia`                      | N·m/(turn/s²) | Write `inertia` once measured (Phase 4)                                                                                                                                                                                                                                                                                                 |

#### 1.2 Input Mode Selector ✅

Dropdown with the relevant modes. The sewing machine recommendation is **VEL_RAMP**.

| Mode | Value | Use Case |
|------|-------|----------|
| PASSTHROUGH | 1 | Direct setpoint |
| **VEL_RAMP** | **2** | **Recommended** — smooth ramps, uses `vel_ramp_rate` + `inertia` |
| POS_FILTER | 3 | Position mode only |
| TRAP_TRAJ | 5 | Position mode (future needle positioning) |
| TORQUE_RAMP | 6 | Smooth torque ramps |

#### 1.3 Integration ✅

- `controls.py` provides a `SettingsTabs(QTabWidget)` (live-editable gain/limit/feed-forward spinboxes, read from device on connect) and an `InputModeSelector(QComboBox)`.
- Placed as a plain "Control Settings" section in the main window below the existing velocity control section (the box is *not* collapsible — it's always visible).

### Phase 2: Error Display & Config Browser ✅ DONE

**Goal:** Replace the raw integer error with decoded, actionable info, and add a full config tree.

#### 2.1 Error Decoding (`errors.py`) ✅

Uses direct property access guarded by `DEVICE_EXCEPTIONS` and inlined bitmask
decoding against `odrive.enums`. Returns structured data:

```python
@dataclass
class ErrorReport:
    timestamp: float
    sources: list  # [ErrorModule(name, value, errors: list[str]), ...]

    @property
    def any(self) -> bool: ...
```

#### 2.2 Current Errors ✅

- `ErrorDialog` (Device > Errors… or the footer `Err:` indicator) shows the live
  decoded errors per source, color-coded; a Clear Errors button.
- Replaces the single raw integer error label.

#### 2.3 Event Log ✅

- `eventlog.py` keeps a time-stamped ring buffer (max 1000 `LogEntry`s) recording
  device events: connect/disconnect, axis-state transitions, control-mode
  changes, setpoint applies, config actions, and error transitions (new errors + clears).
  The entries leading up to an error thus show context of what happened before.
- Shown in `LogDialog` via **Debug > Event Log…**, with export-to-file. Non-modal
  and live-updating: a single shared instance stays open beside the main window
  and refreshes itself by observing appended entries (via the `log_updated` Qt signal).
- The viewer works even while disconnected, so the run-up to a disconnect is visible.

#### 2.4 Config Browser (Read + IDLE-Gated Write) ✅ DONE

QML `Window` + `TreeView` walking the live ODrive object graph recursively. Full config view **and edit** without `odrivetool`. Decisions:

- **Structure** is walked eagerly from the local Python object graph (`getattr` recursion — attribute names cost no USB traffic; only leaf *values* do). Recursion capped at `MAX_DEPTH = 7` (deepest chain in `Firmware/odrive-interface.yaml` is 5 levels: `odrv → axis → motor/controller → config → sub-config`; +2 slack). Callables and endpoint refs are not traversed.
- **Values load lazily** on subtree expansion, plus a manual Refresh button. No continuous polling — it would fight the 100 ms main poll over the same USB link.
- **Name filter**: case-insensitive text box filtering the tree by path name.
- **Editable = `.config` leaves only** (float/int/bool). Everything else (estimates, errors, states) is read-only display; enum-valued leaves show and edit as raw ints (odrivetool parity; names can come later if misread).
- **Edit UX**: an `edit` button on each writable leaf opens a small OK/cancel dialog that commits on OK.
- **IDLE gating with commit-time re-check:** writes are only committed when the axis is IDLE; the gate is checked again at commit time (refuse + event-log if the state changed while the editor was open). Rationale: raw config changes while running can cause bad control behavior.
  - This gate also applies to the **Electrical Limits** and **Mechanical Limits** settings tabs (§1.1). The **Control Parameters tab stays write-anytime** — online gain tuning is handy, and a bad value trips the limit error rather than causing silent misbehavior.
- Every write lands in the event log (`WRITE` entry), like all device writes.

#### 2.5 Integration ✅ (errors + config browser)

- `errors.py` provides: structured error decoding (`ErrorReport`/`ErrorModule` dataclasses) and the `ErrorDialog` (current decoded errors + clear) that replaces the raw-integer error label. ✅
- `eventlog.py` provides the time-stamped event log (`LogEntry`/`format_log`) and `LogDialog` (Debug > Event Log…, offline-capable, non-modal + live via an observation signal, export). ✅
- `controls.py` also provides the read-only Config Browser dialog (`QDialog` + `QTreeWidget`). ⬜ superseded — see §2.4 (QML browser with IDLE-gated write)
- Config Browser implemented as `configtree.py` (`ConfigTreeModel`) + `qml/ConfigBrowserDialog.qml` (Device > Config Browser…): lazy subtree walk, name filter, per-row `edit` buttons for `.config` bool/int/float leaves, IDLE-gated writes with commit-time re-check (`backend.writeBrowserValue`). The Electrical/Mechanical Limits tabs use the same gate via Qt's enablement cascade: they bind `enabled: connected && backend.axisIdle`, so their rows can't fire while the axis runs (residual ≤100 ms poll-lag race accepted; firmware enforces real safety). ✅
- Device menu: the standalone "Dump Errors…"/"Clear Errors" become a single "Errors…" action (plus a clickable `Err:` footer field) that opens the error dialog; "Config Browser…" added beside Live Plot. ✅

### Phase 2.5: Migrate UI to QML (DONE ✅)

**Goal:** Swap the QWidget front-end for a declarative **Qt Quick (QML)** UI while
keeping every feature and design principle intact. The migration is a front-end
swap, not a rework: all device logic (connection, 100 ms poll, error decode,
config read/write, event log) stays in Python and is exposed to QML through one
backend `QObject`.

**Why this shape:** the connection/threading model in `ARCHITECTURE.md` (`threading.Thread`
+ `QTimer.singleShot(0, ...)` crossings, single source of truth `self.odrive`) is
Qt-mechanism-based, not widget-based — it survives the swap unchanged. QML takes
over layout, state binding, and dialog chrome; Python keeps the logic that is
testable and hardware-coupled. `PySide6` already ships QtQml/QtQuick in the repo
venv, so no new dependency.

**Result:** `controls.py` and the widget `ODriveGUI`/dialog classes are removed;
`errors.py`/`eventlog.py` hold only pure logic; `backend.py` + `qml/` replaced the
widget tree. The UI uses the **Fusion** Qt Quick style (desktop look, matches the
old widget theme). A headless **pytest suite** (`tests/`, offscreen + mock ODrive)
covers the backend logic and QML loading/linkage.

#### Structure

```
QtGUI/
├── main.py          # entry: QApplication + QQmlApplicationEngine + backend wiring
├── backend.py       # GuiBackend(QObject) — the single QML-facing API (all device logic)
├── errors.py        # read_error_report / format_current (pure logic)
├── eventlog.py      # LogEntry / format_log (pure logic)
├── qml/
│   ├── main.qml            # ApplicationWindow: menubar, control bar, pinned footer,
│   │                       #   Control Command, Settings tabs (2-column grid)
│   ├── SetpointRow.qml     # editable DoubleSpinBox setpoint row (Control Command)
│   ├── SpinRow.qml         # reusable numeric settings row (feature-gated)
│   ├── CheckRow.qml        # reusable boolean settings row (feature-gated)
│   ├── ErrorDialog.qml     # movable Window dialog, live errorsText
│   ├── EventLogDialog.qml  # movable non-modal Window, live logText
│   └── DeviceInfoDialog.qml # movable Window dialog
└── tests/          # headless pytest (offscreen, mock ODrive)
```

#### 2.5.1 Backend object (`backend.py`) ✅

The current `ODriveGUI` logic (device ref, connect/reconnect, poll timer, control
handlers, setpoint sync, feature gating, event log) relocates into
`GuiBackend(QObject)` unchanged in substance, exposed to QML as the context
property `backend`:

| QML side (binding / handler) | Backend side |
|------------------------------|--------------|
| `backend.connText` / `connColor` | `_set_conn()` behaviour |
| `backend.stateText`, `errorText`, `vbusText`, `powerText` | the 100 ms poll results |
| `backend.run()`, `stop()`, `applySetpoint()`, `startState(name)` | existing `on_*` control handlers |
| `backend.setMode(name)`, `inputModes`, `setInputMode(idx)` | mode/input-mode change + selector population |
| `backend.setConfig/getConfig/hasConfig(base, attr, ...)` | settings read/write + `hasattr` gating |
| `backend.logEvent(cat, msg)`, signal `logUpdated`, `logText` | event log + live viewer |
| `backend.save/export/importConfig()`, `reboot()`, `clearErrors()`, `exportLog()`, `deviceInfoText()`, `setVerbose()` | menu/footer actions (`QFileDialog`/`QMessageBox` native) |

#### 2.5.2 Main window (`qml/main.qml`) ✅

The status footer lives in the `ApplicationWindow.footer` property (pinned to the
window bottom, like the old `QStatusBar`) — not a row in the body layout.

Qt Quick Controls 2 `ApplicationWindow`, mirroring the current layout 1:1:
- **Menubar:** Device (Save/Export/Import Config, Reboot, Errors, Device Info) + Debug (Verbose, Event Log, Force Reconnect) — actions call backend slots.
- **Control bar:** ▶ Run / ■ Stop + Program dropdown + Start (states execute only via explicit button, unchanged).
- **Status footer:** the five composed fields (connection / state / error / VBus / power), bound to backend properties; the `Err:` field stays clickable. Error transitions keep logging to the event log.
- **Control Command:** mode combo + input-mode selector + the three setpoint rows (velocity/torque/position) with the same visibility switching and closed-loop `enabled` gating; setpoints go to the device only on **Apply** / **Enter** (no implicit writes).

#### 2.5.3 Settings tabs — declarative rows, no spec tables ✅

`controls.py`'s `_SPINS`/`_CHECKS` positional-tuple tables and the
`_write_scalar_rows` pairing parser are widget-era scaffolding and are **not**
ported. Instead, one reusable component per row type + a generic backend config
API:

- `SpinRow.qml` / `CheckRow.qml` components declare a parameter as named
  properties (`attr`, `base`, `label`, `unit`, `min`, `max`, `decimals`,
  `step`); each row self-manages feature gating, read-on-bind and
  write-on-change against the backend.
- Backend gains three generic slots: `hasConfig(base, attr)` (the `hasattr`
  gate), `getConfig(base, attr)` (read-on-bind), `setConfig(base, attr, value)`
  (write-on-change, `DEVICE_EXCEPTIONS`-guarded, with a sync guard so a
  backend refresh doesn't echo-write).
- Tabs become `GridLayout`s of named rows (each tab is a 2-column grid via
  `columns: 2`, one row per param). Tooltips (`requested_current_range`) are row
  properties.
- What remains in Python: gating + read/write semantics, error handling,
  tooltip text. `_RowConfigPanel`/`SettingsTabs` are deleted.

#### 2.5.4 Dialogs ✅

Error dialog, Event Log, and Device Info are top-level Qt Quick **`Window`s**
(`qml/ErrorDialog.qml`, `qml/EventLogDialog.qml`, `qml/DeviceInfoDialog.qml`) —
native title bar, movable/resizable, sized to their content — opened from the
menubar / footer (`errorDialog.show()` etc.). Dialog content is backend-exposed:
the error dialog binds `backend.errorsText`, the event log binds live
`backend.logText`, and device info calls `backend.deviceInfoText()` on open.
Native `QFileDialog` for config import/export and log export stays in the backend
(`backend.exportLog()`); the import/reboot confirm prompts keep `QMessageBox`
(synchronous native modals — deferred; convert to QML prompts only if styling is
wanted). Event Log stays non-modal + live via the `logUpdated` signal. Widget
`ErrorDialog`/`LogDialog` classes are deleted from `errors.py`/`eventlog.py`, which
now hold only pure logic.

#### 2.5.5 Acceptance ✅

Automated: `check.sh` runs ruff, mypy, py_compile, **qmllint** (ships with
PySide6), and a headless **pytest** suite (`qtgui/tests/`, `QT_QPA_PLATFORM=offscreen`,
mock ODrive) covering backend logic (mode/setpoint/mode-switch, config API,
errors/device-info/event-log text) and QML loading + linkage (mode/input-mode
combo follow the backend).

Manual pass on a real ODrive: done (connect, run/stop, mode switch, setpoint
apply, settings edit with device echo, errors view + clear, event log,
config export/import).

### Phase 3: Monitoring & Plotting (3.1 DONE ✅)

**Goal:** Live visual feedback for tuning, plus data logging.

#### 3.1 Live Plot ✅

Implemented in `monitoring.py` (+ sampling in `backend.py`, Device > Live Plot…):
- Native top-level **pyqtgraph** window (`PlotWidget`) over the same `QApplication`
  (pyqtgraph is QWidget-based; no QML embedding glue).
- Channels (label/unit registry in `monitoring.CHANNELS`, device readers in
  `backend._PLOT_READERS` — one entry each to observe another property later):
  velocity (`vel_estimate`), position (`pos_circular` if exposed, else
  `pos_estimate`), current Iq (fw-derived from its two measured phase currents),
  raw phase currents phA/phB, torque (= Iq × `torque_constant`; no fw endpoint
  on 0.5.x), setpoint (loop-side `*_setpoint`, post input-mode filtering) and
  input (`input_*`, what Apply wrote). Missing endpoints → NaN → curve gap
  (feature-gated).
- Later channel candidates (from the operator's odrivetool liveplot history;
  each is one `CHANNELS` entry + one `_PLOT_READERS` entry):
  mechanical/electrical power (`controller.mechanical_power` /
  `electrical_power`), bus current (`motor.I_bus`), velocity integrator torque
  (`controller.vel_integrator_torque`).
- Plot sampling runs at **~167 Hz** on its own 6 ms timer (`plotTick`) — budget from the §3.4 Step 0 benchmark: ~4000 Hz single-channel ceiling, keep total USB transfers **under 3000 Hz** for stability margin (15 readers × 167 Hz + 10 Hz status poll ≈ 2600 Hz); the status footer/control poll stays at 10 Hz (`updateReadings`). Sampling happens regardless of the window
  being open — history exists when the plot is opened; nothing sampled while
  being open; nothing sampled while disconnected. Fixed-retention ring buffer (`SampleBuffer`, 60 s @ 167 Hz ≈ 10k rows).
- Window selector 5/30/60 s; Pause freezes redraw only — sampling continues,
  resume shows an unbroken trace. Full redraw of ≤833 pts/curve (5 s window; 10k worst-case) at 10 Hz
  (`connect="finite"`) instead of incremental append.

#### 3.2 Data Logging (after the Recorder)

Not a separate logger: the Recorder (§3.4 Stage A) collects the data trace, and its per-recording **Save button** writes it out as CSV (`odrive_YYYYMMDD_HHMMSS.csv`, header row = channel labels). Reuses `SampleBuffer.csv()`; nothing new to build beyond wiring the button.
- Rationale: one capture path instead of two parallel ones — anything worth logging is worth recording through the same interval/checkbox UI.

#### 3.3 Dependencies

✅ `pyqtgraph` in `requirements.txt` (installed via `uv pip install pyqtgraph`).
CSV export reuses `SampleBuffer.csv()` (header = channel labels).

#### 3.4 Oscilloscope Recording

One-shot capture-and-plot, beside the live plot (not replacing it): click Record, capture for an interval, plot the recording. Two stages plus a measurement step:

**Step 0 — poll-rate benchmark** (`tools/bench_poll_rate.py`, CLI against real hardware) ✅ DONE:
reads `odrv.n_evt_control_loop` (firmware-global control-loop tick counter) 4000 times with timestamps; prints sustained sample rate, inter-sample gap jitter, ticks-per-read distribution, and the dt↔dtick correlation. The counter doubles as an internal clock reference: its delta over the run gives the true loop frequency, so reads/loop is measured directly rather than inferred.

Decision bar: ≥100 Hz × 3 channels = useful, ~200 Hz × 4 channels = good — measured single-channel rate is an upper bound, so divide it by 3–4 for per-channel rate before judging.

**Measured (real hardware, idle host):** ~3900–4340 Hz single-channel (`n_evt_control_loop`), Δt 0.24 ms median / ~0.36 ms p95 / max ≤ 0.8 ms across consecutive runs. Loop frequency confirmed at **8000–8002 Hz** via the counter (matches 168 MHz/(6·3500)). dt↔dtick correlation is a sensitive host-load indicator: **~0.80 stable on an idle machine**; transient background load both inflates Δt-max (seen: 3.3 ms / 28 ticks) and drags corr down to 0.2–0.5. Ticks/transfer ≈ 0.5 ⇒ sequential polling sees only half of all loop ticks (bursts up to ~15 unobserved under load) — polling is fine for Stage A's display needs, and this quantifies why Stage B needs subscriptions instead.

**Stage A — interim GUI recorder** (conditional on Step 0 meeting the bar):
- Channel checkboxes generated from the `monitoring.CHANNELS` registry (same readers as §3.1).
- Interval spinbox in the dialog. During recording the recorder temporarily *is* the poller: stop `update_timer`, run the fast read loop, restart — no parallel-sampling coordination.
- Each recording opens a new pyqtgraph window using the same plot layout as §3.1; recordings can be compared side-by-side.
- Save button per window → CSV export (reuses `SampleBuffer.csv()`).
- Purpose meanwhile: judge sensor noise and control lag. It cannot see inside the control loop.

**Stage B — firmware oscilloscope extension** (kept, not scheduled): the firmware's built-in `oscilloscope` object samples at the **control-loop rate, 8 kHz** (TIM1/8 @ 168 MHz, `PERIOD_CLOCKS=3500`, `RCR=2` ⇒ `CURRENT_MEAS_HZ = 8000`; `oscilloscope_.update()` runs inside `control_loop_cb()`, loop-synchronous with current sampling). Its 4096-sample buffer gives **T_max ≈ 0.51 s per recording**. Currently dead code: `trigger_src`/`data_src` are hardwired to `nullptr` at compile time (`Firmware/MotorControl/odrive_main.h`). Wiring 4–8 real endpoints in is a separate self-contained firmware change (like the friction-compensation work in Phase 4).

Complementary to Stage A rather than competing: Stage B's value is capturing **one loop-internal signal at full 8 kHz resolution** over half a second (e.g. `Iq_setpoint` vs `Iq_measured`) — what §4.3 step-response tuning will want. For multi-channel noise/lag surveys, Stage A wins on channels × duration; GUI reads can't see inside the control loop either way.

**Design reference:** the odrive tooling **0.6.11** implements exactly this pattern natively — `odrive/high_rate_capturer.py` (vendored copy on branch `odrive-0.6.11-vendored`): subscription-based ring buffers at control-loop rate with cycle-count/nanosecond timestamps and a configurable trigger point, riding on `libodrive`. Use it as the blueprint for Stage B — or as the ready-made implementation if/when the stack moves to 0.6 tooling (the 0.5.6 libfibre has no subscription API to backport it to).

### Phase 4: Torque Drive + Calibration + Dynamics (NEXT ⬜)

**Goal:** Low-speed torque drive experiment, calibration workflow, step response tuning, dynamics measurement.

#### 4.1 Low-Speed Torque-Drive Support

The insight: at low speed, **torque drive** (commanding current directly) avoids the velocity PID fighting the jittery hall velocity estimate.

**Context (low-speed behaviour of hall sensors):** Hall feedback gives only 6 states per pole pair — very low resolution — so the PLL velocity estimate (`snap_to_zero_vel` branch in `encoder.cpp`) forces velocity to 0 below a threshold, causing low-speed vibration/cogging, and near-zero velocity control behaves like position control. Community reports confirm this is expected for hall sensors (`discourse.odriverobotics.com/t/precise-low-speed-velocity-control/6758`, .../low-frequency-noise-with-hall-encoders-odrive-3-6/10464): lowering the encoder bandwidth improves estimate smoothness but does not remove steady-state oscillation.

A "Low-speed torque mode" preset that:
- Sets `control_mode = TORQUE_CONTROL`
- Enables `enable_torque_mode_vel_limit` + sets `vel_limit` (safety — prevent runaway)
- Applies friction-compensation torque offset
- Lets the user command torque directly (later mapped to pedal)

#### 4.2 Calibration Wizard (`calibration.py`)

A multipage `QDialog` via `QStackedWidget`:

| Step | Action | Error Handling |
|------|--------|----------------|
| **0. Pre-check** | ⚠ Ask the user to disengage the handwheel clutch and confirm. Belt load causes calibration to fail (motor jumps electrical revs). No sensor — relies on the user. |
| **1. Motor Profile** | Load the motor preset (`pole_pairs`, `motor_type`, `current_lim` — see `Hardware.md`), phase R/L from config. Editable. | Validate CPR = 6 × pole_pairs |
| **2. Encoder Config** | Set `mode=HALL`, CPR = 6 × pole_pairs | Validate consistency |
| **3. Motor Cal** | Run motor calibration, poll state + errors at 50 ms | Detect return to IDLE or error |
| **4. Hall Phase Cal** | Run `ENCODER_HALL_PHASE_CALIBRATION` | Same polling + error decode |
| **5. Hall Polarity Cal** | Run `ENCODER_HALL_POLARITY_CALIBRATION` | Same |
| **6. Encoder Offset Cal** | Run `ENCODER_OFFSET_CALIBRATION` | Same |
| **7. Summary** | Pass/fail per step, decoded errors, prompt to save config | — |

**Already-calibrated baseline:** The GUI reads `pre_calibrated` (motor + encoder) and the hall flags on entry. For any step that is already valid, the wizard offers **Skip** (recommended) or **Recalibrate**. Recalibrating stages its writes first and only commits to the device **after an explicit confirmation** — the working baseline is never clobbered without the user's OK.

**Disconnect safety:** If the device disconnects during any step, the wizard shows an error. After reconnect, the wizard reads the device state to sync the UI — it does not change the device state. The user can retry or cancel the wizard.

**Calibration current:** Pre-fills `calibration_current` to the device's saved
value. If the motor jumps electrical revs, suggests increasing it.

**Finalize & save (`pre_calibrated`):** After the calibration steps pass, run a **functional test** (spin under control, confirm sensible readings). Only then set `encoder.config.pre_calibrated = true` (and `motor.config.pre_calibrated` when applicable) and **save** to NVM. Saving follows the project rule (see Design Principles): the device is put into **IDLE first** and the save requires **user confirmation** — so a calibration/config write can never occur while the motor is running.

**Calibration current settings (distinct, from the interface):** several currents exist along the calibration paths and are exposed read/write in a dedicated **Calibration** tab of the Control Settings surface (beside Electrical Limits / Mechanical Limits / Control Parameters), so the operator can align them before calibrating:
- `motor.config.calibration_current` — current for measuring phase R/L during `AXIS_STATE_MOTOR_CALIBRATION`; set relative to the motor's rated/torque-measurement current (the machine's tuned value is in `Hardware.md`).
- `axis.config.calibration_lockin.current` — current for the open-loop lockin spins used by encoder offset / index / hall-polarity / hall-phase calibration (`encoder.cpp` uses `calibration_lockin`); set relative to the motor's rated current.
- `axis.config.general_lockin.current` — current for `AXIS_STATE_LOCKIN_SPIN` (manual lockin spin).
- `axis.config.sensorless_ramp.current` — sensorless-only, **not** used for this hall/BLDC machine (skip).
- `motor.config.resistance_calib_max_voltage` — related calibration *voltage* (max V for the R measurement), exposed alongside the currents.

**Plumbing (future):** these live on `axis.config` (`calibration_lockin.{…}`), so the panel `bind()` gains an `axis` ref and the attribute-read/write helper gains dotted-path support (e.g. `calibration_lockin.current`). Generic and feature-gated (`hasattr`) like the rest of §1.

#### 4.3 Step Response Test (Tuning Aid)

A step change in setpoint while recording the system response. Not a dashboard feature — it's a **tuning tool** to measure overshoot, settling time, and steady-state error.

- Custom implementation (or `odrive.utils.step_and_plot()` if available).
- Plots setpoint vs. actual response.
- Shows numeric metrics: overshoot %, settling time, steady-state error.

#### 4.4 Inertia & Friction Measurement

"Measure Dynamics" section. Each test runs multiple repetitions; shows **mean ± standard deviation**.

| Test | Method | Output | Used For |
|------|--------|--------|----------|
| **Break-away torque** | Minimum torque to start motion from rest | Static friction (N·m) | Start-up torque pulse in pedal mapping |
| **Running friction** | Torque needed to run steadily at ~1 rps | Coulomb friction (N·m) | Torque offset in all control modes |
| **Inertia** | Accelerate 1 → 10 rps, compute J = τ / α | Inertia (N·m/(rps/s)) | `controller.config.inertia` |

**Important:** Break-away friction is ~6× larger than running friction (measured manually). They must NOT be conflated. The GUI stores them as separate values.

**How results are used:**
- `inertia` → written to `controller.config.inertia` (feed-forward for VEL_RAMP/TRAP_TRAJ). This is a pure GUI/config change — no firmware work.
- `break_away_friction` and `running_friction` → **consumed by firmware** (start-up torque pulse on moving; friction-compensation torque offset added to `input_torque` in all modes, so the motor actually starts at low pedal positions).

**Firmware modification — separate stage:** There is no consistent way to compensate friction / apply a start-up pulse from the GUI alone (ODrive has no built-in friction-compensation endpoint), so this is an **explicit, self-contained firmware change** on `Firmware/MotorControl`. It is tracked as its own stage, independently testable and reverted, decoupled from the GUI measurement stage. The firmware change is defined before the pedal mapping (Phase 5) so the friction offset exists when pedal input lands.

> **FUTURE firmware option — torque input filter:** As an alternative to `VEL_RAMP` for the control input, a low-pass **torque filter** (smoothing the commanded torque/current input) may be added to the firmware. This is a candidate firmware change to revisit if velocity ramping proves unsuitable (e.g. for pedal torque-mode sewing) — a separate, independently testable firmware change like the friction work above.
>
> **Context — why `TORQUE_RAMP`/`torque_ramp_rate` does NOT substitute:** `TORQUE_RAMP` is an *input mode* that ramps `torque_setpoint_` only (`controller.cpp` `INPUT_MODE_TORQUE_RAMP`) — it never updates `vel_setpoint_`. In `VELOCITY` control the loop runs on `vel_setpoint_`, so `TORQUE_RAMP`+`VELOCITY` leaves the velocity setpoint frozen and merely injects a ramping torque offset; it cannot smooth a *velocity* command. It only makes sense paired with `TORQUE_CONTROL` mode (the low-speed torque-drive / pedal-torque path). Enum note (this firmware): `CONTROL_MODE_VELOCITY_CONTROL = 2`, `CONTROL_MODE_TORQUE_CONTROL = 1`, `INPUT_MODE_TORQUE_RAMP = 6`. `VEL_RAMP` is the velocity-mode smoothing mechanism; reserve `TORQUE_RAMP` for torque mode. The sewing baseline uses **VELOCITY (2) + VEL_RAMP (2)**.

### Phase 5: Foot Pedal (FUTURE ⬜)

**Goal:** Replace GUI setpoint with analog foot pedal input.

- Configure GPIO as `GPIO_MODE_ANALOG_IN`, read ADC.
- Custom nonlinear response curve (dead zone, friction-compensation offset).
- Two mapping modes:
  - **Velocity mode:** pedal → `input_vel` (standard sewing).
  - **Torque mode:** pedal → `input_torque` (+ friction offset + velocity limit) for smoother low speed.
- Pedal position bar/slider in UI.
- Curve editor (drag points or polynomial).

**Note:** Belt ratio and SPM display are explicitly **out of scope** — belt slip makes these unreliable. If a sensor is added later, this can be revisited.

---

## 4. Cross-Cutting Concerns

### 4.1 Error Handling Strategy

**No UI warnings are needed for speed, current, or torque** — the ODrive
controller enforces limits with its own error-stop mechanisms (`vel_limit`,
`current_lim`, `dc_max_positive_current`, `enable_torque_mode_vel_limit`).

All device communication follows this pattern:

```python
try:
    value = self.axis.controller.config.vel_gain  # or any device read/write
except Exception as e:
    self._read_failed("vel_gain", e)  # drops the link, auto-reconnects
```

- **Read failures:** Single strike — any transport failure drops the link immediately and auto-reconnects. The worker blocks in `find_any()` until the device reappears; that blocking wait *is* the reconnect mechanism.
  - **Known ceiling:** if the discovery layer itself wedges, `_connecting` stays set and only a GUI restart recovers.
- **Write failures:** Shown immediately (event log / status feedback). Not fatal — device may be in a transient state.
- **Calibration errors:** Decoded via standard bitmask logic, shown in the wizard step with a hint. User can retry or skip.

### 4.2 Feature Availability (Feature Detection)

The plan targets **ODrive v0.5.6 series** firmware. The GUI checks for **required features** on connect (not version strings):

- Check for expected attributes at each feature level (e.g., `controller.config.vel_gain`, `controller.config.input_mode`, specific control mode enums).
- If a feature is missing, disable the relevant UI section and show a message.
- **Already done:** `hasattr` guard for `odrv.reboot()` — extend this pattern to all feature-gated functionality.
- No version string filtering — the same firmware may vary by build flags.

### 4.3 Testing Strategy

| Level | What | How |
|-------|------|-----|
| **Unit** | Module-level logic (error decoding, config parsing, motor profile dataclass) | `pytest`, no hardware needed |
| **Integration** | Device communication, calibration steps | Against a mock of the hardware interface |
| **UI** | Main window layout, signal wiring, menu actions | Manual testing per phase |
| **Regression** | All phases combined | Baseline config check after each phase |

No automated GUI testing (PySide6 `QTest` is too brittle for a single-developer project). Instead, the baseline config in `Hardware.md` is checked after each change.

### 4.4 Multi-Axis Limitation

The GUI is explicitly **Axis 0 only**. This is documented in the UI and architecture. If multi-axis support is needed later, the architecture supports it by:
- Adding an axis selector dropdown.
- Making all control/monitoring widgets axis-aware.
- Storing per-axis state in a dict keyed by axis number.

### 4.5 Portability (motor / PSU agnostic)

The GUI and all features must work with a different motor and PSU than this sewing-machine setup. `Hardware.md` is **reference context only** — a documentation of one known-good configuration for feasibility checks, **never an assumption about the live device**:
- All parameter values (gains, limits, CPR, pole pairs, current/vel limits, setpoint ranges) are **read from the connected device at runtime**; nothing is hard-coded from `Hardware.md`.
- Phase 1 spinbox ranges are generous so a different motor/PSU is not clipped; per-row feature gating (§4.2) disables anything the firmware doesn't expose.
- Calibration (Phase 4.2) uses the *device's existing* values as the starting point and validates consistency (CPR = 6 × pole_pairs) rather than assuming 8 pole pairs / CPR 48.
- Machine-specific assumptions (belt ratio, SPM, pole count, PSU rating) are explicitly local to this setup and flagged as such.

### 4.6 UI is a monitor / settings interface, not a controller

General project rule — see **Design Principles** at the top of this document. Implementation details of close/Ctrl+C behaviour are in `ARCHITECTURE.md`.

### 4.7 Status footer

A composed status footer (permanent right-hand widget in the status bar) shows, in separate labeled fields — no duplicate/overlapping connection text:
- **Connection indicator**: `● Online` (green) / `● Offline` (red) / `● Connecting…` / `● Rebooting…` (orange).
- **Axis state**: always shown (`State: <AXIS_STATE_NAMES>`, e.g. `IDLE`, `CLOSED_LOOP_CONTROL`, `MOTOR_CALIBRATION`), not only while running.
- **Error state**: `Err: OK` (green) or `Err: <id>` (red), refreshed with the 100 ms poll. Decoding/hints come with Phase 2 (Error Display).
- **Bus voltage** (V), from `vbus_voltage`.
- **Power draw** (W) = `vbus_voltage × ibus`.

Transient action feedback (save/export/apply/verbose, write failures) is recorded
in the in-memory event log viewable via Debug > Event Log…; the status bar shows
only the permanent connection/state/error/bus states.

---

## 5. Dependencies

| Feature    | Dependency    | Version | Purpose                                                 |
|------------|---------------|---------|---------------------------------------------------------|
| Core       | PySide6       | ≥ 6.0   | Qt bindings                                             |
| Core       | odrive        | ≥ 0.5.0 | Device communication (local in `tools/odrive/`)         |
| Phase 3+   | pyqtgraph     | ≥ 0.12  | Live plotting                                           |
| All others | Python stdlib | —       | threading, pathlib, dataclasses, csv, json, collections |

---
