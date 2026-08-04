# ODrive Qt GUI

A PySide6-based graphical interface for ODrive, focused on **Axis 0 velocity control**.

## Features

- **Velocity control** as primary mode (with position & torque modes available)
- Real-time monitoring:
  - VBus voltage
  - Motor current
  - Velocity estimate (rps)
  - Position estimate (rev)
  - Axis errors
- Quick start/stop buttons
- All calibration states available when needed

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

The GUI auto-connects to the first ODrive it finds. The **status bar** (footer)
shows the connection state and the app keeps retrying / auto-reconnects if the
device is unplugged. While searching, the status bar shows
"Finding ODrive..." until a device appears.

1. Select your control mode (Velocity/Position/Torque)
2. Set the setpoint and click **▶ Run**
3. Stop with **■ Stop** (goes to Idle)
4. Use **Calibration & States** to pick a state and click **Execute State**
   (states are *not* executed just by selecting them from the dropdown)

### Debugging

- **Debug ▸ Verbose Logging** — enables DEBUG-level output to the console
  (connect attempts, read failures, thread transitions).
- **Debug ▸ Force Reconnect** — drops the current connection and reconnects.
- **Debug ▸ Device Info…** — shows serial number, firmware version, VBus and
  axis error state.
- **Debug ▸ Dump Read Failures…** — shows the internal read-failure counter
  and reconnect thresholds.
