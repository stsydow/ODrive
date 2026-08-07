# QtGUI — Development Plan

A lightweight native desktop GUI for the ODrive brushless motor controller,
focused on practical control of a single axis (Axis 0). This document is
self-contained: all hardware context, configuration baselines, and design
decisions are included.

---

## Design Principles

Project-wide rules. Implementation specifics (close behaviour, Ctrl+C, threading) live in `ARCHITECTURE.md`.

**The GUI is a monitor / settings interface, not a controller.**

- It **never performs realtime control** and never should be required to. Safety (limits, error-stops, velocity/current limiting) is enforced by the **firmware + controller**.
- The UI may write a setpoint or select a mode, but **does not drive or supervise** the control loop. Once a speed is set, **the motor keeps running independently** of the GUI.
- **Closing the GUI, reconnecting, or losing connection never stops the motor.** The GUI commands the device only through explicit user actions: **Run (Closed Loop)**, **Stop (Idle)**, and **Execute State**.
- Connect/disconnect transitions only tear down GUI references — there are **no implicit writes** to `requested_state`.
- **Setpoints are applied only on explicit confirmation** (Apply button or Enter key). Adjusting a setpoint field never commands the motor; only a confirmed apply sends it to the device.
- **Saving configuration requires confirmation and idles the device first.** Any NVM write (`save_configuration`) is an explicit, user-confirmed action that first transitions the axis to **IDLE** before writing — a config/calibration save can never happen while the motor is running.

---

## 1. Hardware & Firmware Context

### Machine

| Item | Detail |
|------|--------|
| **Board** | ODESC v4.2 (STM32F405, ODrive v3.6-compatible clone) |
| **Firmware** | Stock ODrive from this repo (v0.5.6 series), one-line mod: `otp_valid_ = true` in `Firmware/MotorControl/odrive_main.h:246` |
| **Motor** | ACT 42BLF03 — 42mm BLDC, 24V, **hall sensors only** (no encoder) |
| **Belt ratio** | ~3.8:1 motor:handwheel (belt slips — **no reliable ratio or SPM without a sensor** — dropped from scope) |
| **Brake resistor** | 2 Ω (as delivered with board) |
| **PSU** | 24 V / 8.3 A (~200 W) |
| **Machine** | Pfaff 130 sewing machine, belt-driven. Mechanical clutch present (must be disengaged for calibration — belt load causes calibration to fail) |
| **Future input** | Custom foot pedal (reflex optocoupler, 0–3.3 V / 0–5 V) with custom response curve |

### Hall Sensor Resolution (verified from firmware)

In `ENCODER_MODE_HALL`, ODrive produces **6 discrete states per electrical
revolution** (60° electrical each). Effective encoder CPR = `6 × pole_pairs`.

The working config has `motor.config.pole_pairs = 8` and `encoder.config.cpr = 48`
(6 × 8 = 48). The ACT 42BLF03 datasheet lists "8 poles" — this corresponds to
**8 pole pairs (16 physical poles)** in the ODrive config, which is correct.

**Low-speed behavior:** The PLL interpolates between hall edges using `vel_estimate`.
Below a threshold, the `snap_to_zero_vel` branch forces velocity to exactly 0,
causing the vibration and cogging observed at low speed.

### Known Good Configuration Baseline

This is the saved working config (`sew_config`, Axis 0). The plan uses this as
the baseline — any deviation during development is intentional and documented.

See **Annex A** for the full parameter table. Key highlights:

| Area | Key values |
|------|-----------|
| **Motor** | `pole_pairs = 8`, `motor_type = HIGH_CURRENT`, `current_lim = 5.0 A` (compromise, see Annex B), torque constant + phase R/L calibrated |
| **Encoder** | `mode = HALL`, `cpr = 48`, hall polarity + phase calibrated, `pre_calibrated = true`, `bandwidth = 100` |
| **Controller** | `control_mode = VELOCITY`, `vel_gain = 0.0346`, `vel_integrator_gain = 0.173`, `vel_integrator_limit = 0.188` (rated continuous torque), `vel_limit = 70`, `inertia = 0` |
| **Input mode** | `input_mode = VEL_RAMP (2)` — smooth ramps for sewing machine |
| **System** | `brake_resistance = 2 Ω`, `dc_max_positive_current = 8.3 A`, overvoltage trip 26.5 V |
| **Thermistors** | FET monitored, motor thermistor disabled (continuous rating is the protection) |

### Current State (GUI)

A PySide6 GUI (`main.py` + `controls.py` + `errors.py` + `util.py`) focused on
Axis 0 velocity control. See `ARCHITECTURE.md` for implementation details.

**Capabilities now (✅ implemented):** Auto-connect/reconnect, velocity/position/
torque mode switching, control-tuning tabs (Electrical Limits | Mechanical Limits |
Control Parameters, feature-gated), an input-mode selector, setpoint spinboxes,
Run/Stop, Programm dropdown + Start, 100 ms polling, save/export/import config,
reboot, clear errors, current-errors dialog, an event log (Debug > Event Log…),
device info dialog (serial + hardware/firmware version), verbose logging (read
failures go to the debug log, not a dialog).

**Gaps (planned, see roadmap):** Calibration workflow (Phase 4), Config Browser
(Phase 2.4), monitoring/plotting (Phase 3), foot-pedal analog input (Phase 5),
hall low-speed torque drive + friction compensation (Phase 4).

---

## 2. Target Module Structure

```
QtGUI/
├── main.py              # App entry, main window, connection, menu bar, readings  ✅
├── controls.py          # Control settings: gains, limits, input mode, feed-fwd   ✅
├── errors.py            # Current error decode + dialog (Phase 2)                ✅
├── eventlog.py          # In-memory UI/device event log + viewer (Debug menu)   ✅
├── util.py              # Shared helpers: safe_getattr, DEVICE_EXCEPTIONS       ✅
├── calibration.py       # Calibration wizard + inertia/friction tests (Phase 4)   ⬜ planned
├── ruff.toml            # Lint config                                              ✅
├── check.sh             # Lint/type-check runner                                   ✅
├── Plan.md              # ← this file
└── ARCHITECTURE.md
```

---

## 3. Feature Roadmap

### Phase 1: Control Settings ✅ DONE

**Implemented in `controls.py`**: `SettingsTabs(QTabWidget)` with three tabs
(Electrical Limits | Mechanical Limits | Control Parameters) and an
`InputModeSelector(QComboBox)` (moved into the Control Command section).

#### 1.1 Gains & Limits Panel (`controls.py`) ✅

Live-editable spinboxes that read the current device value on connect:

| Parameter | Attribute | Units | Notes |
|-----------|-----------|-------|-------|
| Velocity gain | `controller.config.vel_gain` | N·m/(turn/s) | Main velocity tuning |
| Velocity integrator gain | `controller.config.vel_integrator_gain` | N·m/turn | Removes steady-state error (accumulates `vel_error × dt`) |
| Velocity integrator limit | `controller.config.vel_integrator_limit` | N·m | Cap integrator windup. Set to the 42BLF03 rated continuous torque (**0.188 N·m**, ~25 % of peak 0.75 N·m) — any value above the motor's continuous torque capability is effectively no cap. Community heuristic is ~50 % of peak torque; the tighter rated-continuous value is preferred for the sewing machine so the integrator never exceeds continuous rating |
| Position gain | `controller.config.pos_gain` | (turn/s)/turn | Position mode only |
| Current limit | `motor.config.current_lim` | A | Show continuous / rated / peak context |
| Current limit margin | `motor.config.current_lim_margin` | A | |
| Velocity limit | `controller.config.vel_limit` | turn/s | Default to 70, controller error-stops if exceeded |
| Enable torque-mode vel limit | `controller.config.enable_torque_mode_vel_limit` | bool | |
| Gain scheduling | `controller.config.enable_gain_scheduling` | bool | |
| Inertia (feed-forward) | `controller.config.inertia` | N·m/(turn/s²) | Write `inertia` once measured (Phase 4) |

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

### Phase 2: Error Display & Config Browser (PARTIALLY DONE ✅/⬜)

**Goal:** Replace the raw integer error with decoded, actionable info, and add a full config tree.

Error display (2.1–2.3) is done; the Config Browser (2.4) is not yet implemented.

#### 2.1 Error Decoding (`errors.py`) ✅

Reuse `odrive.utils.dump_errors()` logic but return structured data:

```python
@dataclass
class ErrorReport:
    system: int           # ODriveError bitmask
    axis0: AxisErrors     # axis, motor, encoder, controller, sensorless
    timestamp: float

@dataclass
class AxisErrors:
    axis: int             # decoded → list of names
    motor: int
    encoder: int
    controller: int
    sensorless: int
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

#### 2.4 Config Browser (Read-Only) ⬜ TODO

`QDialog` + `QTreeWidget` walking the ODrive object tree recursively. Full config view without `odrivetool`. **Safety:** Recursion depth limited to prevent infinite loops on circular references. Only primitive values and sub-objects displayed (no callable traversal).

#### 2.5 Integration ✅ (errors) / ⬜ (config browser)

- `errors.py` provides: structured error decoding (`ErrorReport`/`ErrorModule` dataclasses) and the `ErrorDialog` (current decoded errors + clear) that replaces the raw-integer error label. ✅
- `eventlog.py` provides the time-stamped event log (`LogEntry`/`format_log`) and `LogDialog` (Debug > Event Log…, offline-capable, non-modal + live via an observation signal, export). ✅
- `controls.py` also provides the read-only Config Browser dialog (`QDialog` + `QTreeWidget`). ⬜ not yet implemented
- Device menu: the standalone "Dump Errors…"/"Clear Errors" become a single "Errors…" action (plus a clickable `Err:` footer field) that opens the error dialog; "Config Browser…" still to be added. ✅ / ⬜

### Phase 3: Monitoring & Plotting (NEXT ⬜)

**Goal:** Live visual feedback for tuning, plus data logging.

#### 3.1 Live Plot

- `pyqtgraph` line chart.
- Channels: velocity, position, current, VBus, setpoint.
- Selectable time window (5s / 30s / 60s).
- Pause/resume.
- **Performance:** Plot updates at 100 ms (same as polling rate). Use `pyqtgraph`'s append mode to avoid full redraw.

#### 3.2 Data Logging

- CSV logging at 100 ms.
- Toggle via File menu.
- Timestamped filenames (`odrive_YYYYMMDD_HHMMSS.csv`).
- Header row with channel names.

#### 3.3 Dependencies

Add `pyqtgraph` to `requirements.txt` (needed here, not Phase 4).

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

A multi-page `QDialog` via `QStackedWidget`:

| Step | Action | Error Handling |
|------|--------|----------------|
| **0. Pre-check** | ⚠ Ask the user to disengage the handwheel clutch and confirm. Belt load causes calibration to fail (motor jumps electrical revs). No sensor — relies on the user. |
| **1. Motor Profile** | Load ACT 42BLF03 preset: `pole_pairs=8`, `motor_type=HIGH_CURRENT`, `current_lim=5.0`, phase R/L from config. Editable. | Validate pole_pairs × 6 = CPR |
| **2. Encoder Config** | Set `mode=HALL`, CPR = 6 × pole_pairs = 48 | Validate consistency |
| **3. Motor Cal** | Run motor calibration, poll state + errors at 50 ms | Detect return to IDLE or error |
| **4. Hall Phase Cal** | Run `ENCODER_HALL_PHASE_CALIBRATION` | Same polling + error decode |
| **5. Hall Polarity Cal** | Run `ENCODER_HALL_POLARITY_CALIBRATION` | Same |
| **6. Encoder Offset Cal** | Run `ENCODER_OFFSET_CALIBRATION` | Same |
| **7. Summary** | Pass/fail per step, decoded errors, prompt to save config | — |

**Already-calibrated baseline:** The GUI reads `pre_calibrated` (motor + encoder) and the hall flags on entry. For any step that is already valid, the wizard offers **Skip** (recommended) or **Recalibrate**. Recalibrating stages its writes first and only commits to the device **after an explicit confirmation** — the working baseline is never clobbered without the user's OK.

**Disconnect safety:** If the device disconnects during any step, the wizard shows an error. After reconnect, the wizard reads the device state to sync the UI — it does not change the device state. The user can retry or cancel the wizard.

**Calibration current:** Pre-fills `calibration_current = 4.0`. If the motor jumps electrical revs, suggests increasing this value.

**Finalize & save (`pre_calibrated`):** After the calibration steps pass, run a **functional test** (spin under control, confirm sensible readings). Only then set `encoder.config.pre_calibrated = true` (and `motor.config.pre_calibrated` when applicable) and **save** to NVM. Saving follows the project rule (see Design Principles): the device is put into **IDLE first** and the save requires **user confirmation** — so a calibration/config write can never occur while the motor is running.

**Calibration current settings (distinct, from the interface):** several currents exist along the calibration paths and are exposed read/write in a dedicated **Calibration** tab of the Control Settings surface (beside Electrical Limits / Mechanical Limits / Control Parameters), so the operator can align them before calibrating:
- `motor.config.calibration_current` — current for measuring phase R/L during `AXIS_STATE_MOTOR_CALIBRATION` (default 10 A; `sew_config` = 4.0 A).
- `axis.config.calibration_lockin.current` — current for the open-loop lockin spins used by encoder offset / index / hall-polarity / hall-phase calibration (`encoder.cpp` uses `calibration_lockin`; default 10 A; `sew_config` = 5.7 A).
- `axis.config.general_lockin.current` — current for `AXIS_STATE_LOCKIN_SPIN` (manual lockin spin; default 10 A; `sew_config` = 3.99 A).
- `axis.config.sensorless_ramp.current` — sensorless-only, **not** used for this hall/BLDC machine (skip).
- `motor.config.resistance_calib_max_voltage` — related calibration *voltage* (max V for R measurement; `sew_config` = 8.0 V), exposed alongside the currents.

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
    self._read_failed("vel_gain", e)  # increments failure counter, triggers reconnect if needed
```

- **Read failures:** Counted per-read. After 5 consecutive failures (~0.5 s), trigger reconnect.
- **Write failures:** Shown immediately in status bar. Not fatal — device may be in a transient state.
- **Calibration errors:** Decoded via `dump_errors()` logic, shown in the wizard step with a hint. User can retry or skip.

**Accessing optional / "maybe-none" values (general rule):** do **not** scatter `try: read … except: pass`. For a genuinely optional attribute read, use the shared `safe_getattr(obj, *attrs, default=None)` helper (in `util.py`), which centrally walks a nested attribute path, catches missing attributes / raised remote reads, and returns `default`; the caller then checks for `None`. Two kinds of reads must stay as **explicit, targeted** `try/except` (never bare `except: pass`):
- reads that must distinguish error classes (e.g. `ObjectLostError` → reconnect — see `_read_failed`), and
- writes (they are surfaced, not swallowed).

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

No automated GUI testing (PySide6 `QTest` is too brittle for a single-developer project). Instead, the baseline config in Annex A is checked after each change.

### 4.4 Multi-Axis Limitation

The GUI is explicitly **Axis 0 only**. This is documented in the UI and architecture. If multi-axis support is needed later, the architecture supports it by:
- Adding an axis selector dropdown.
- Making all control/monitoring widgets axis-aware.
- Storing per-axis state in a dict keyed by axis number.

### 4.5 Portability (motor / PSU agnostic)

The GUI and all features must work with a different motor and PSU than this sewing-machine setup. Annex A is **reference context only** — a documentation of one known-good configuration for feasibility checks, **never an assumption about the live device**:
- All parameter values (gains, limits, CPR, pole pairs, current/vel limits, setpoint ranges) are **read from the connected device at runtime**; nothing is hard-coded from Annex A.
- Phase 1 spinbox ranges are generous (e.g. current limits 0–60 A) so a different motor/PSU is not clipped; per-row feature gating (§4.2) disables anything the firmware doesn't expose.
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

| Feature | Dependency | Version | Purpose |
|---------|-----------|---------|---------|
| Core | PySide6 | ≥ 6.0 | Qt bindings |
| Core | odrive | ≥ 0.5.0 | Device communication (local in `tools/odrive/`) |
| Phase 3+ | pyqtgraph | ≥ 0.12 | Live plotting |
| All others | Python stdlib | — | threading, pathlib, dataclasses, csv, json, collections |

---

## 6. Annex A: Full Device Configuration (Known Good Baseline)

From the user's saved config (`sew_config` — Axis 0, sewing machine motor).

**This table is reference context only.** It documents one known-good configuration, used for feasibility checks and as a write-back example. It is **not** assumed to be the live device state, and every feature must work with a different motor/PSU — values are read from the connected device at runtime, never hard-coded (see §4.5 Portability).

| Parameter | Value | Notes |
|-----------|-------|-------|
| `motor.config.pole_pairs` | **8** | 8 pole pairs → 48 hall counts/rev |
| `motor.config.motor_type` | 0 (HIGH_CURRENT) | |
| `motor.config.current_lim` | **5.0 A** | Compromise between continuous and peak ratings (see Annex B) |
| `motor.config.current_lim_margin` | 8.0 A | Errors at 13 A |
| `motor.config.requested_current_range` | 12.0 A | Current sensor range |
| `motor.config.calibration_current` | 4.0 A | |
| `motor.config.torque_constant` | 0.036 N·m/A | |
| `motor.config.torque_lim` | 0.7 N·m | |
| `motor.config.phase_resistance` | 0.348 Ω | Calibrated |
| `motor.config.phase_inductance` | 0.140 mH | Calibrated |
| `motor.config.pre_calibrated` | true | |
| `encoder.config.mode` | 1 (HALL) | |
| `encoder.config.cpr` | 48 | 6 × 8 pole pairs |
| `encoder.config.hall_polarity_calibrated` | true | |
| `encoder.config.pre_calibrated` | true | |
| `encoder.config.phase_offset` | 101 | |
| `encoder.config.phase_offset_float` | 0.677 | |
| `encoder.config.direction` | 1 | |
| `encoder.config.bandwidth` | 100 | PLL bandwidth |
| `controller.config.control_mode` | **2 (VELOCITY)** | Reference baseline value for the sewing config (firmware enum: VELOCITY=2, TORQUE=1). Not assumed at runtime — GUI reads the live device value |
| `controller.config.input_mode` | **2 (VEL_RAMP)** | Smooth ramps for sewing machine, uses `vel_ramp_rate` + `inertia` |
| `controller.config.vel_gain` | 0.0346 N·m/(turn/s) | |
| `controller.config.vel_integrator_gain` | 0.173 N·m/(turn/s·s) | |
| `controller.config.vel_integrator_limit` | **0.188 N·m** | Rated continuous torque (42BLF03) — caps integrator windup |
| `controller.config.vel_limit` | **70.0 turn/s** | ~4200 RPM — above motor rated 4000 RPM, kept for now |
| `controller.config.vel_ramp_rate` | 50.0 turn/s² | |
| `controller.config.pos_gain` | 2.5 (turn/s)/turn | |
| `controller.config.inertia` | 0.0 | Feed-forward — not yet calibrated |
| `controller.config.enable_overspeed_error` | true | |
| `controller.config.enable_torque_mode_vel_limit` | true | |
| `controller.config.enable_vel_limit` | true | |
| `config.brake_resistance` | 2.0 Ω | |
| `config.enable_brake_resistor` | true | |
| `config.dc_max_positive_current` | 8.3 A | PSU limit |
| `config.dc_max_negative_current` | -0.05 A | Regen limit |
| `config.dc_bus_overvoltage_trip_level` | 26.5 V | |
| `motor_thermistor.config.enabled` | false | No motor temp sensor — continuous rating protects motor |
| `fet_thermistor.config.enabled` | true | FET temp monitored |

---

## 7. Annex B: Motor Datasheet — 42BLF Series

Source: `42BLF.PDF` (ACT MOTOR, 42BLF series brushless DC motor).

### General Specifications

| Parameter | Value |
|-----------|-------|
| Winding Type | Star |
| Hall Effect Angle | 120° Electrical Angle |
| Insulation Class | B |
| Ambient Temperature | -20°C ~ +50°C |
| Insulation Resistance | 100 MΩ min. (500 VDC) |
| Dielectric Strength | 500 VAC 1 minute |

### Electrical Specifications (per model)

| Parameter | 42BLF01 | 42BLF02 | **42BLF03** |
|-----------|---------|---------|----------|
| Number of Poles | 8 | 8 | 8 |
| Number of Phases | 3 | 3 | 3 |
| Rated Voltage | 24 VDC | 24 VDC | 24 VDC |
| Rated Speed | 4000 RPM | 4000 RPM | 4000 RPM |
| Rated Torque | 0.063 N·m | 0.125 N·m | **0.188 N·m** |
| Rated Current | 1.9 A | 3.4 A | **5.7 A** |
| Output Power | 26 W | 52 W | **78 W** |
| Peak Torque | 0.18 N·m | 0.38 N·m | **0.75 N·m** |
| Peak Current | 5.7 A | 10.2 A | **18 A** |
| Torque Constant | 0.035 N·m/A | 0.036 N·m/A | **0.036 N·m/A** |
| Back EMF | 3.7 V/KRPM | 3.8 V/KRPM | **3.8 V/KRPM** |
| Rotor Inertia | 24 g·cm² | 48 g·cm² | **72 g·cm²** |
| Body Length | 47 mm | 63 mm | **79 mm** |
| Mass | 0.33 kg | 0.48 kg | **0.63 kg** |

**Note:** The user's conservative continuous current rating for this application
(3.25 A) is lower than the datasheet rated current (5.7 A). The config compromise
`current_lim = 5.0 A` sits between the two. Pole count "8" in the datasheet
corresponds to `pole_pairs = 8` in the working device config.

---

## 8. Change Log

| Date | Change |
|------|--------|
| 2025-08 | Initial plan. Consolidation of original `Plan.md` — fixed contradictions, removed duplication, added Phase 0, corrected dependency mapping, corrected `input_mode` enum (value 6 = TORQUE_RAMP, not POS_FILTER), restored Annex A/B per user request. |
| 2025-08 | Removed UI warning noise (speed/current/torque) — replaced with controller-level protections. Replaced firmware-version detection with `hasattr` feature checks. |
| 2025-08 | Removed Phase 0 and Known Flaws section — Annex A is now the reference config (device will be reset and written). Updated Annex A values to the intended baseline (input_mode=VEL_RAMP, vel_limit=66.7). |
| 2025-08 | Review fixes: reconnect threshold confirmed as 5 consecutive failures (~0.5 s) — corrected code + ARCHITECTURE.md (was 50/~5 s). Removed module line-count estimates in §1.3/§2.5 in favour of feature descriptions. Corrected `vel_integrator_gain` units (N·m/turn). Calibration wizard now detects `pre_calibrated` and offers Skip / stage-then-confirm Recalibrate. Split friction compensation out of the GUI-only measurement into an explicit standalone firmware stage. Device menu: "Dump Errors…"/"Clear Errors" replaced by a single "Errors…" action opening the new error panel. |
| 2025-08 | Final baseline decisions: `vel_limit` kept at **70** (was 66.7) — any value >50 is practically no-limit since torque/current caps out first; `input_mode = VEL_RAMP (2)` everywhere. `vel_integrator_limit` set to **0.188 N·m** (42BLF03 rated continuous torque, ~25 % of peak) instead of 10.0, so the integrator can't demand more torque than the motor can continuously produce (community heuristic is ~50 % of peak torque — a tighter cap chosen deliberately). |
| 2025-08 | Web review: added hall low-speed performance context (§4.1) with community references; documented why `TORQUE_RAMP` + `torque_ramp_rate` does not work in `VELOCITY` mode — see §4.4 future-torque-filter note. Corrected the control_mode enum: `CONTROL_MODE_VELOCITY_CONTROL = 2` (not 1), `TORQUE=1`. Added §4.5 Portability — Annex A is reference context only, never assumed as the live device state; features are motor/PSU-agnostic. |
| 2025-08 | Added a top-level **Design Principles** section (UI is a monitor/settings interface, not a controller — never required for realtime control; closing/reconnecting/Ctrl+C never stops the motor; explicit Run/Stop/Execute-State only; no implicit `requested_state` writes; safety is the firmware/controller's job). §4.6 now cross-references it; the close/Ctrl+C *implementation* details moved to `ARCHITECTURE.md`. Ctrl+C switched to OS-level `SIG_DFL` (a Python `KeyboardInterrupt` isn't serviced while the Qt C++ event loop runs). Removed implicit `requested_state = IDLE` from `closeEvent` and reconnect cleanup. |
| 2025-08 | Investigated calibration current settings from the interface; documented the distinct currents in §4.2 (`motor.config.calibration_current`, `axis.config.calibration_lockin.current`, `axis.config.general_lockin.current`, skip `sensorless_ramp.current`; + `resistance_calib_max_voltage`). Decision: expose them in a dedicated **Calibration** tab of the Control Settings surface (beside the existing three sections); requires an `axis` ref + dotted-path attribute support in `bind()`/row helpers. |
| 2025-08 | Setpoints now require explicit confirmation: velocity/torque/position spinboxes no longer write on change — the active setpoint is sent only via the "Apply Setpoint" button or the Enter key. Added to the Design Principles (adjusting a field never commands the motor). |
| 2025-08 | Replaced the single status label (which duplicated connection text) with a composed status footer (Plan.md §4.7): connection indicator ● Online/Offline/Connecting, Err: OK/err, bus voltage (V), and power draw (W = VBus × Ibus). Refreshed by the 100 ms poll; transient action messages stay separate. |
| 2025-08 | Calibration finalize: after calibration + a functional test, set `encoder.config.pre_calibrated = true` (and motor `pre_calibrated` when applicable) then save. Saving rule added to Design Principles: any `save_configuration` is an explicit, user-confirmed action that puts the device into IDLE first. |
| 2025-08 | Optional/"maybe-none" value access: added a shared `safe_getattr(obj, *attrs, default=None)` helper (guarded nested getattr for device reads; handles missing attributes and raised reads) and a general rule (Plan.md §4.1) — no scattered `try/except: pass`; optional reads default to None, while disconnect-distinguishing reads (`_read_value`/`_read_failed`) and writes keep explicit targeted try/except. Replaced `maybe_read(fn, default)` and the scattered attribute-read blocks across `main.py`, `controls.py`, `errors.py` with it. |
| 2025-08 | Collapsed device state: the four stored references `axis`/`motor`/`encoder`/`controller` are gone; `self.odrive` is the single source of truth and the others are derived `safe_getattr`-backed read-only properties, so they can never go stale or drift out of sync. `connect_odrive`/`_on_connected` no longer hand-manage the object graph. |
| 2025-08 | Removed the collapsible "Control Settings" group-box wrapper: the `SettingsTabs` panel is added directly to the main layout (no checkable box / toggle slot / intermediate layout), and the `_on_controls_collapsed` handler was deleted. |
| 2025-08 | Device Info dialog now also shows the hardware version (`hw_version_major`/`minor`/`variant`, formatted `vX.Y` with an optional `-NV` suffix) read via `safe_getattr`. Removed the "Dump Read Failures" dialog/menu action — read failures already go to the debug log (`_read_failed` logs once per distinct error; `update_readings` logs the fallback reconnect counter), so the popup was redundant. |
| 2025-08 | Fix: on switching to closed-loop (and on connect), the active setpoint display now reads the device's current input setpoint (new `_sync_setpoint_from_device()` per control mode) instead of a stale/reset local value; removed the zeroing of the velocity spinbox on Stop. |
| 2025-08 | Phase 2 (error display) implemented: new `errors.py` (renamed from `monitoring.py`) with `read_error_report()` (structured decode of system/axis/motor/encoder/controller/sensorless), a live color-coded `ErrorPanel` with a bounded (1000) history and a history dialog (export to file). Replaced the raw error label and the Device menu's "Dump Errors"/"Clear Errors" with the decoded panel + a single "Errors…" history action. Fixed a source-base bug (axis module was reading odrv.error). Config Browser still pending (next). |
| 2025-08 | Phase 2 (error display): factored the error display and the event log into two modules/dialogs — `errors.py` (`ErrorDialog`: current decoded errors + clear, Device > Errors… or the `Err:` footer) and `eventlog.py` (`LogDialog`: time-stamped connect/state/mode/setpoint/config/error/clear event log + export, Debug > Event Log…, works while disconnected so the run-up to a disconnect is visible). |
| 2025-08 | Removed the "Readings" section; velocity & position estimates now live beside their setpoints in the "Control Command" area (compact `est:` labels). Bus voltage / power remain in the status footer. |
| 2025-08 | Control Command rows are now mutually exclusive by control mode: the velocity setpoint row (incl. its estimate) is hidden unless in velocity mode, matching torque/position rows. |
| 2025-08 | Unified the three Control Command setpoint rows: all built with shared `_make_setpoint_spin` / `_make_setpoint_row` helpers → identical `[label] [spinbox] [estimate?] [stretch]` layout; renamed `vel_set_row`→`vel_group`; immediate row-visibility on mode switch; dropped unused `format_current`/`QFont` imports. |
| 2025-08 | Fix: in circular position mode the position estimate is now displayed wrapped into [0, circular_setpoint_range) (new `_position_circular_range()`), matching the circular setpoint behaviour instead of an accumulating raw count. |
| 2025-08 | Moved the input-mode selector from the Control Settings panel into the Control Command section, restricted to modes valid for the current control mode (velocity: PASSTHROUGH/VEL_RAMP; position: PASSTHROUGH/POS_FILTER/TRAP_TRAJ; torque: PASSTHROUGH/TORQUE_RAMP). An inapplicable device input_mode is auto-corrected to the first valid mode on a mode change. |
| 2025-08 | Consolidated the repeated polled-read try/except blocks in `update_readings` into a single `_read_value(name, fn, setter)` helper returning `(value, fatal)`: non-fatal failures are logged once and return None, only `ObjectLostError` feeds the reconnect counter. |
| 2025-08 | Input-mode selector refinements: Passthrough is listed LAST (never the default) when a shaping mode exists; default is the recommended mode per control mode (VEL_RAMP / TORQUE_RAMP / TRAP_TRAJ), auto-correcting an inapplicable/Passthrough device mode; dropped the "— recommended" label suffix; combo auto-sizes to its longest item. |
| 2025-08 | Axis-state dropdown uses friendly labels ("Lock-In Spin", "Motor Calibration", …) instead of raw enum names; mapping unchanged. |
| 2025-08 | Renamed the axis-state dropdown label "State:" -> "Programm:" and the button "Execute State" -> "Start". |
| 2025-08 | Footer state field shows friendly names: "Idle", "Control Loop", "Calibration: <Programm>" (preferring the dropdown program labels), with the redundant "State:" prefix dropped for a uniform footer. |
| 2025-08 | Consolidated "Control Parameters" into the settings as a third tab (Electrical Limits | Mechanical Limits | Control Parameters), replacing the separate control-params section. |
