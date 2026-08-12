# Qt GUI Architecture

A lightweight PySide6 desktop GUI for monitoring and controlling a single ODrive axis
(velocity-focused, with position and torque modes). Device/motor specifics live in
`Hardware.md`. This file documents how the code is put together and why. When in doubt,
the code is the source of truth — this doc is the durable why, not a line-by-line log.
Process patterns and lessons live in `Methods.md` (read it before changing the code).

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
  The GUI reads the state on connect and periodically to reflect the device state.
  It commands the device only through explicit user actions.
  Connect/disconnect transitions only tear down GUI references — there are **no implicit writes**.
- **Setpoint and parameter changes are applied only on explicit confirmation** (Apply button or Enter key).
  Adjusting a setpoint field never commands the motor; only a confirmed apply sends it.

## UI Stack: Qt Quick (QML) + a single Python backend

The UI is declarative **QML** (Qt Quick Controls, Fusion style); all device logic lives
in one Python `GuiBackend(QObject)` exposed to QML as the context property `backend`.
QML binds to `backend`'s properties, calls its slots, and reacts to its signals; it never
talks to the device directly. This keeps layout/state declarative in QML and the
hardware-coupled, testable logic in Python.

## Module Layout

```
QtGUI/
├── main.py        # App entry: QApplication + QQmlApplicationEngine + context wiring
├── backend.py     # GuiBackend(QObject): the single QML-facing API (all device logic)
├── errors.py      # Error decode (read_error_report) + text formatting (pure logic)
├── eventlog.py    # In-memory event log + formatting (LogEntry/format_log, pure logic)
├── status_backend.py # StatusBackend(QObject): status-footer state (conn/state/errors/VBus/Power)
├── qml/
│   ├── main.qml            # ApplicationWindow: menubar, control bar, footer, command, settings
│   ├── SetpointRow.qml     # reusable Control Command setpoint row (DoubleSpinBox)
│   ├── SpinRow.qml / CheckRow.qml   # reusable settings rows (feature-gated)
│   └── ErrorDialog / EventLogDialog / DeviceInfoDialog.qml  # movable Window dialogs
├── tests/         # headless pytest suite (offscreen, mock ODrive)
├── ruff.toml / check.sh   # lint config + static-check + test runner
├── Hardware.md            # machine / motor / device-config reference
└── ARCHITECTURE.md        # ← this file
```

`GuiBackend` owns all UI state and the device logic. The device root (`self.odrive`) is the
single source of truth; `axis`/`motor`/`encoder`/`controller` are derived read-only
properties (guarded by `DEVICE_EXCEPTIONS`) so they can never go stale or drift out of sync.
QML owns presentation only: it binds to `backend.*` properties (updated on the 100 ms
poll) and invokes `backend.*()` slots for user actions.

## Connection Lifecycle

```
App start ─ QTimer.singleShot(500ms) ─▶ connectOdrive()
                └─ threading.Thread(_connect_worker) ─▶ odrive.find_any()  (blocks)
                     ├─ success ─▶ _on_connected(): store odrive, register _on_lost,
                     │              enable UI (status backend -> Online)
                     └─ failure  ─▶ _on_connect_failed(): retry after 1s

Device lost (USB unplug / reboot)
  └─ PRIMARY:  odrv._on_lost fires (library discovery thread) ─▶ connectOdrive()
       └─ if reads raise ObjectLostError in updateReadings but _on_lost never
          fired (a library bug): caught centrally, logged with traceback, polling
          stopped and reconnect attempted — so the miss is visible, not silent.
```

All worker→UI crossings go through `QTimer.singleShot(0, ...)` (thread-safe post to the
Qt event loop — no mutexes or custom signals).

## Threading Model

| Thread | Work |
|--------|------|
| **Main (Qt event loop)** | All QML + backend: poll timer (`updateReadings`), control/state/mode handlers, event log |
| **Background daemon** | `_connect_worker` → blocking `odrive.find_any()`; result delivered via `QTimer.singleShot(0, ...)` |
| **Library discovery thread** | `odrv._on_lost` → `_on_device_lost()` → `QTimer.singleShot(0, connectOdrive)` |

Qt objects (the QML window and the backend) are only ever touched from the main thread.

## QML ↔ Backend bridge

`backend` is registered as a context property (`engine.rootContext().setContextProperty("backend", ...)`),
so QML accesses it by name everywhere (menus, rows, dialogs); a second context property
`statusBackend` (owned by `GuiBackend`) backs the status footer. The contract:

- **Properties** — connection + status footer (`connected`, `connText`, `connColor`,
  `stateText`, `errorText`, `errorColor`, `vbusText`, `powerText`) live on the
  `statusBackend` context property; control command (`currentMode`, `inputModes`,
  `currentInputMode`, three setpoint values, two estimate labels) and `closedLoop`
  (drives setpoint gating) stay on `backend`. Each has a notify signal so QML bindings
  re-evaluate only when the value changes.
- **Notifications** — poll writes only emit when the displayed string/value actually
  changes (avoid re-rendering on every 100 ms tick). Dialog content (`errorsText`,
  `logText`) is a live-bound property updated on the relevant change signal.
- **Slots** — `run()`, `stop()`, `startState(name)`, `setMode(name)`,
  `setInputMode(idx)`, `setActiveSetpoint(v)`, `applySetpoint()`, `setConfig/getConfig/hasConfig`,
  `save/export/importConfig`, `reboot`, `clearErrors`, `exportLog`, `deviceInfoText`,
  `setVerbose`, `forceReconnect`.
- **Sync, not write-through** — a setpoint row's `DoubleSpinBox` is *not* bound to the
  device; user edits stay local (`setActiveSetpoint`) and reach the device only on
  `applySetpoint()` / Enter. The box re-syncs from `backend` on the setpoint-changed
  signal (QML's `valueModified` fires only on interactive edits, so it can't echo back).

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Single source of truth** | `self.odrive` is the only stored device ref; axis/motor/encoder/controller are direct-access properties (guarded by `DEVICE_EXCEPTIONS`) — removes a five-field state that could drift/stale across reconnects. |
| **QML context property, one `backend` object** | One `QObject` is the whole QML-facing API; no per-field signal plumbing. `setContextProperty` is the runtime bridge (qmllint relies on this, hence `.qmllint.ini` demoting the `unqualified`/`ContextProperties` warnings — a known false positive for injected backends). |
| **`threading.Thread` + `QTimer.singleShot(0,...)`** | A plain daemon thread plus a posted timer beats `QThread`/`moveToThread` boilerplate for one blocking call, and is equally safe (Qt is thread-safe for `singleShot`). |
| **`_on_lost` primary, `ObjectLostError` fallback** | `_on_lost` gives instant disconnect notification; a central `ObjectLostError` catch in `updateReadings` catches the (rare) case where it doesn't fire, stops polling and reconnects. |
| **Catch only `DEVICE_EXCEPTIONS`** | The expected, transient fibre/odrive failures (`ObjectLostError`, `EOFError`, `TimeoutError`, `OSError`, `CancelledError`). A bare generic libfibre `Exception` is a bug and is left uncaught so it surfaces with a stack trace. |
| **`_on_lost.done()` guard on connect** | If the device dropped before the callback is registered, schedule a fresh reconnect rather than letting `add_done_callback` fire in the connect thread. |
| **States execute only via button** | Selecting a dropdown item just sets the selection; only Start/Execute actually commands the device — browsing can't accidentally start a calibration. |
| **Confirmed setpoint apply** | Spinboxes never write on change; the active setpoint goes to the device only on Apply / Enter. |
| **Two-tier UI gating** | The whole Control Command group is `enabled: statusBackend.connected` (disables mode/input combos offline); setpoint rows additionally gate on `backend.closedLoop` (only while running). Settings tab is gated on `statusBackend.connected`. |
| **Status footer pinned to window bottom** | `ApplicationWindow.footer` holds the composed connect/state/error/VBus/power bar (like the old `QStatusBar`), keeping the main area focused. |
| **Dialogs as top-level `Window`s** | Error, Event Log, and Device Info are separate Qt Quick `Window`s (native title bar, movable/resizable) sized to their content layout — not frameless popups. File dialogs stay native `QFileDialog`. |
| **Event log, non-modal + live** | `logEvent` records device-side history (connect/state/mode/setpoint/error/clear) so a run-up to an error is visible, even offline. The viewer binds `backend.logText` live via the `logUpdated` signal (no polling), and works while disconnected. |
| **No transient status-bar messages** | Action feedback and write failures go to the event log (persistent context), not ephemeral status messages. |
| **Auto-connect/reconnect, no manual button** | Startup auto-connect + loss auto-reconnect; the footer shows state. |
| **Ctrl+C via `SIG_DFL`** | The Qt event loop is a blocking C++ call, so a Python `KeyboardInterrupt` isn't serviced during `exec()`. Restoring the default SIGINT action terminates reliably from any state. Safe because the GUI is monitor-only. |

## Exception Handling

Device reads/writes catch only `DEVICE_EXCEPTIONS`. Never blanket-swallow `Exception`. `sys.path` bootstrap for
`tools/odrive` + `pyfibre` is kept as bare inline `sys.path.insert` calls before imports
(rules require the bare form — an intermediate assignment trips ruff's E402).

## Adding a New Feature

1. **New control widget**: add a reusable component under `qml/` (or inline in `main.qml`),
   bound to `backend` properties/slots; wire the underlying logic in `backend.py`.
2. **New device call**: add a `@Slot` on `GuiBackend`, wrap in `try/except DEVICE_EXCEPTIONS`;
   leave unknown errors uncaught. Call it from QML via `backend.method()`.
3. **New reading**: add a property + notify signal in `backend.py`, update it in
   `updateReadings()` (only emitting when the value changes), and bind a QML label to it.
4. **New setting param**: add a `SpinRow`/`CheckRow` in the relevant `main.qml` tab with
   `base`/`attr`; the backend's generic `setConfig/getConfig/hasConfig` handle gating + IO.
5. **New disconnect trigger**: `_on_lost` handles most cases; reconnects automatically.

## Development Tooling

- **`./check.sh`** — `ruff check .`, `mypy` on the QtGUI modules, `py_compile`, `qmllint`
  on `qml/`, and a headless `pytest` run (`QT_QPA_PLATFORM=offscreen`, mock ODrive).
  Skips `pyside6-qmllint`/`pytest` gracefully if not installed. `ruff`/`mypy`/`pytest` are
  expected on PATH (venv). Optional formatting (opt-in, normalizes the tree): `ruff format .`.
- **`tests/`** — pytest suite: backend unit tests against a mock device + QML load/linkage
  tests offscreen. No hardware or display required.
- **Commit titles** follow Conventional Commits (`type(scope): summary`, imperative,
  ≤72 chars) with a title, blank line, then a short what/why body. Example:
  `fix: read device setpoint into display on closed-loop entry`.
