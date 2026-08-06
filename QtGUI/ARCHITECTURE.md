# Qt GUI Architecture

A lightweight PySide6 desktop GUI for monitoring and controlling a single ODrive axis.  
Designed for **velocity control** as the primary use case, with position and torque modes available.

## Design Principles

These project-wide rules must hold for every feature; implementation specifics are documented below and in the relevant code sections.

- **The GUI is a monitor / settings interface, not a controller.** It never performs realtime control and never should be required to. Safety (limits, error-stops, velocity/current limiting) is enforced by the **firmware + controller**.
- The UI may write a setpoint or select a mode, but **does not drive or supervise** the control loop. Once a speed is set, **the motor keeps running independently** of the GUI.
- **Closing the GUI, reconnecting, or losing connection never stops the motor.** The GUI commands the device only through explicit user actions: **Run (Closed Loop)**, **Stop (Idle)**, and **Execute State**.
- Connect/disconnect transitions only tear down GUI references — there are **no implicit writes** to `requested_state`.

## Architecture

```
QtGUI/main.py
  │
  ├── controls.py                     # Phase 1: Control Settings
  │     ├── ControlParamsGroup(QGroupBox)  # velocities/integrator/pos gains, inertia
  │     ├── LimitsTabs(QTabWidget)         # Electrical + Mechanical limit tabs
  │     └── InputModeSelector(QComboBox)
  │
  ├── ODriveGUI(QMainWindow)          # Main window, owns all UI & state
  │     ├── setup_ui()                # Layout: Connection, Control, Calibration, Device, Readings
  │     ├── connect_odrive()          # Background thread → odrive.find_any()
  │     ├── _connect_worker()         # Daemon thread, delivers result via QTimer.singleShot
  │     ├── _on_connected()           # Wire up axis, register _on_lost callback
  │     ├── _on_device_lost()         # Library notification → auto-reconnect
  │     ├── sync_ui_from_controller() # Poll controller.control_mode → update combo + spinboxes
  │     ├── update_readings()         # 100ms timer: read all values, detect disconnect
  │     └── closeEvent()              # Stop poll timer (UI-only; motor keeps running)
  │
  ├── Module-level constants
  │     ├── MODE_NAMES / MODE_VALUES  # control_mode enum ↔ display name
  │     ├── STATE_MAP                 # axis state enum lookup (avoids globals())
  │     └── RECONNECT_*               # Fallback thresholds
  │
  └── ODrive library (tools/odrive/)
        ├── find_any()                # Blocking device discovery
        ├── obj._on_lost              # Disconnect notification (future callback)
        ├── save_configuration()      # Persist to NVM
        ├── backup_config()           # Export to JSON file
        ├── restore_config()          # Import from JSON file
        ├── reboot()                  # Device reboot + auto-reconnect
        └── axis0.{motor, encoder, controller}  # Real-time interface
```

## Connection Lifecycle

```
┌──────────────────────────────────────────────────────────────────┐
│  App start                                                       │
│    └─ QTimer.singleShot(500ms) → connect_odrive()               │
│         └─ threading.Thread(target=_connect_worker)               │
│              └─ odrive.find_any()  ← blocks until device found   │
│                   ├─ success → _on_connected(odrv)               │
│                   │    ├─ self.odrive = odrv                     │
│                   │    ├─ self.axis = odrv.axis0                 │
│                   │    ├─ register odrv._on_lost callback        │
│                   │    └─ enable controls                        │
│                   │                                              │
│                   └─ failure → _on_connect_failed(msg)           │
│                        └─ auto-reconnect?                        │
│                             ├─ yes → QTimer.singleShot(1s,       │
│                             │        connect_odrive)             │
│                             └─ no  → popup error, disable        │
│                                                                  │
│  Device disconnected (USB unplug)                                │
│    ├─ PRIMARY: odrv._on_lost fires (library discovery thread)    │
│    │    └─ QTimer.singleShot(0, connect_odrive)                  │
│    └─ FALLBACK: update_readings sees 5× consecutive failures     │
│         └─ connect_odrive()                                      │
│                                                                  │
│  Device reboot (user or import)                                  │
│    └─ _on_lost → auto-reconnect → re-connected                   │
└──────────────────────────────────────────────────────────────────┘
```

## UI Layout

```
┌──────────────────────────────────────────────────┐
│  File  Device ▼  Debug ▼                          │  ← menu bar
├──────────────────────────────────────────────────┤
│  [▶ Run (Closed Loop)]  [■ Stop (Idle)]  State: [AXIS_STATE_IDLE ▾]  [Execute State] │
├──────────────────────────────────────────────────┤
│  Velocity Control                                 │
│  Control Mode: [Velocity Control ▾]               │
│  Velocity Setpoint (rps): [   0.000   ▲▼ ]        │
│  Torque Setpoint (A): [   0.000   ▲▼ ]  ← hidden │
│  Position Setpoint (rev): [   0.0000  ▲▼ ]← hidden│
├──────────────────────────────────────────────────┤
│  ☑ Control Settings  (Phase 1, collapsible)       │
│  Input Mode: [Velocity Ramp (2) — recommended ▾]  │
│  ▍ Control Parameters  (vel_gain, integrators, inertia) │
│   [Vel gain (N·m/(t/s)) ___]  [Vel int gain (N·m/t)___]│
│  ┌──────────────────────────────────────────────┐ │
│  │ [Electrical Limits | Mechanical Limits]      │ │
│  │  [Cur lim (A)___]  [Cur lim margin (A)___]  │ │
│  └──────────────────────────────────────────────┘ │
├──────────────────────────────────────────────────┤
│  Readings (monitoring)                            │
│  VBus Voltage: 24.50 V                            │
│  Motor Current: 0.12 A                            │
│  Velocity Estimate: 0.045 rps  ← bold            │
│  Position Estimate: 1.2345 rev                    │
│  Error: None                                      │
├──────────────────────────────────────────────────┤
│  [Ready...]                           ● Connected │  ← status bar
└──────────────────────────────────────────────────┘
```

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Plain `threading.Thread` instead of `QThread`** | `QThread` + `QObject::moveToThread` boilerplate is overkill for a single blocking call. A daemon thread + `QTimer.singleShot(0, ...)` to deliver the result to the main thread is simpler and equally safe. |
| **`_on_lost` callback as primary disconnect detection** | The odrive library's background discovery thread already monitors device health. Registering a done callback on `obj._on_lost` gives instant notification without polling. |
| **Read-failure fallback** | Catches edge cases where `_on_lost` doesn't fire (older firmware, stale object). 5 consecutive failures at 100ms ≈ 0.5 seconds. |
| **`MODE_NAMES` / `STATE_MAP` dicts** | Avoids fragile `globals()` lookups and repeated `if/elif` chains. Single source of truth for enum ↔ display name. |
| **`QTimer.singleShot(0, ...)` for thread crossing** | Qt's event loop is thread-safe for `QTimer.singleShot` — it posts a `QTimerEvent` to the main thread's event queue. No mutexes or custom signals needed. |
| **States execute only via button** | Selecting a state in the dropdown only changes the selection; the `Execute State` button is the sole trigger, so browsing the list can never accidentally start a calibration. |
| **`_on_lost.done()` guard** | If the device disconnects before the callback can be registered, `add_done_callback` would fire immediately in the connect thread. The `done()` check avoids this by scheduling a fresh reconnect instead. |
| **`hasattr` for remote methods** | Before calling `odrv.reboot()`, the code checks with `hasattr` so that firmware variants without the reboot endpoint get a clear error message instead of an `AttributeError`. |
| **Feature-gated control settings (Phase 1)** | `GainsPanel`/`InputModeSelector` read the device on `bind()` and disable any row the firmware doesn't expose (`hasattr` on `obj.config`), per Plan.md §4.2. Writes are wrapped and surfaced via a status callback; a `_syncing` guard suppresses write-backs during programmatic sync. |
| **Device section → menu bar** | Save/Export/Import/Reboot moved from a group box to a **Device** menu on the menu bar. This declutters the main area and follows standard desktop GUI conventions. The menu is disabled until connected, just like the control widgets. |
| **Connection status in the status bar footer** | The connection status label is a permanent widget on the `QStatusBar` instead of a dedicated group box. This keeps the main area focused on control and monitoring, while the footer always shows the connection state. Temporary action messages (save, export, etc.) appear via `showMessage()` on the left. |
| **Persistent status bar messages** | `showMessage(text, 0)` is used for connection progress ("Finding ODrive...", "Connected!") so they don't disappear after the default 3 s timeout. Action messages (save, export) still use the default transient timeout. |
| **Debug menu** | A Debug menu provides verbose logging toggle, force reconnect, and device info — useful for diagnosing connection issues without restarting the GUI. |
| **No connect/disconnect button** | Auto-connect on startup + auto-reconnect on loss makes a manual button redundant. The status bar shows the current state. |
| **UI never stops the motor (monitor-only)** | Per the general project rule, closing the window or reconnecting must not command the device. `closeEvent()` only stops the poll timer; reconnect only tears down GUI references. The motor is commanded **exclusively** via the explicit Run / Stop / Execute-State actions. |
| **Ctrl+C via `SIG_DFL`** | The Qt event loop is a blocking C++ call, so a Python `KeyboardInterrupt` is not serviced during `app.exec()` (a timer "nudge" is unreliable). Restoring the default SIGINT action (`signal.signal(SIGINT, SIG_DFL)`) makes Ctrl+C terminate the process at the OS level, reliably from any state (including while "finding device"). Safe because the GUI is monitor-only. |

## Threading Model

```
Main Thread (Qt event loop)          Background Thread (daemon)
─────────────────────────────        ────────────────────────────
update_readings()  ← 100ms timer
sync_ui_from_controller()
on_run_clicked(), on_stop_clicked()
on_mode_changed(), on_velocity_changed()
on_save_config(), on_export_config()
on_import_config(), on_reboot()
                                     _connect_worker()
                                       odrive.find_any()  ← blocking
                                         │
                      QTimer.singleShot(0, ...)  ────→  _on_connected()
                                         │
                      QTimer.singleShot(0, ...)  ────→  _on_connect_failed()
                                                               
odrv._on_lost.add_done_callback()
  │
  └─ discovery thread ──→ _on_device_lost()
                              │
          QTimer.singleShot(0, ...) ────→ connect_odrive()
```

## Adding a New Feature

1. **New control widget**: Add it to `setup_ui()` in the appropriate group box. Wire signals to a slot.
2. **New ODrive method call**: Call it on `self.odrive` (or `self.axis`/`self.controller`/`self.motor`/`self.encoder`). Wrap in `try/except` — the serial connection may drop.
3. **New reconnect trigger**: The `_on_device_lost` callback handles most cases. If your feature causes the device to disconnect (e.g., reboot), `_on_lost` will fire and auto-reconnect will re-establish the connection.
4. **New reading**: Add a label + update code in `update_readings()`.

## Dependencies

- **PySide6 ≥ 6.0** — Qt bindings
- **odrive** (local package in `tools/odrive/`) — device discovery, Fibre protocol, configuration helpers
- **Python ≥ 3.8** — f-strings, threading, `pathlib`-compatible