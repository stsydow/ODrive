# Architecture

ODrive is a high-performance brushless motor controller firmware and tooling.
This document captures the project's architecture for future reference.

## Directory Layout

| Directory | Responsibility |
|-----------|---------------|
| **Firmware/** | C++ firmware for STM32F405 (ODrive v3.x, NRND) |
| **Firmware/MotorControl/** | Core motor control logic: axis, motor, encoder, FOC, controllers, estimators |
| **Firmware/communication/** | Protocol interfaces: USB (Fibre), UART, I2C, CAN |
| **Firmware/Board/v3/** | Board-specific HAL, pin definitions, ADC, PWM for v3 hardware |
| **Firmware/ThirdParty/** | CMSIS, FreeRTOS, STM32CubeF4, HAL drivers, USB device library |
| **Firmware/fibre-cpp/** | Fibre RPC framework — C++ reference implementation (embedded + libfibre) |
| **Firmware/odrive-interface.yaml** | Interface definition (attributes, enums, methods) driving code generation |
| **tools/** | Python ecosystem: `odrive` library, `pyfibre`, `fibre-tools`, test suite |
| **tools/odrive/** | Python package for device discovery, DFU, configuration, utilities |
| **tools/fibre-tools/** | Fibre interface generator (YAML → C++/Python bindings) |
| **GUI/** | Vue.js web GUI (Electron) with Flask backend server |
| **GUI/src/** | Vue components, Fibre comms layer, store, views |
| **GUI/server/** | Flask/SocketIO server bridging GUI to odrivetool |
| **Arduino/** | Arduino library for I2C/serial ODrive control |
| **docs/** | Sphinx documentation (RST) |
| **analysis/** | MATLAB/Python simulation, motor analysis, cogging torque |

## Key Types and Traits

### Firmware (C++)
- **`ODrive`** (`main.cpp`): Top-level singleton exposing the Fibre interface defined in `odrive-interface.yaml`.
- **`Axis`** (`axis.hpp`): Per-axis state machine managing calibration → full_encod → velocity → position modes.
- **`Motor`** (`motor.hpp`): Current sensing, DC calibration, arming, fault handling.
- **`Controller`** (`controller.hpp`): Velocity/position/current control modes with trajectory tracking.
- **`Encoder`** (`encoder.hpp`): Handles incremental, absolute, and Hall sensor encoders.
- **`FOC`** (`foc.hpp`): Field-oriented control (Park/Inverse Park transforms).
- **`PWMMapping_t`** (`odrive_main.h`, `low_level.cpp`, `pwm_input.cpp`): ADC and PWM input mapping with deadband support, `deadband_idle` mode (freewheeling handwheel when in deadband), and safety arming latch.
- **Component hierarchy** (`component.hpp`): Hierarchical task scheduling with priority-based execution in the control loop.
- **`ConfigManager`** (`nvm_config.hpp`): Non-volatile configuration persistence.

### Fibre Framework
- **`fibre-cpp`** (`Firmware/fibre-cpp/`): C++ RPC framework for cross-platform object exposure. Embedded mode (inline) and `libfibre` (shared library) backends.
- **Interface generator** (`tools/fibre-tools/interface_generator.py`): Transforms `odrive-interface.yaml` into C++ class headers and Python bindings.
- **`pyfibre`** (`tools/odrive/pyfibre/`): Python Fibre client library for device discovery and attribute/method access.

### Python Tooling
- **`odrive` package** (`tools/odrive/`): Device discovery (`find_any()`), DFU flashing (`dfuse/`), configuration helpers, USB/CAN/I2C backends.
- **`odrivetool`** (`tools/odrivetool`): CLI entry point wrapping the Python library.

## Control Flow

1. **Boot** (`main.cpp`): STM32 HAL init → Fibre platform init → config load → FreeRTOS task creation.
2. **Control loop**: `ODrive::Run()` iterates through component hierarchy at fixed frequency, calling each component's `Update()` (encoder → estimator → FOC → current control → motor).
3. **Status LED**: `StatusLedController::update()` runs periodically, pulsing green (armed), red (error), or blue (idle).
4. **Fibre endpoints**: USB/UART/CAN/I2C interfaces expose the `ODrive` interface tree to hosts. Attributes are read/written; methods are invoked remotely.
5. **CAN**: `communication/can/` — CAN protocol with heartbeat, node-id, and ODrive-specific messages.

## Data Flow

```
Host (odrivetool/GUI)
  ← Fibre protocol →
  USB / UART / I2C / CAN
  ← Attributes & methods →
Firmware (ODrive → Axis → Motor)
  ← Real-time control →
  PWM → MOSFETs → Motor phases
  ← ADC feedback →
  Current sense, encoder, thermistors
```

## Design Decisions

- **Fibre over custom protocol**: Unified RPC framework handles attribute access, method invocation, and event notification across all backends (USB, UART, CAN, I2C).
- **Interface-driven code generation**: `odrive-interface.yaml` is the single source of truth for the public API. C++ and Python bindings are generated, avoiding drift.
- **QtGUI never keeps device sub-objects (QtGUI/)**. The connected root is stored as `self.odrive`; `axis0`/`motor`/`encoder`/`controller` are derived per use and never cached, so a stale reference can't outlive a disconnect. `safe_getattr` is used only to reach/guard `axis0` — the one level absent on an empty/disconnected object — because once connected the tree below `axis0` is fully populated and read with plain attribute access. Disconnect detection relies on `_on_lost` plus reads that surface `ObjectLostError` (via `_read_value`) into the reconnect counter.
- **The ODrive object tree is statically defined, not optional (`Firmware/odrive-interface.yaml`).** The YAML is the single source of truth for the exposed tree (see `tools/fibre-tools/interface_generator.py`), and it contains no conditional/optional markers — for a given firmware build every object and attribute (`motor`, `controller`, `encoder`, `sensorless_estimator`, version fields, config flags) is always present. So the GUI treats the tree as fully populated when connected. The only two real sources of absence are:**(a)** disconnect (the empty `odrive` object — handled by the `axis0` guard above) and **(b)** firmware-version drift, where an attribute added/removed across releases is gated with `hasattr(obj.config, attr)` (see `QtGUI/controls.py`) rather than `safe_getattr`. Guarded reads that are redundant after a presence check — e.g. `safe_getattr(obj.config, x)` after `hasattr` — are avoided.
- **Configuration backup/restore persistence (`tools/odrive/configuration.py`)**: Endpoints are restored from path strings to `RemoteObject` instances via `path_to_obj()`, non-finite floats serialize as valid JSON strings (`Infinity`, `-Infinity`, `NaN`) to preserve limits without `NoneType` codec errors, and read-only attributes without `exchange` (such as `anticogging` metrics) are skipped cleanly during restore.
- **Component hierarchy**: Motor control components are organized in a tree with priority-based scheduling, enabling modular extension of the control loop.
- **FreeRTOS**: Preemptive multitasking with separate tasks for USB IRQ, UART, CAN, and the main control loop.

## Dependencies

- **STM32 HAL / CMSIS**: Vendor SDK for GPIO, ADC, TIM, USB, USART, I2C.
- **FreeRTOS**: Real-time OS for task scheduling.
- **Fibre**: Custom RPC framework (C++ embedded + libfibre shared lib).
- **Python**: `odrive` package uses `pyusb`, `pyserial`, `pyfibre` for host-side communication.
- **Vue.js + Flask**: Web GUI with WebSocket support for live data plotting.

## Entry Points

| Entry | File | Purpose |
|-------|------|---------|
| Firmware main | `Firmware/MotorControl/main.cpp` | Device firmware entry |
| Python library | `tools/odrive/__init__.py` | `find_any()` discovery |
| CLI tool | `tools/odrivetool` | `odrivetool` command |
| GUI server | `GUI/server/odrive_server.py` | Flask backend for web GUI |
| Fibre generator | `tools/fibre-tools/interface_generator.py` | YAML → bindings |
