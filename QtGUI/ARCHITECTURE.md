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
  (One deliberate nuance: explicitly switching the control mode steers an
  incompatible input mode to the new mode's default — still a user-initiated write.)
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
├── configtree.py  # ConfigTreeModel: object-graph tree model for the Config Browser
├── errors.py      # Error decode (read_error_report) + text formatting (pure logic)
├── eventlog.py    # In-memory event log + formatting (LogEntry/format_log, pure logic)
├── monitoring.py  # Live plot: channel registry + SampleBuffer + pyqtgraph PlotWindow
├── status_backend.py # StatusBackend(QObject): unified status badge, decoded errors, VBus/Power
├── qml/
│   ├── main.qml            # ApplicationWindow: menubar, control bar, footer, command, settings
│   ├── SetpointRow.qml     # reusable Control Command setpoint row (DoubleSpinBox)
│   ├── SpinRow.qml / CheckRow.qml   # reusable settings rows (feature-gated; limits tabs gate via enabled)
│   ├── StatusBar.qml       # footer status bar (unified status badge + VBus/power fields)
│   └── ErrorDialog / EventLogDialog / DeviceInfoDialog / ConfigBrowserDialog.qml  # Window dialogs
├── tests/         # headless pytest suite (offscreen, mock ODrive)
├── ruff.toml / check.sh   # lint config + static-check + test runner
├── Hardware.md            # machine / motor / device-config reference
└── ARCHITECTURE.md        # ← this file
```

`GuiBackend` owns all UI state and the device logic. `self.odrive` is the only stored device
ref; the device state is the source of truth and display values are re-read from it by the
100 ms poll (all display reads happen inside `updateReadings`, see "Device I/O" below) or on
explicit user actions.
QML owns presentation only: it binds to `backend.*` properties (updated on the 100 ms poll)
and invokes `backend.*()` slots for user actions.

## Connection Lifecycle

```
App start ─ QTimer.singleShot(500ms) ─▶ connectOdrive()
                └─ threading.Thread(_connect_worker) ─▶ odrive.find_any()  (blocks)
                     ├─ success ─▶ _on_connected(): store odrive, register _on_lost,
                     │              enable UI (status backend -> Online)
                     └─ failure  ─▶ _on_connect_failed(): retry after 1s

Device lost (USB unplug / reboot)
  └─ PRIMARY:  odrv._on_lost fires (library discovery thread) ─▶ connectOdrive()
       └─ if reads fail inside updateReadings without _on_lost having fired
          (transient TimeoutError/OSError, or the lost-object race): caught
          centrally, logged with traceback and reconnect attempted — visible,
          not silent.
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

- **Properties** — unified status badge (`statusText`, `statusColor`, `hasError`),
  connection + status footer legacy compatibility (`connected`, `connText`, `connColor`,
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
  `saveConfig`, `savePreCalibrated`, `export/importConfig`, `reboot`, `clearErrors`, `exportLog`,
  `deviceInfoText`, `setVerbose`.
- **Sync, not write-through** — a setpoint row's `DoubleSpinBox` is *not* bound to the
  device; user edits stay local (`setActiveSetpoint`) and reach the device only on
  `applySetpoint()` / Enter. The box re-syncs from `backend` on the setpoint-changed
  signal (QML's `valueModified` fires only on interactive edits, so it can't echo back).

## Config Browser (§2.4)

`ConfigTreeModel` (configtree.py) walks the live ODrive object graph for the QML `TreeView`
in `qml/ConfigBrowserDialog.qml` (Device > Config Browser…). Structure is built lazily per
subtree expansion (`dir()` + `getattr`, callables skipped, MAX_DEPTH=7); values are read in
the same walk and cached until Refresh. Editable = leaves under `.config` that are bool/int/
float; an `edit` button on each writable leaf opens a small OK/cancel editor and
`backend.writeBrowserValue(path, text)`
parses the input as the current value's type (ints accept hex for enum leaves), re-checks the
IDLE gate at commit time, writes, logs a WRITE event and invalidates the view cache. The name
filter prunes the tree to paths containing the substring — applied on Enter/focus-loss
(`onEditingFinished`) and matched **name-only**: the scan classifies branches via fibre
class-member introspection (`RemoteAttribute._magic_getter`), so it never reads scalar
endpoint values over USB. Transport failures anywhere in the walk follow the same single-strike
policy as the poll: `_drop_link()` + auto-reconnect (Plan §4.1).

The same IDLE gate backs the Electrical/Mechanical Limits settings tabs — without any flag
plumbing: those tabs bind `enabled: statusBackend.connected && backend.axisIdle` (the property
is refreshed by the 100 ms poll in `_sync_closed_loop`), and Qt Quick's enablement cascade
blocks every row inside. Control Parameters stays write-anytime (online gain tuning). Residual
gap: a ≤100 ms poll-lag race where a click lands just after the axis left IDLE — accepted,
since the firmware enforces real safety; only the browser's long-lived editor needs the strict
commit-time re-check.

## Analog Input

Firmware: one mapping struct **per GPIO** (`odrive.config.gpioN_analog_mapping` /
`analog_mappings[i]` = {endpoint, min, max, deadband_*}); a 10 ms poll thread writes the
scaled ADC value to every slot whose **endpoint ref is valid** — enable *is* a non-null
endpoint, and `gpioN_mode = ANALOG_IN` is only the prerequisite for a valid ADC reading.
The endpoint is generic (any fibre endpoint); the common case is a controller input
(`controller._input_{vel,pos,torque}_property`), which is mode-agnostic — the mapping never
reads the control mode.

GUI split by parameter dependence:
- **Input target** (units follow the setpoint) → **on the setpoint row**: enable checkbox + min + max.
  Radio: one row is the input target at a time; enabling a row retargets the mapping to that
  row's input and disables the others. The driven row is disabled and mirrors the live
  mapped value.
- **Input source** (parameter-independent) → **Settings → "Analog Input" tab**: GPIO
  selector (fixed list 3/4, the ADC-capable pins) + deadband fields (guarded — the
  firmware properties may be absent on older fw) + "Idle in deadband" toggle.
- All mapping writes are board-level → **IDLE-gated** like the limits tabs. The device's
  `analog_mappings[active_gpio]` is the source of truth; the backend projects it to
  {active_gpio, target row, min, max, deadband} and polls the live mapped value.
- **Deadband Idle + Safety Arm Latch**: in firmware, `deadband_idle` transitions the axis
  to `AXIS_STATE_IDLE` in deadband (freewheeling handwheel, cold FETs) and re-engages
  `CLOSED_LOOP_CONTROL` when pressed. A firmware-level safety latch requires explicit arming
  (manual run or `startup_closed_loop_control`), and disarms immediately on any error or manual stop.

Status: row-based split implemented. One active input mapping at a time; other
sensors/control lines may reuse the source side.

# ponytail: the browser's EXPANSION walk reads scalar values too (~40 reads/expansion);
cheap at the measured ~4 kHz sequential link rate (`bench_poll_rate.py`, Plan §3.4 Step 0). The filter scan is already name-only (see above).

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Single source of truth** | `self.odrive` is the only stored device ref — no cached copy of device values that could drift/stale across reconnects (a partial snapshot dict was tried and deliberately removed: half-fresh values are worse than a failed read). |
| **QML context property, one `backend` object** | One `QObject` is the whole QML-facing API; no per-field signal plumbing. `setContextProperty` is the runtime bridge (qmllint relies on this, hence `.qmllint.ini` demoting the `unqualified`/`ContextProperties` warnings — a known false positive for injected backends). |
| **`threading.Thread` + `QTimer.singleShot(0,...)`** | A plain daemon thread plus a posted timer beats `QThread`/`moveToThread` boilerplate for one blocking call, and is equally safe (Qt is thread-safe for `singleShot`). |
| **Central guarded poll, not per-read handlers** | All display reads run inside `updateReadings`' single try/except → one failure policy (log + auto-reconnect). User-action slots keep their own handlers because they must report write failures. |
| **`_on_lost` primary, central read-failure fallback** | `_on_lost` gives disconnect notification, but it lags the actual failure (fires only when libfibre releases the object). A central catch of `(*DEVICE_EXCEPTIONS, AttributeError)` in `updateReadings` covers transient transport errors and the lost-object race, stops polling and reconnects. |
| **Catch only `DEVICE_EXCEPTIONS`** | The expected, transient fibre/odrive failures (`ObjectLostError`, `EOFError`, `TimeoutError`, `OSError`, `CancelledError`). A bare generic libfibre `Exception` is a bug and is left uncaught so it surfaces with a stack trace. |
| **`_on_lost.done()` guard on connect** | If the device dropped before the callback is registered, schedule a fresh reconnect rather than letting `add_done_callback` fire in the connect thread. |
| **States execute only via button** | Selecting a dropdown item just sets the selection; only Start/Execute actually commands the device — browsing can't accidentally start a calibration. |
| **Confirmed setpoint apply** | Spinboxes never write on change; the active setpoint goes to the device only on Apply / Enter. |
| **UI gating on connect** | The whole Control Command group and Settings tab are `enabled: statusBackend.connected`. Setpoints stay editable in Idle on purpose: pre-setting a velocity before Run is the point, and writing `input_*` while idle moves nothing. |
| **Status footer pinned to window bottom** | `ApplicationWindow.footer` holds the composed status bar (unified status badge + VBus/power), keeping the main area focused. |
| **Unified status badge with error decode** | Merges connection, axis state, and error into one high-signal status indicator (`Connecting` / `Error: <NAME>` / `Idle (Armed/Disarmed)` / `Running` / `Calibration...`). Errors decode bitmasks into human-readable strings and clicking opens the Error dialog. |
| **Expected reboot absorption on NVM save** | `saveConfig` and `savePreCalibrated` absorb the expected `ObjectLostError` when ODrive v3 saves to flash and reboots, marking status as `Rebooting…` without false-alarm error dialogs. |
| **Dialogs as top-level `Window`s** | Error, Event Log, and Device Info are separate Qt Quick `Window`s (native title bar, movable/resizable) sized to their content layout — not frameless popups. File dialogs stay native `QFileDialog`. |
| **Event log, non-modal + live** | `logEvent` records device-side history (connect/state/mode/setpoint/error/clear) so a run-up to an error is visible, even offline. The viewer binds `backend.logText` live via the `logUpdated` signal (no polling), and works while disconnected. |
| **No transient status-bar messages** | Action feedback and write failures go to the event log (persistent context), not ephemeral status messages. |
| **Auto-connect/reconnect, no manual button** | Startup auto-connect + loss auto-reconnect; the footer shows state. |
| **Live plot samples only active channels** | The plot window pushes its checkbox+control-mode-visible channel set (`setActiveChannels`) on every toggle; `_sample_plot` reads just those. Unchecked/gated channels stay NaN in ring buffer and CSV. Cap: `ACTIVE_CHANNEL_LIMIT` (15) concurrent channels per the USB read budget; extras are logged and dropped, not silently skipped or over-sampled. |
| **Ctrl+C via `SIG_DFL`** | The Qt event loop is a blocking C++ call, so a Python `KeyboardInterrupt` isn't serviced during `exec()`. Restoring the default SIGINT action terminates reliably from any state. Safe because the GUI is monitor-only. |

## Device I/O: how libfibre reads actually work

Findings from `tools/odrive/pyfibre/fibre/libfibre.py` / `protocol.py`:

- **Every attribute access is its own blocking RPC.** `odrv.axis0.encoder.vel_estimate`
  resolves each hop via `RemoteAttribute.__get__` → C `libfibre_get_attribute`, and magic
  getters call a `read()` remote function that blocks the calling thread until the endpoint
  round-trip completes. Only the Python *wrapper* objects are memoized (`LibFibre._objects`),
  never values.
- **No bulk read exists.** The exported C API has no batch primitive (only `libfibre_call`,
  `libfibre_get_attribute`, raw tx/rx); `fibre.read_all` drains bytes of a single transfer;
  `RemoteObject._dump()` walks the tree doing N sequential RPCs. Reading all displayed values
  at once would require firmware/protocol changes — deliberately not pursued. If poll traffic
  ever matters, shrink the set of polled endpoints instead.
- **Exception mapping** (`_get_exception`): status codes become `CancelledError`, `EOFError`
  (closed), `ObjectLostError` (host unreachable), `TimeoutError`/`OSError` (transport layers
  above), or a bare `Exception` for internal/protocol errors — the latter stay uncaught on
  purpose (bug signal).
- **`_on_lost` lags the failure.** It fires when libfibre releases the object
  (`_release_py_obj` → `_destroy()`), after reads already fail. Once destroyed, further
  attribute access raises **`AttributeError`** (class swapped to `EmptyInterface`), not
  `ObjectLostError`. Hence the poll catches `(*DEVICE_EXCEPTIONS, AttributeError)`.
- **Consequence:** one guarded fetch point (`GuiBackend.updateReadings`) covers every
display read; any transport hiccup drops
  the link and auto-reconnects (upgrade path: an N-strike counter if USB proves flaky).

## Firmware oscilloscope (control-loop debugging)

The firmware has an on-chip oscilloscope (`Firmware/MotorControl/oscilloscope.{hpp,cpp}`)
for recording signals at the **control-loop rate** — the mechanism to use when the GUI's
100 ms poll is too slow (loop tuning, step responses). How it works:

- One capture buffer: `float data_[4096]` (`OSCILLOSCOPE_SIZE` in oscilloscope.hpp; RAM-bound,
  bump it if needed). At 8 kHz control rate that is ~0.5 s of signal.
- `Oscilloscope::update()` runs once per `control_loop_cb` interrupt, next to the estimator
  output-port resets, so sample rate == control-loop rate.
- Simple edge trigger: one `trigger_src_` float watched against `trigger_threshold_`; below
  threshold arms the capture, crossing above fills the buffer with samples of ONE variable.
- **Compile-time channel selection:** both sources are plain pointers set in
  `odrive_main.h:L227` (`nullptr` by default → records nothing out of the box). Capturing a
  value means pointing `data_src_` at that float (e.g. a controller/encoder estimate),
  optionally setting `trigger_src_`, and reflashing. There is no endpoint to select channels
  at runtime.
- Host interface: only `odrv.oscilloscope.size` and `get_val(index)` — one blocking RPC per
  sample. `tools/odrive/utils.py` wraps this: `oscilloscope_dump(odrv, n, file)` writes CSV,
  `show_oscilloscope(odrv)` plots via matplotlib. No timestamps are exported; index ×
  control-loop period is the timebase.

If loop debugging becomes a QtGUI feature: pick the variable in `odrive_main.h`, reflash,
then add a "Capture & Export" slot that reads `get_val(0..size-1)` in a worker thread and
writes CSV. Known ceilings (accepted until proven limiting): single channel, compile-time
selection, fixed trigger, 4096-sample window. Multi-channel/runtime-selectable capture would
be a firmware project, not a GUI feature.

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
