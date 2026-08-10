# Qt GUI Architecture

A lightweight PySide6 desktop GUI for monitoring and controlling a single ODrive axis
(velocity-focused, with position and torque modes). Device/motor specifics live in
`Hardware.md`. This file documents how the code is put together and why. When in doubt,
the code is the source of truth — this doc is the durable why, not a line-by-line log.

## Design Principles

Project-wide rules that must hold for every feature (implementation specifics in the
code and below).

- **The GUI is a monitor / settings interface, not a controller.** It never performs
  realtime control and never should be required to. Safety (limits, error-stops,
  velocity/current limiting) is **enforced by the firmware** running on the controller.
  The UI shall write setpoints and select a modes, but does not drive or supervise the
  control loop. Once a speed is set, the motor keeps running independently of the GUI.
- **The device state is the source of truth** and closing the GUI, reconnecting, 
  or losing connection may happen but is not a big deal and never stops the motor.
  The GUI reads the state on connect and preiodically to reflect the device state.
  It commands the device only through explicit user actions.
  Connect/disconnect transitions only tear down GUI references — there are **no implicit writes**.
- **Setpoint and parameter changes are applied only on explicit confirmation** (Apply button or Enter key).
  Adjusting a setpoint field never commands the motor; only a confirmed apply sends it.

## Module Layout

```
QtGUI/
├── main.py        # App entry, main window (ODriveGUI), menus, connection, 100ms poll
├── controls.py    # Control Settings (SettingsTabs) + InputModeSelector
├── errors.py      # Error decode (read_error_report) + current-errors dialog
├── eventlog.py    # In-memory event log + viewer (LogEntry/LogDialog)
├── util.py        # safe_getattr() guarded reads + DEVICE_EXCEPTIONS
├── ruff.toml / check.sh   # lint config + static-check runner
├── Hardware.md            # machine / motor / device-config reference
└── ARCHITECTURE.md        # ← this file
```

`ODriveGUI(QMainWindow)` owns all UI and state. The device root (`self.odrive`) is the
single source of truth; `axis`/`motor`/`encoder`/`controller` are derived read-only
properties (`safe_getattr`-backed) so they can never go stale or drift out of sync.

## Connection Lifecycle

```
App start ─ QTimer.singleShot(500ms) ─▶ connect_odrive()
                └─ threading.Thread(_connect_worker) ─▶ odrive.find_any()  (blocks)
                     ├─ success ─▶ _on_connected(): store odrive, register _on_lost,
                     │              bind controls, enable UI
                     └─ failure  ─▶ _on_connect_failed(): retry after 1s

Device lost (USB unplug / reboot)
  └─ PRIMARY:  odrv._on_lost fires (library discovery thread) ─▶ connect_odrive()
       └─ if reads raise ObjectLostError in update_readings but _on_lost never
          fired (a library bug): caught centrally, logged with traceback, polling
          stopped and reconnect attempted — so the miss is visible, not silent.
```

All worker→UI crossings go through `QTimer.singleShot(0, ...)` (thread-safe post to the
Qt event loop — no mutexes or custom signals).

## Threading Model

| Thread | Work |
|--------|------|
| **Main (Qt event loop)** | All UI: poll timer (`update_readings`), control/state/mode handlers, menus |
| **Background daemon** | `_connect_worker` → blocking `odrive.find_any()`; result delivered via `QTimer.singleShot(0, ...)` |
| **Library discovery thread** | `odrv._on_lost` → `_on_device_lost()` → `QTimer.singleShot(0, connect_odrive)` |

Qt widgets are only ever touched from the main thread.

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Single source of truth** | `self.odrive` is the only stored device ref; axis/motor/encoder/controller are `safe_getattr`-backed properties — removes a five-field state that could drift/stale across reconnects. |
| **`threading.Thread` + `QTimer.singleShot(0,...)`** | A plain daemon thread plus a posted timer beats `QThread`/`moveToThread` boilerplate for one blocking call, and is equally safe (Qt is thread-safe for `singleShot`). |
| **`_on_lost` primary, read-failure fallback** | `_on_lost` gives instant disconnect notification; a 5×-consecutive-read-failure counter (≈0.5s) catches cases where it doesn't fire. |
| **`safe_getattr` + `_read_value`/`_read_failed`** | Centralize reads so a transient failure logs once and returns a default; only `ObjectLostError` drives the reconnect counter. |
| **Catch only `DEVICE_EXCEPTIONS`** | The expected, transient fibre/odrive failures (`ObjectLostError`, `EOFError`, `TimeoutError`, `OSError`, `CancelledError`). A bare generic libfibre `Exception` is a bug and is left uncaught so it surfaces with a stack trace. |
| **`hasattr` feature gating** | Disable any row/action the attached firmware doesn't expose (e.g. `reboot()`, `exit` missing) instead of assuming a version string. |
| **`MODE_NAMES` / `STATE_MAP` dicts** | Enum ↔ display-name maps instead of fragile `globals()` lookups or `if/elif` chains. |
| **`_on_lost.done()` guard on connect** | If the device dropped before the callback is registered, schedule a fresh reconnect rather than letting `add_done_callback` fire in the connect thread. |
| **States execute only via button** | Selecting a dropdown item just sets the selection; only Start/Execute actually commands the device — browsing can't accidentally start a calibration. |
| **Confirmed setpoint apply** | Spinboxes never write on change; the active setpoint goes to the device only on Apply / Enter. |
| **Control Command gating** | Setpoint inputs enabled only while the axis is in `CLOSED_LOOP_CONTROL` (checked each poll); the mode combo stays usable while connected. |
| **Status footer + on-demand dialogs** | Permanent composed footer (connect/state/error/VBus/power) keeps the main area focused; errors, event log, config, and device info are dialogs opened from menus/footer. |
| **Event log, non-modal + live** | `log_event` records device-side history (connect/state/mode/setpoint/error/clear) so a run-up to an error is visible, even offline. The viewer is a single shared instance updated via a signal (`log_updated`), not a poll or static snapshot. |
| **No transient status-bar messages** | Action feedback and write failures go to the event log (persistent context), not ephemeral `statusBar().showMessage()`. |
| **Auto-connect/reconnect, no manual button** | Startup auto-connect + loss auto-reconnect; the footer shows state. |
| **Ctrl+C via `SIG_DFL`** | The Qt event loop is a blocking C++ call, so a Python `KeyboardInterrupt` isn't serviced during `exec()`. Restoring the default SIGINT action terminates reliably from any state. Safe because the GUI is monitor-only. |

## Exception Handling

Device reads/writes catch only `DEVICE_EXCEPTIONS` and surface writes; optional reads
use `safe_getattr`. Never blanket-swallow `Exception`. `sys.path` bootstrap for
`tools/odrive` + `pyfibre` is kept as bare inline `sys.path.insert` calls before imports
(rules require the bare form — an intermediate assignment trips ruff's E402).

## Adding a New Feature

1. **New control widget**: add it in `setup_ui()`, wire signals to a slot.
2. **New device call**: call on `self.odrive` (or a property like `self.controller`),
   wrap in `try/except DEVICE_EXCEPTIONS`; leave unknown errors uncaught.
3. **New reading**: add a label + update it in `update_readings()`.
4. **New disconnect trigger**: `_on_lost` handles most cases; reconnects automatically.

## Development Tooling

- **`./check.sh`** — `ruff check .`, `mypy` on the QtGUI modules, `py_compile`. Assumes
  `ruff`/`mypy` are on PATH (venv). Optional formatting (opt-in, normalizes the tree):
  `ruff format .`.
- **Commit titles** follow Conventional Commits (`type(scope): summary`, imperative,
  ≤72 chars) with a title, blank line, then a short what/why body. Example:
  `fix: read device setpoint into display on closed-loop entry`.
