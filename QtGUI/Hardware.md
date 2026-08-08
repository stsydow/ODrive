# Hardware & Device Reference

The machine, motor, firmware, and known-good device configuration for the
target ODrive setup. It is the hardware backdrop for the Qt GUI — and is
**reference context only**: a documentation of one known-good configuration for
feasibility checks, **never an assumption about the live device**. All
parameter values are read from the connected device at runtime, never
hard-coded.

---

## 1. Machine

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

## 2. Hall Sensor Resolution (verified from firmware)

In `ENCODER_MODE_HALL`, ODrive produces **6 discrete states per electrical
revolution** (60° electrical each). Effective encoder CPR = `6 × pole_pairs`.

The working config has `motor.config.pole_pairs = 8` and `encoder.config.cpr = 48`
(6 × 8 = 48). The ACT 42BLF03 datasheet lists "8 poles" — this corresponds to
**8 pole pairs (16 physical poles)** in the ODrive config, which is correct.

**Low-speed behavior:** The PLL interpolates between hall edges using `vel_estimate`.
Below a threshold, the `snap_to_zero_vel` branch forces velocity to exactly 0,
causing the vibration and cogging observed at low speed. This motivates the
low-speed torque-drive work (see the GUI feature plan).

## 3. Known Good Configuration Baseline

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

---

## 4. Annex A: Full Device Configuration (Known Good Baseline)

From the user's saved config (`sew_config` — Axis 0, sewing machine motor).

**This table is reference context only.** It documents one known-good
configuration, used for feasibility checks and as a write-back example. It is
**not** assumed to be the live device state, and every feature must work with a
different motor/PSU — values are read from the connected device at runtime,
never hard-coded.

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

## 5. Annex B: Motor Datasheet — 42BLF Series

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
