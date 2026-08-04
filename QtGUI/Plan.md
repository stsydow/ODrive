# QtGUI — Development Plan & Feature Roadmap

## General Context

**Project:** ODrive open-source brushless motor controller (madcowswe/ODrive)
**QtGUI role:** A lightweight native desktop GUI alternative to the Vue.js/Electron web GUI.
The web GUI is feature-rich but "wants too much at the same time" — the QtGUI should
be more focused and practical.

**Target user:** The developer (self) first, potentially published later.

### Hardware & Firmware

| Item | Detail |
|------|--------|
| **Board** | ODESC v4.2 (STM32F405, ODrive v3.6-compatible clone) |
| **Firmware** | Stock ODrive from THIS repo (v0.5.6 series), one-line mod: `otp_valid_ = true` in `Firmware/MotorControl/odrive_main.h:246` (forces the "genuine board" OTP check to pass on the clone) |
| **Motor** | ACT 42BLF03 — 42mm BLDC, 24V, 78W, **continuous 3.25A** / peak 18A, 4000 RPM, **hall sensors only** (no encoder) |
| **Belt ratio** | ~3.8:1 motor:handwheel (approx, but belt slips — no reliable SPM calibration without a sensor) |
| **Brake resistor** | 2Ω (as delivered with board) |
| **PSU** | 24V / 8.3A (~200W) |
| **Machine** | Pfaff 130 sewing machine, belt-driven on original mount. Mechanical clutch present but only used for maintenance (motor drives belt directly during stitching) |
| **Future input** | Custom foot pedal (reflex optocoupler, 0–3.3V / 0–5V) with custom response curve |

### Key Technical Facts (verified from firmware)

**Hall sensor resolution:** In `ENCODER_MODE_HALL`, ODrive produces **6 discrete states per
electrical revolution** (60° electrical each). Effective encoder CPR = `6 × pole_pairs`.

**Pole count resolved:** The saved config has `motor.config.pole_pairs = 8` and
`encoder.config.cpr = 48`. Since 6 × 8 = 48, this is consistent. The ACT 42BLF03
datasheet says "8 poles" but the working config uses 8 pole pairs. The motor
effectively has **8 pole pairs (16 physical poles), CPR = 48.**

**Low-speed hall behavior:** Position is tracked by a PLL that interpolates between the
48 hall edges using `vel_estimate`. At very low speed the estimator jitters and the code
even has a `snap_to_zero_vel` branch that forces velocity to exactly 0 below a threshold.
This causes the **vibration and cogging** the user observes.

**Feed-forward support in ODrive:**
- `controller.config.inertia` [N·m/(turn/s²)] — used by `INPUT_MODE_VEL_RAMP`, `TRAP_TRAJ`, `POS_FILTER`
- `input_vel` in position mode = velocity feed-forward; `input_torque` in velocity/position mode = torque feed-forward
- **There is NO built-in friction feed-forward parameter.** Friction compensation must be applied as a torque offset (to `input_torque`).

**Current & power management:** The PSU is 24V/8.3A but the controller has capacitors
that decouple supply current from motor coil current. `dc_max_positive_current = 8.3`
(the bus limit) is the real supply-side protection. Motor current (`current_lim`)
is a different thing. The PSU has circuit protection — the goal is to not trigger it.
The motor has NO temp sensor — the continuous current rating is its thermal protection,
so keeping `current_lim` sensible is important. The GUI should monitor `dc_bus_voltage`,
`ibus`, FET temp, and `dc_max_positive_current` rather than a naive current sum.

---

## Current State (August 2025)

### What Exists

A single-file PySide6 GUI (`main.py`, ~900 lines) focused on **Axis 0 velocity control**:

| Area | Capabilities |
|------|-------------|
| **Connection** | Auto-connect on startup, background thread discovery, auto-reconnect via `_on_lost` callback + fallback read-failure counter |
| **Control** | Velocity/Position/Torque control mode switching, setpoint spinboxes, Run/Stop buttons |
| **Calibration/States** | Dropdown of all axis states, triggered by "Execute State" button (not on selection) |
| **Monitoring** | 100ms polling: VBus voltage, motor current, velocity estimate, position estimate, axis error |
| **Device Actions** | Save config to NVM, Export/Import JSON config, Reboot, Clear Errors, Dump Errors (console) |
| **Debug** | Verbose logging toggle, Force Reconnect, Device Info dialog, Read Failure counter dialog |

### Strengths

1. Clean architecture, documented threading model
2. Safe state execution (dropdown + button, no accidental calibration)
3. Robust reconnection (primary `_on_lost` + fallback read-failure counter)
4. Thread-safe (all Qt via `QTimer.singleShot`)
5. Well-documented (ARCHITECTURE.md, README.md)

### Weaknesses / Gaps

1. No calibration workflow — dropdown with states, no guidance, no progress, no validation
2. No control tuning — no access to PID gains, current limits, velocity limits, input modes
3. Error display is minimal — single raw integer, no sub-error decoding
4. No Hall sensor awareness — no pole-count config, no low-speed handling
5. No monitoring history — readings displayed but not recorded or plotted
6. No analog input support — no foot pedal mapping yet
7. No feed-forward calibration (inertia/friction measurement)

---

## Target Module Structure

The user prefers **a few deep modules** rather than many shallow ones:

```
QtGUI/
├── main.py              # App entry, main window, connection, menu bar, readings
├── calibration.py       # Calibration wizard + inertia/friction measurement tests
├── controls.py          # Control settings: gains, limits, input mode, feed-forward
├── monitoring.py        # Error display, live plotting, data logging
├── Plan.md
└── ARCHITECTURE.md
```

---

## Proposed Feature Roadmap



### Phase 1: Control Settings (NOW — HIGHEST PRIORITY)

**Goal:** Expose tuning parameters and input mode selection so you can tweak
and observe the effects immediately. "Before we change controls, we need to have
the monitoring working" — this phase gives you the knobs + the error display to
see what happens.

#### 1.1 Gains & Limits Panel (`QtGUI/controls.py`)

Live-editable spinboxes (read current device value on connect):

| Parameter | Attribute | Units | Notes |
|-----------|-----------|-------|-------|
| Velocity gain | `controller.config.vel_gain` | N·m/(turn/s) | Main velocity tuning |
| Velocity integrator | `controller.config.vel_integrator_gain` | N·m/(turn/s·s) | Removes steady-state error |
| Velocity integrator limit | `controller.config.vel_integrator_limit` | N·m | Cap windup |
| Position gain | `controller.config.pos_gain` | (turn/s)/turn | Position mode only |
| Current limit | `motor.config.current_lim` | A | Between 3.25A cont. and 18A peak |
| Current limit margin | `motor.config.current_lim_margin` | A | |
| Velocity limit | `controller.config.vel_limit` | turn/s | Safety cap |
| Enable torque-mode vel limit | `controller.config.enable_torque_mode_vel_limit` | bool | Safety for torque drive |
| Gain scheduling | `controller.config.enable_gain_scheduling` | bool | "Anti-hunt", reduces gains by position error |
| Inertia (feed-forward) | `controller.config.inertia` | N·m/(turn/s²) | From dynamics test (later) |

#### 1.2 Input Mode Selection

| Mode | Use Case |
|------|----------|
| `PASSTHROUGH` | Direct setpoint |
| **`VEL_RAMP`** | **Recommended for sewing machine** — smooth ramps, uses `vel_ramp_rate` + `inertia` feed-forward |
| `TRAP_TRAJ`, `POS_FILTER` | Position mode (future needle positioning) |
| `TORQUE_RAMP` | Smooth torque ramps |

For the sewing machine, `INPUT_MODE_VEL_RAMP` + measured `inertia` is the ideal combo —
it naturally smooths hall-sensor velocity jitter and gives smooth start/stop.

⚠ **Current config has `input_mode = 6 (POS_FILTER)` with `control_mode = 1 (VELOCITY)`**
— POS_FILTER is only valid for POSITION_CONTROL. This is a leftover from position-mode
testing and should be corrected to `VEL_RAMP` or `PASSTHROUGH`. See Known Flaws.

#### Implementation Details
- `QtGUI/controls.py` (~300–400 lines): `GainsPanel`, `InputModeSelector`
- Integrate into `main.py`: collapsible "Control Settings" group box below velocity control

---

### Phase 2: Error Display & Config Browser (NOW — HIGH PRIORITY)

**Goal:** Replace the single raw integer with decoded, actionable error info,
and add a full config tree so you can see what the device is doing.

#### 2.1 Error Decoding (`QtGUI/monitoring.py`)

Reuse `odrive.utils.dump_errors()` logic but return structured data:

```python
ErrorReport:
  system: ODriveError
  axis0:
    axis, motor, encoder, controller, sensorless  # each: bitmask → decoded names
```

#### 2.2 Error Panel

- Each error source on its own line, color-coded (green/red/yellow)
- Decoded name + short description + hint
- Clear button

#### 2.3 Error History

- Time-stamped deque (max ~1000), accessible via Device menu
- Export to text file

#### 2.4 Config Browser (Read-Only)

`QDialog` + `QTreeWidget` walking the ODrive object tree recursively — full config view
without `odrivetool`. Useful for seeing the entire device state at a glance.

#### Implementation Details
- Error logic in `QtGUI/monitoring.py` (~200 lines)
- Config Browser in `QtGUI/controls.py` (~100 lines)
- Integrate into `main.py`: replace the single error label with the error panel;
  add "Config Browser…" to the Device menu

---

### Phase 3: Monitoring & Plotting (NEXT)

**Goal:** Live visual feedback for tuning, plus data logging for analysis.

#### 3.1 Live Plot
- `pyqtgraph` line chart; channels: velocity, position, current, VBus, setpoint
- Selectable time window; pause/resume

#### 3.2 Data Logging
- CSV logging at 100ms; toggle via File menu; timestamped filenames

---

### Phase 4: Torque Drive + Calibration + Dynamics (NEXT)

**Goal:** Low-speed torque drive experiment, then the full calibration workflow,
step response tuning, and dynamics measurement.

#### 4.1 Low-Speed Torque-Drive Support

The user's insight: at low speed, **torque drive** (commanding current, relying on the
fast robust current loop) avoids the velocity PID fighting the jittery hall velocity estimate.

Expose a **"Low-speed torque mode"** preset that:
- Sets `control_mode = TORQUE_CONTROL`
- Enables `enable_torque_mode_vel_limit` + sets `vel_limit` (safety — prevent run-away)
- Applies friction-compensation torque offset
- Lets the user command torque directly (and later map the pedal to torque)

#### 4.2 Calibration Wizard Structure (`QtGUI/calibration.py`)

A multi-page `QDialog` (via `QStackedWidget`):

| Step | Action |
|------|--------|
| **0. Pre-check** | ⚠ **Warn:** disengage the handwheel clutch before calibration. Belt load causes calibration to fail (motor jumps over electrical revs). |
| **1. Motor Profile** | Load ACT 42BLF03 preset: `pole_pairs = 8`, `motor_type = HIGH_CURRENT`, `current_lim = 5.0`, phase R/L from current config. Fields editable. |
| **2. Encoder Config** | Set `encoder.config.mode = ENCODER_MODE_HALL`, CPR = `6 × pole_pairs = 48`, `ignore_illegal_hall_state` (optional) |
| **3. Motor Cal** | Run motor calibration, poll state, validate result |
| **4. Hall Phase Cal** | Run `ENCODER_HALL_PHASE_CALIBRATION` (spins motor in open loop, records hall edge phases) |
| **5. Hall Polarity Cal** | Run `ENCODER_HALL_POLARITY_CALIBRATION` |
| **6. Encoder Offset Cal** | Run `ENCODER_OFFSET_CALIBRATION` |
| **7. Summary** | Pass/fail per step, decoded errors, prompt to save config |

**Calibration current:** The wizard pre-fills `calibration_current = 4.0` (from the
working config). If the motor jumps electrical revs, the wizard suggests increasing
this value. The pre-check already warns about belt load.

**Pole-count verification:** CPR = 6 × 8 = 48 is hardcoded in the ACT 42BLF03 preset.
No verification needed — the config already proves this is correct.

#### 4.3 Progress Tracking

- Poll `axis.current_state` + error registers at 50ms during each calibration step
- Show current step, elapsed time, spinner ✓/✗ status
- Auto-detect return to `IDLE` (complete)
- On error: stop, show decoded error + hint

#### 4.4 Step Response Test (Tuning Aid)

A step change in setpoint while recording the system's response. Not a dashboard
feature — it's a **tuning tool** to measure overshoot, settling time, and
steady-state error. Helps determine if `vel_gain` / `vel_integrator_gain` are
correct. Lives in the calibration/tuning section.

- Uses `odrive.utils.step_and_plot()` or a custom implementation
- Plots setpoint vs. actual response
- Shows numeric metrics (overshoot %, settling time, steady-state error)

#### 4.5 Inertia & Friction Measurement Tests

A separate "Measure Dynamics" section (each step repeated several times to average
out noise):

1. **Break-away torque (static friction)** — minimum torque to start motion from rest.
   ⚠ **Measured ~6× larger than running friction** (user's manual test). Stiction is
   always higher than Coulomb friction (Stribeck curve).
2. **Running friction (Coulomb)** — torque needed to run steadily at ~1 rev/s.
3. **Inertia** — accelerate from 1 → 10 rev/s, compute `J = τ / α` from commanded
   torque vs. measured acceleration.

Each test runs multiple repetitions; the UI shows **mean ± standard deviation**.
Results stored in the GUI motor profile as **separate values** — break-away and
running friction must NOT be conflated (6× difference).

**How results are used:**
- `inertia` → written to `controller.config.inertia` (used by `VEL_RAMP`/`TRAP_TRAJ` feed-forward)
- `break_away_friction` → no direct ODrive parameter; used for the **start-up torque
  pulse** in the pedal mapping: a brief torque ramp/pulse to overcome stiction, then scale
  back to running friction once motion is detected.
- `running_friction` → **[FIRMWARE_CHANGE] torque offset** (feed-forward and add to
  `input_torque` - in all control modes). This compensates friction so the motor actually
  starts at low pedal positions; it will also keep the control error down in all modes.
- **Stiction implication:** a plain PID velocity controller must wind up its integrator
  to overcome stiction, causing delay + overshoot. This is another reason the
  **torque-drive at low speed** approach matters — you can command the break-away
  torque directly, then back off to running friction. This is exactly the pedal
  behavior a sewing machine needs (press pedal → firm start → smooth sewing).

#### Implementation Details
- `QtGUI/calibration.py` (~500–700 lines):
  - `CalibrationWizard(QDialog)` — multi-page wizard
  - `MeasureDynamicsDialog(QDialog)` — inertia/friction tests with stats
  - `StepResponseTest(QDialog)` — step test with plot and metrics
  - `CalibrationRunner` — background polling worker
  - `MotorProfile` — dataclass (JSON serializable), ACT 42BLF03 preset built in
  - `DynamicsResult` — mean/std values
- `QtGUI/controls.py`: `LowSpeedTorquePreset` (moved here with the torque-drive experiment)
- Integrate into `main.py`: replace state combo + Execute button with "Calibrate Motor…"
  and "Measure Dynamics…" buttons

---

### Phase 5: Foot Pedal Input (FUTURE)

**Goal:** Replace the GUI setpoint with the foot pedal analog input.

#### 5.1 Analog Input
- Configure a GPIO as `GPIO_MODE_ANALOG_IN`, read ADC value
- Map voltage → setpoint with a configurable curve

#### 5.2 Response Curve
- **Custom nonlinear curve** for better low-speed control (user's requirement)
- Dead zone at pedal-bottom, friction-compensation offset so low pedal positions
  actually move the machine
- Two mapping modes:
  - **Velocity mode**: pedal → `input_vel` (standard sewing behavior)
  - **Torque mode**: pedal → `input_torque` (+ friction offset + velocity limit)
    for smoother low speed, with break-away torque pulse at start
- Later: SPM (stitches/min) based mapping using the belt ratio

#### 5.3 UI
- Pedal position bar/slider, curve editor (drag points or polynomial)
- Display handwheel speed (motor rps ÷ belt ratio) as secondary readout

#### Implementation Details
- New section in `QtGUI/controls.py` — pedal mapping + curve editor
- May require firmware mod for custom curve on-device; GUI-side mapping is the first step

---

## Dependencies

| Feature | Dependency | Purpose |
|---------|-----------|---------|
| Core | PySide6 | Qt bindings |
| Core | odrive | Device communication |
| Phase 4 | pyqtgraph | Live plotting |
| All others | Python stdlib | None |

---

## Current Device Configuration (Known Good Baseline)

From the user's saved config (`sew_config` — Axis 0, sewing machine motor):

| Parameter | Value | Notes |
|-----------|-------|-------|
| `motor.config.pole_pairs` | **8** | 8 pole pairs → 48 hall counts/rev ✓ |
| `motor.config.motor_type` | 0 (HIGH_CURRENT) | |
| `motor.config.current_lim` | 5.0 A | Continuous rating |
| `motor.config.current_lim_margin` | 8.0 A | Errors at 13 A |
| `motor.config.requested_current_range` | 12.0 A | Current sensor range |
| `motor.config.calibration_current` | 4.0 A | |
| `motor.config.torque_constant` | 0.036 N·m/A | |
| `motor.config.torque_lim` | 0.7 N·m | |
| `motor.config.phase_resistance` | 0.348 Ω | Measured by calibration |
| `motor.config.phase_inductance` | 0.140 mH | Measured by calibration |
| `motor.config.pre_calibrated` | true | |
| `encoder.config.mode` | 1 (HALL) | |
| `encoder.config.cpr` | 48 | 6 × 8 pole pairs |
| `encoder.config.hall_polarity_calibrated` | true | |
| `encoder.config.pre_calibrated` | true | |
| `encoder.config.phase_offset` | 101 | |
| `encoder.config.phase_offset_float` | 0.677 | |
| `encoder.config.direction` | 1 | |
| `encoder.config.bandwidth` | 100 | PLL bandwidth |
| `controller.config.control_mode` | 1 (VELOCITY) | |
| `controller.config.input_mode` | **6 (POS_FILTER)** | ⚠ See flaw below |
| `controller.config.vel_gain` | 0.0346 N·m/(turn/s) | |
| `controller.config.vel_integrator_gain` | 0.173 N·m/(turn/s·s) | |
| `controller.config.vel_integrator_limit` | 10.0 N·m | |
| `controller.config.vel_limit` | 70.0 turn/s | Motor max ≈ 66.7 (4000 RPM) |
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
| `fet_thermistor.config.enabled` | true | Controller board temp monitored |

### Resolved Questions

1. **Pole count** — resolved: `pole_pairs = 8`, `cpr = 48`. The datasheet's "8 poles"
   is misleading or refers to a different variant. The working config is correct.
2. **Belt ratio** — dropped. No value without a sensor, belt slips so errors accumulate.
   SPM display will not be attempted.
3. **PSU current** — resolved. Supply current ≠ motor current (capacitors decouple).
   The real protections are `dc_max_positive_current` and FET/motor temperature.
4. **Calibration with belt** — confirmed: does NOT work (current too low, motor jumps
   electrical revs). Wizard must include a pre-check to disengage the clutch.
5. **Control mode testing** — confirmed: user wants to switch modes in the UI and
   monitor parameters with a plot.

### Open Design Questions

1. **Which control mode at low speed** — velocity (with VEL_RAMP + inertia) vs.
   torque drive (with friction offset + vel limit). The plan supports both;
   user wants to test and compare in the UI.

### Known Flaws / Observations in Current Setup

These are issues found while reviewing the saved config and firmware. They are
NOT blocking — the GUI should surface and help manage them.

1. **`input_mode = POS_FILTER (6)` with `control_mode = VELOCITY (1)`** — inconsistent.
   POS_FILTER is for position control only. A leftover from testing — the user
   confirmed **VEL_RAMP is the intended mode** (just wasn't saved in the last config).
2. **Motor thermistor disabled** — `motor_thermistor.config.enabled = false`.
   Expected: no temp sensor on the motor. The 3.25A continuous rating is the
   thermal protection. The GUI should display continuous vs. peak rating clearly.
3. **`vel_limit = 70` > motor max (66.7 turn/s)** — harmless. May later be useful
   for a "slow operation mode" or testing control modes at e.g. 10 rev/s.
4. **`current_lim = 5.0` is a compromise** between 3.25A continuous and 18A peak.
   The GUI should show both continuous and peak ratings and clarify that current_lim
   sits between them.
5. **Calibration with belt attached fails** (confirmed by user). The wizard's
   pre-check (step 0) handles this. Clutch disengagement is the manual step.
6. **Belt slip** — the belt slips occasionally, so any position-based measurement
   (belt ratio, SPM) is unreliable without a sensor. Dropped.

---

## Summary

```
Phase 1: Control Settings + Input Mode  ───  NOW  ───  Knobs first, see effects
Phase 2: Error Display + Config Browser  ───  NOW  ───  Monitoring + full config view
Phase 3: Live Plot + Data Logging        ─── NEXT  ───  Visual feedback + recording
Phase 4: Torque Drive + Cal + Step + Dyn ─── NEXT  ───  Experiments + calibration + tuning
Phase 5: Foot Pedal                      ─── LATER ───  Control without GUI
```

---

*Last updated: August 2025*
