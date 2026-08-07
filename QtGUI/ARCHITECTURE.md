# Qt GUI Architecture

A lightweight PySide6 desktop GUI for monitoring and controlling a single ODrive axis.  
Designed for **velocity control** as the primary use case, with position and torque modes available.

## Design Principles

These project-wide rules must hold for every feature; implementation specifics are documented below and in the relevant code sections.

- **The GUI is a monitor / settings interface, not a controller.** It never performs realtime control and never should be required to. Safety (limits, error-stops, velocity/current limiting) is enforced by the **firmware + controller**.
- The UI may write a setpoint or select a mode, but **does not drive or supervise** the control loop. Once a speed is set, **the motor keeps running independently** of the GUI.
- **Closing the GUI, reconnecting, or losing connection never stops the motor.** The GUI commands the device only through explicit user actions: **Run (Closed Loop)**, **Stop (Idle)**, and **Execute State**.
- Connect/disconnect transitions only tear down GUI references — there are **no implicit writes** to `requested_state`.
- **Setpoints are applied only on explicit confirmation** (Apply button or Enter key). Adjusting a setpoint field never commands the motor; only a confirmed apply sends it to the device.

## Architecture

```
QtGUI/main.py
  │
  ├── controls.py                     # Phase 1: Control Settings
  │     ├── SettingsTabs(QTabWidget)       # Electrical | Mechanical | Control Params
  │     └── InputModeSelector(QComboBox)
  ├── errors.py                       # Phase 2: Error decode + on-demand dialog
  │     ├── read_error_report(odrv, axis)  # structured decode (system/axis/motor/enc/ctl...)
  │     └── LogDialog(QDialog)              # event log (context + errors) + current errors, clear/export
  │                                       # opened via Device > Errors… or footer click
  ├── util.py                         # Shared helpers
  │     └── safe_getattr()                 # guarded nested getattr for device reads
  │
  ├── ruff.toml / check.sh            # Dev tooling: lint config + static-check runner
  │
  ├── ODriveGUI(QMainWindow)          # Main window, owns all UI & state
  │     ├── setup_ui()                # Layout: menus, Control Command, Control Settings
  │     ├── connect_odrive()          # Background thread → odrive.find_any()
  │     ├── _connect_worker()         # Daemon thread, delivers result via QTimer.singleShot
  │     ├── _on_connected()           # Store device; axis/motor/… are derived properties
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
│                   │    ├─ axis/motor/encoder/controller derived  │
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
│  [▶ Run (Closed Loop)]  [■ Stop (Idle)]  Programm: [AXIS_STATE_IDLE ▾]  [Start] │
├──────────────────────────────────────────────────┤
│  Control Command  (setpoints, enabled only in closed-loop) │
│  Control Mode: [Velocity Control ▾]               │
│  Velocity Setpoint (rps): [   0.000   ▲▼ ]  est: 0.045 rps│
│  Torque Setpoint (A): [   0.000   ▲▼ ]  ← hidden │
│  Position Setpoint (rev): [   0.0000  ▲▼ ] est: 1.23 rev│← hidden │
├──────────────────────────────────────────────────┤
│  Control Settings                      (Phase 1) │
│  [Electrical | Mechanical | Control Params]      │
│    tab rows: [Par (unit)___] [Par (unit)___]     │
├──────────────────────────────────────────────────┤
│  [Ready...]  ●Online  State:CLS_LOOP  Err:OK  24.5V  49.0W │  ← status bar
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
| **Feature-gated control settings (Phase 1)** | `SettingsTabs`/`InputModeSelector` read the device on `bind()` and disable any row the firmware doesn't expose (`hasattr` on `obj.config`), per Plan.md §4.2. Writes are wrapped and surfaced via a status callback; a `_syncing` guard suppresses write-backs during programmatic sync. |
| **Single source of truth for the device** | `self.odrive` is the only stored device reference; `axis0`/`motor`/`encoder`/`controller` are derived `safe_getattr`-backed read-only properties. This removes the fragile five-field state that could go stale or drift out of sync (e.g. during reconnect), and simplifies cleanup in `connect_odrive`/`_on_connected`. |
| **`safe_getattr` for device reads** | All optional attribute reads go through `safe_getattr(obj, *attrs, default=None)` — a guarded nested `getattr` that returns the default on a missing attribute or a raised remote read. It replaces scattered `try/except` blocks while `_read_value`/`_read_failed` still distinguish `ObjectLostError` for the reconnect fallback. |
| **Device section → menu bar** | Save/Export/Import/Reboot moved from a group box to a **Device** menu on the menu bar. This declutters the main area and follows standard desktop GUI conventions. The menu is disabled until connected, just like the control widgets. |
| **Connection status in the status bar footer** | The connection status is a permanent composed widget on the `QStatusBar` (connection indicator, error, bus voltage, power draw — Plan.md §4.7) instead of a dedicated group box. This keeps the main area focused on control and monitoring, while the footer always shows live state. Temporary action messages (save, export, etc.) appear via `showMessage()` on the left without duplicating the connection text. |
| **Persistent status bar messages** | `showMessage(text, 0)` is used for connection progress ("Finding ODrive...", "Connected!") so they don't disappear after the default 3 s timeout. Action messages (save, export) still use the default transient timeout. |
| **Debug menu** | A Debug menu provides a verbose logging toggle and force reconnect — useful for diagnosing connection issues without restarting the GUI. (Device Info lives in the **Device** menu.) |
| **No connect/disconnect button** | Auto-connect on startup + auto-reconnect on loss makes a manual button redundant. The status bar shows the current state. |
| **UI never stops the motor (monitor-only)** | Per the general project rule, closing the window or reconnecting must not command the device. `closeEvent()` only stops the poll timer; reconnect only tears down GUI references. The motor is commanded **exclusively** via the explicit Run / Stop / Execute-State actions. |
| **Ctrl+C via `SIG_DFL`** | The Qt event loop is a blocking C++ call, so a Python `KeyboardInterrupt` is not serviced during `app.exec()` (a timer "nudge" is unreliable). Restoring the default SIGINT action (`signal.signal(SIGINT, SIG_DFL)`) makes Ctrl+C terminate the process at the OS level, reliably from any state (including while "finding device"). Safe because the GUI is monitor-only. |
| **Errors as an on-demand log viewer** | To keep the window compact, errors are shown in an on-demand `LogDialog` opened from the Device > Errors… menu or by clicking the `Err:` indicator in the status footer (`_ClickableLabel`). It shows a time-stamped **event log** (connect/state/mode/setpoint/error/clear entries — so the run-up to an error has context) plus the current decoded errors, with clear/export. |
| **Confirmed setpoint apply** | The velocity/torque/position spinboxes do **not** write on change. The active setpoint is written to the device only on explicit confirmation — an "Apply Setpoint" button or the Enter key (`lineEdit().returnPressed`). Adjusting a field never moves the motor. |
| **Control Command gating + state in footer** | The renamed "Control Command" section keeps its mode combo usable when connected, but the setpoint/Apply inputs are enabled only while the axis is in `CLOSED_LOOP_CONTROL` (checked each 100 ms poll). The current axis state is always shown in the status footer (`AXIS_STATE_NAMES` reverse map), not only while running. |
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
2. **New ODrive method call**: Call it on `self.odrive` (or a derived property like `self.axis`/`self.controller`). Wrap in `try/except DEVICE_EXCEPTIONS` — the serial connection may drop; leave genuinely unknown errors uncaught so they surface with a stack trace.
3. **New reconnect trigger**: The `_on_device_lost` callback handles most cases. If your feature causes the device to disconnect (e.g., reboot), `_on_lost` will fire and auto-reconnect will re-establish the connection.
4. **New reading**: Add a label + update code in `update_readings()`.

## Git Commit Conventions

Commit titles follow **Conventional Commits** (`type(scope): summary`), and the message body uses a **title, then a blank line, then a short description**.

Format:
```
<type>(<scope>): <imperative summary>

<short description of what and why (wrapped ~72 cols)>
```

- **Title** (`type(scope): summary`): lowercase, imperative mood, ≈72 chars max. Keep the summary concise.
- **Blank line** after the title — it separates the subject from the body.
- **Body**: a short description covering *what* changed and *why* (not a line-by-line log).

Common `type`s:
| Type | Use |
|------|-----|
| `feat` | New feature / capability |
| `fix` | Bug fix |
| `refactor` | Code change that neither fixes a bug nor adds a feature |
| `docs` | Documentation only (Plan/ARCHITECTURE comments) |
| `test` | Adding/updating tests |
| `chore` | Tooling / maintenance (no product code) |
| `ui` | User-interface / widget / status-bar work |

Examples:
```
ui: composed status footer + closed-loop-gated commands

Rename the velocity section to "Control Command", gate its setpoints on
closed-loop control, and replace the status label with a composed footer.
```
```
fix: read device setpoint into display on closed-loop entry
```

Keep each commit focused on one concern; don't mix unrelated changes in a single commit.

## Static Checks (`check.sh`)

`./check.sh` runs the linters/type-checker against the QtGUI source (no install —
it assumes `ruff` and `mypy` are already on PATH):

- `ruff check .` — lint (uses `ruff.toml`; selects `E/F/I/UP/B/RUF/BLE` at a
  120-char line length for the tabular config tables).
- `mypy` on `main.py` / `controls.py` / `errors.py` / `util.py` — static typing.
- `python -m py_compile` — syntax sanity.

Optional formatting (not gated by `check.sh` — normalizes most of the codebase,
so it's opt-in): `ruff format .`

**Exception handling note:** device reads/writes catch only `DEVICE_EXCEPTIONS`
(`util.py`) — the expected, transient fibre/odrive failures (`ObjectLostError`,
`EOFError`, `TimeoutError`, `OSError`, `asyncio.CancelledError`). A bare generic
`Exception` from libfibre ("internal error", "peer misbehaving", "unknown
error") is treated as a bug and deliberately left uncaught so it surfaces with a
stack trace rather than being silently swallowed.

## Dependencies

- **PySide6 ≥ 6.0** — Qt bindings
- **odrive** (local package in `tools/odrive/`) — device discovery, Fibre protocol, configuration helpers
- **Python ≥ 3.8** — f-strings, threading, `pathlib`-compatible