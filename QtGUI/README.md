# ODrive Qt GUI

A PySide6 (Qt Quick/QML) graphical interface for ODrive, focused on **Axis 0 velocity control**.

## Features

- **Velocity control** as primary mode (with position & torque modes available)
- Real-time monitoring:
  - VBus voltage & power draw
  - Velocity estimate (rps)
  - Position estimate (rev)
  - Axis errors (decoded)
- Quick start/stop buttons
- Live setpoints (editable, applied on **Apply** / Enter — no implicit writes)
- All calibration states available when needed
- Reusable, feature-gated settings (electrical / mechanical / control parameters)
- Live event log (works even while disconnected)

## Requirements

- Python 3.8+
- ODrive v3.x device
- PySide6
- odrive Python library (the one bundled in `tools/odrive/` — `main.py` adds
  it to `sys.path` automatically, so no separate install is required)

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
python main.py
```

The GUI auto-connects to the first ODrive it finds. The **status footer** (bottom
status bar) shows the connection state and the app keeps retrying / auto-reconnects if
the device is unplugged. While searching, the footer shows "Offline (retrying)" until a
device appears.

1. Select your **Control Mode** (Velocity / Position / Torque) and **Input Mode**.
2. Set the setpoint (editable field / arrows) and click **▶ Run** — the setpoint is sent
   to the device on **Apply** / Enter, **not** while you type.
3. Stop with **■ Stop** (goes to Idle).
4. Use the **Program** dropdown and **Start** to run a calibration/axis state (states are
   *not* executed just by selecting them).
5. Tune gains/limits in the **Settings** tabs (Electrical Limits · Mechanical Limits ·
   Control Parameters); rows the firmware doesn't expose are disabled.

### Menus

- **Device** — Save Config, Export/Import Config, Reboot, Errors (decoded, with Clear),
  Device Info.
- **Debug** — Verbose Logging, Event Log…, Force Reconnect.

The Errors dialog is also opened by clicking the `Err:` field in the status footer. The
Error, Device Info, and Event Log dialogs are separate movable windows.

### Debugging

- **Debug ▸ Verbose Logging** — enables DEBUG-level output to the console
  (connect attempts, read failures, thread transitions).
- **Debug ▸ Force Reconnect** — drops the current connection and reconnects.
- **Debug ▸ Event Log…** — chronological log of connect/state/mode/setpoint/error events;
  exportable, and works while disconnected.
- **Device ▸ Device Info** — serial number, firmware/hardware version.
- **Device ▸ Errors** — decoded per-module error bits with a Clear Errors button.

## Tests

A headless pytest suite (mock ODrive, no hardware/display) lives in `tests/` and runs as
part of `./check.sh` (with `QT_QPA_PLATFORM=offscreen`):

```bash
python -m pytest tests/
```
