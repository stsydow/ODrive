"""QML-facing backend for the ODrive GUI.

GuiBackend(QObject) owns every piece of device/UI state the widget
``ODriveGUI`` held, minus the widget construction. Exposed to QML as the
context property ``backend``, it is the single bridge: QML binds to its
properties, calls its slots, and reacts to its signals.

Connection lifecycle, polling, error decode, setpoint sync and the event log
are ported from the widget main.py; ARCHITECTURE.md semantics are unchanged —
only the presentation target changed from widgets to QML properties. Qt is
only ever touched from the main (Qt) thread (the connect worker reports back
via ``QTimer.singleShot(0, ...)``).
"""

import logging
import math
import threading
import time
from collections import deque

import odrive
import odrive.configuration
import odrive.enums
from odrive.enums import (
    AXIS_STATE_CLOSED_LOOP_CONTROL,
    AXIS_STATE_IDLE,
    CONTROL_MODE_POSITION_CONTROL,
    CONTROL_MODE_TORQUE_CONTROL,
    CONTROL_MODE_VELOCITY_CONTROL,
    GPIO_MODE_ANALOG_IN,
    GPIO_MODE_DIGITAL,
    INPUT_MODE_PASSTHROUGH,
    INPUT_MODE_POS_FILTER,
    INPUT_MODE_TORQUE_RAMP,
    INPUT_MODE_TRAP_TRAJ,
    INPUT_MODE_VEL_RAMP,
)
from PySide6.QtCore import Property, QObject, QTimer, Signal, Slot
from PySide6.QtWidgets import QFileDialog, QMessageBox

from configtree import ConfigTreeModel
from errors import DEVICE_EXCEPTIONS
from eventlog import LogEntry, format_log
from monitoring import (
    ACTIVE_CHANNEL_LIMIT,
    CHANNELS,
    SAMPLE_INTERVAL_MS,
    PlotWindow,
    SampleBuffer,
)
from status_backend import STATE_MAP, StatusBackend

logger = logging.getLogger(__name__)

RECONNECT_RETRY_DELAY_MS = 1000
UI_UPDATE_INTERVAL_MS = 100  # ms -> 10Hz

# Control mode <-> display name (order defines the mode-combo index).
MODE_NAMES = {
    CONTROL_MODE_VELOCITY_CONTROL: "Velocity",
    CONTROL_MODE_POSITION_CONTROL: "Position",
    CONTROL_MODE_TORQUE_CONTROL: "Torque",
}
_MODE_ORDER = (
    CONTROL_MODE_VELOCITY_CONTROL,
    CONTROL_MODE_POSITION_CONTROL,
    CONTROL_MODE_TORQUE_CONTROL,
)
MODE_VALUES = {name: value for value, name in MODE_NAMES.items()}

# Setpoint endpoint + display label per control mode (order irrelevant).
_SETPOINT_TARGETS = {
    CONTROL_MODE_VELOCITY_CONTROL: ("input_vel", "Velocity"),
    CONTROL_MODE_TORQUE_CONTROL: ("input_torque", "Torque"),
    CONTROL_MODE_POSITION_CONTROL: ("input_pos", "Position"),
}

# Analog input: maps an ADC reading to a controller input endpoint
# (odrv.config.gpio{N}_analog_mapping, firmware low_level.cpp
# analog_polling_thread).
ANALOG_GPIOS = (3, 4)
_ANALOG_ENDPOINT_ATTR = {
    CONTROL_MODE_VELOCITY_CONTROL: "_input_vel_property",
    CONTROL_MODE_TORQUE_CONTROL: "_input_torque_property",
    CONTROL_MODE_POSITION_CONTROL: "_input_pos_property",
}


def _f(obj, attr):
    """getattr as float; NaN when the endpoint doesn't exist on this fw."""
    v = getattr(obj, attr, None)
    return float(v) if v is not None else math.nan


# Plot channels: key -> reader(axis, odrive) -> float. Keys must match the
# entries in monitoring.CHANNELS (labels/rows/mode gating live there); adding
# a channel means one entry here and one in CHANNELS.


def _pos_reader(axis, _odrv):
    enc = axis.encoder
    v = getattr(enc, "pos_circular", None)
    if v is None:
        v = getattr(enc, "pos_estimate", None)
    return float(v) if v is not None else math.nan


def _iq_reader(axis, _odrv):
    return _f(getattr(axis.motor, "current_control", None), "Iq_measured")


def _torque_reader(axis, _odrv):
    # Iq is derived by fw from its two measured phase currents; there is no
    # torque endpoint on 0.5.x -> computed as Iq * torque_constant.
    iq = _iq_reader(axis, None)
    tc = getattr(getattr(axis.motor, "config", None), "torque_constant", None)
    return iq * float(tc) if not math.isnan(iq) and tc is not None else math.nan


def _adc_reader(gpio):
    """Analog input on `gpio` as 0-1 fraction of 3.3 V.
    gpio=3 is live today; gpio=4 uses the same analog mode when needed."""

    def read(_axis, odrv):
        fn = getattr(odrv, "get_adc_voltage", None)
        return float(fn(gpio)) / 3.3 if fn else math.nan

    return read


_PLOT_READERS = {
    "vel": lambda a, d: _f(a.encoder, "vel_estimate"),
    "pos": _pos_reader,
    # loop setpoints: post-filtering values the controller chases
    "pos_sp": lambda a, d: _f(a.controller, "pos_setpoint"),
    "vel_sp": lambda a, d: _f(a.controller, "vel_setpoint"),
    "tq_sp": lambda a, d: _f(a.controller, "torque_setpoint"),
    # commanded inputs: what Apply wrote (steps instantly under ramps)
    "pos_in": lambda a, d: _f(a.controller, "input_pos"),
    "vel_in": lambda a, d: _f(a.controller, "input_vel"),
    "tq_in": lambda a, d: _f(a.controller, "input_torque"),
    "iq": _iq_reader,
    "i_a": lambda a, d: _f(a.motor, "current_meas_phA"),
    "i_b": lambda a, d: _f(a.motor, "current_meas_phB"),
    "torque": _torque_reader,
    "p_mech": lambda a, d: _f(a.controller, "mechanical_power"),
    "p_elec": lambda a, d: _f(a.controller, "electrical_power"),
    "vbus": lambda a, d: _f(d, "vbus_voltage"),
    # analog inputs: GPIO3 live today, GPIO4 same analog mode when needed
    "adc3": _adc_reader(3),
}

# Input modes exposed in the selector. Value -> display label.
INPUT_MODES = {
    INPUT_MODE_PASSTHROUGH: "Passthrough",
    INPUT_MODE_VEL_RAMP: "Velocity Ramp",
    INPUT_MODE_POS_FILTER: "Position Filter",
    INPUT_MODE_TRAP_TRAJ: "Trapezoidal Trajectory",
    INPUT_MODE_TORQUE_RAMP: "Torque Ramp",
}
MODES_BY_CONTROL = {
    CONTROL_MODE_VELOCITY_CONTROL: [INPUT_MODE_VEL_RAMP, INPUT_MODE_PASSTHROUGH],
    CONTROL_MODE_POSITION_CONTROL: [
        INPUT_MODE_POS_FILTER,
        INPUT_MODE_TRAP_TRAJ,
        INPUT_MODE_PASSTHROUGH,
    ],
    CONTROL_MODE_TORQUE_CONTROL: [INPUT_MODE_TORQUE_RAMP, INPUT_MODE_PASSTHROUGH],
}
DEFAULT_BY_CONTROL = {
    CONTROL_MODE_VELOCITY_CONTROL: INPUT_MODE_VEL_RAMP,
    CONTROL_MODE_POSITION_CONTROL: INPUT_MODE_TRAP_TRAJ,
    CONTROL_MODE_TORQUE_CONTROL: INPUT_MODE_TORQUE_RAMP,
}

# Axis states selectable from the "Program" dropdown (label -> value).


class GuiBackend(QObject):
    """All device logic, exposed to QML."""

    # -- QML-facing signals --------------------------------------------
    estimatesChanged = Signal()  # velEstimateText / posEstimateText
    closedLoopChanged = Signal()  # closedLoop
    modeChanged = Signal()  # currentMode
    inputModeModelChanged = Signal()  # inputModes / currentInputMode
    setpointChanged = Signal()  # vel/torque/pos setpoints
    verboseChanged = Signal()  # verbose
    idleChanged = Signal()  # axisIdle
    logUpdated = Signal()  # a LogEntry was appended
    errorsChanged = Signal()
    analogChanged = Signal()  # analog mapping state / live mapped value

    def __init__(self, verbose=False):
        super().__init__()
        self.odrive = None  # single source of truth; sub-objects derived per use
        self._target_serial = None  # reconnects target this device once known
        self.status_backend = StatusBackend(self)
        self._connecting = False
        self._last_synced_mode = None
        self._verbose = verbose

        # QML-bound state
        self._closed_loop = False
        self._axis_idle = False
        self._current_mode = 0  # index into _MODE_ORDER
        self._active_mode_enum = CONTROL_MODE_VELOCITY_CONTROL
        self._input_modes = []
        self._input_mode_values = []
        self._input_mode_index = -1
        self._setpoints = dict.fromkeys(_MODE_ORDER, 0.0)
        self._vel_est_text = "est: -- rps"
        self._pos_est_text = "est: -- rev"

        # Analog input mapping state.
        self._analog_gpio = 3
        self._analog_target = "Disabled"
        self._analog_value = 0.0
        self._analog_min = 0.0
        self._analog_max = 0.0
        self._analog_deadband_available = False
        self._analog_deadband_enable = False
        self._analog_deadband_start = 0.0
        self._analog_deadband_end = 0.0
        self._analog_deadband_level = 0.0
        self._analog_deadband_idle = False
        self._armed = False

        self.event_log = deque(maxlen=1000)
        self.samples = SampleBuffer()
        # channels sampled per plot tick; starts as the default-on registry
        # entries so opening the plot later still finds history
        self._active_channels = {
            key for key, _, default_on, *_ in CHANNELS if default_on
        }
        self._plot_window = None
        self._config_model = ConfigTreeModel(self)

        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self.updateReadings)
        self.update_timer.start(UI_UPDATE_INTERVAL_MS)

        # Plot sampling at on its own tick:
        self.plot_timer = QTimer()
        self.plot_timer.timeout.connect(self.plotTick)
        self.plot_timer.start(SAMPLE_INTERVAL_MS)

        QTimer.singleShot(500, self.connectOdrive)

    # -- static models (combo contents) --------------------------------

    @Property(list, constant=True)
    def modeNames(self):
        return [MODE_NAMES[m] for m in _MODE_ORDER]

    @Property(list, constant=True)
    def stateNames(self):
        return list(STATE_MAP.keys())

    @Property(bool, notify=closedLoopChanged)
    def closedLoop(self):
        return self._closed_loop

    @Property(bool, notify=idleChanged)
    def axisIdle(self):
        return self._axis_idle

    @Property(bool, notify=verboseChanged)
    def verbose(self):
        return self._verbose

    # -- control-command properties ------------------------------------

    @Property(int, notify=modeChanged)
    def currentMode(self):
        return self._current_mode

    @Property(list, notify=inputModeModelChanged)
    def inputModes(self):
        return self._input_modes

    @Property(int, notify=inputModeModelChanged)
    def currentInputMode(self):
        return self._input_mode_index

    @Property(float, notify=setpointChanged)
    def velSetpoint(self):
        return self._setpoints[CONTROL_MODE_VELOCITY_CONTROL]

    @Property(float, notify=setpointChanged)
    def torqueSetpoint(self):
        return self._setpoints[CONTROL_MODE_TORQUE_CONTROL]

    @Property(float, notify=setpointChanged)
    def posSetpoint(self):
        return self._setpoints[CONTROL_MODE_POSITION_CONTROL]

    @Property(str, notify=estimatesChanged)
    def velEstimateText(self):
        return self._vel_est_text

    @Property(str, notify=estimatesChanged)
    def posEstimateText(self):
        return self._pos_est_text

    @Property(int, notify=analogChanged)
    def analogGpio(self):
        return self._analog_gpio

    @Property(list, constant=True)
    def analogGpios(self):
        return list(ANALOG_GPIOS)

    @Property(str, notify=analogChanged)
    def analogTarget(self):
        """Target endpoint mode ('Velocity', 'Position', 'Torque') or 'Disabled'."""
        return self._analog_target

    @Property(float, notify=analogChanged)
    def analogValue(self):
        return self._analog_value

    @Property(float, notify=analogChanged)
    def analogMin(self):
        return self._analog_min

    @Property(float, notify=analogChanged)
    def analogMax(self):
        return self._analog_max

    @Property(bool, notify=analogChanged)
    def analogDeadbandAvailable(self):
        return self._analog_deadband_available

    @Property(bool, notify=analogChanged)
    def analogDeadbandEnable(self):
        return self._analog_deadband_enable

    @Property(float, notify=analogChanged)
    def analogDeadbandStart(self):
        return self._analog_deadband_start

    @Property(float, notify=analogChanged)
    def analogDeadbandEnd(self):
        return self._analog_deadband_end

    @Property(float, notify=analogChanged)
    def analogDeadbandLevel(self):
        return self._analog_deadband_level

    @Property(bool, notify=analogChanged)
    def analogDeadbandIdle(self):
        return self._analog_deadband_idle

    # -- connection lifecycle ------------------------------------------

    @Slot()
    def connectOdrive(self):
        """Start connecting to an ODrive in a background thread."""
        if self._connecting:
            return
        if self.odrive is not None:
            self.odrive = None
        self._connecting = True
        self._last_synced_mode = None
        self.status_backend.set_conn("\u25cf Connecting\u2026", "orange", False)
        threading.Thread(target=self._connect_worker, daemon=True).start()

    def _connect_worker(self):
        try:
            # 0.5.7-hardened: once a serial is known, reconnects target that
            # exact device instead of silently adopting whichever ODrive shows up.
            odrv = odrive.find_any(serial_number=self._target_serial)
        except DEVICE_EXCEPTIONS as e:
            msg = str(e)
            QTimer.singleShot(0, self, lambda: self._on_connect_failed(msg))
            return
        QTimer.singleShot(0, self, lambda: self._on_connected(odrv))

    def _on_connected(self, odrv):
        self.odrive = odrv
        self._connecting = False

        # 0.5.7-hardened: remember this device; reconnects target it by serial.
        try:
            self._target_serial = odrive.get_serial_number_str_sync(odrv)
        except DEVICE_EXCEPTIONS:
            pass  # stay untargeted; any-device reconnect is the fallback

        try:
            if self.odrive._on_lost.done():
                logger.warning(
                    "on_connected: device already lost during setup, reconnecting"
                )
                QTimer.singleShot(0, self.connectOdrive)
                return
            self.odrive._on_lost.add_done_callback(self._on_device_lost)
        except DEVICE_EXCEPTIONS as e:
            logger.warning("on_connected: _on_lost registration failed: %s", e)

        self._sync_setpoint_from_device()
        self._analog_gpio = self._detect_active_gpio()
        self._load_analog_bounds()
        self._sync_analog()
        axis = self._axis()
        self._armed = bool(
            getattr(getattr(axis, "config", None), "startup_closed_loop_control", False)
        )
        self.status_backend.set_conn("\u25cf Online", "green", True)
        self._config_model.reset()  # re-root the Config Browser (§2.4)
        # Input-mode model is built by the first poll tick (<100 ms away) via
        # _sync_mode -> _input_mode_model_for; nothing extra to do here.
        self.logEvent("CONNECT", "online (axis0 wired)")
        logger.info("Connected to ODrive")

    def _on_device_lost(self, _future):
        logger.warning(
            "on_device_lost: connection lost (thread=%s)",
            threading.current_thread().name,
        )
        # Drop immediately so poll ticks stop hitting the dead object
        # (the reconnect itself must run on the Qt thread).
        self.odrive = None
        self.logEvent("CONNECT", "device lost, reconnecting")
        QTimer.singleShot(0, self, self.connectOdrive)

    def _on_connect_failed(self, msg):
        self._connecting = False
        logger.warning(
            "on_connect_failed: %s (retry in %d ms)", msg, RECONNECT_RETRY_DELAY_MS
        )
        self.status_backend.set_conn("\u25cf Offline (retrying)", "red", False)
        QTimer.singleShot(RECONNECT_RETRY_DELAY_MS, self.connectOdrive)

    # -- helpers -------------------------------------------------------

    def _axis(self):
        """Axis 0 reference, or None while disconnected / link gone."""
        if self.odrive is None:
            return None
        try:
            return self.odrive.axis0
        except DEVICE_EXCEPTIONS:
            return None

    # -- control handlers ----------------------------------------------

    @Slot()
    def run(self):
        axis = self._axis()
        if axis is None:
            return
        try:
            axis.requested_state = AXIS_STATE_CLOSED_LOOP_CONTROL
        except DEVICE_EXCEPTIONS as e:
            self.logEvent("STATE", f"failed to run: {e}")
            return
        self._armed = True
        self._sync_setpoint_from_device()
        self.logEvent("STATE", "Run: Closed Loop")

    @Slot()
    def stop(self):
        axis = self._axis()
        if axis is None:
            return
        try:
            axis.requested_state = AXIS_STATE_IDLE
        except DEVICE_EXCEPTIONS as e:
            self.logEvent("STATE", f"failed to stop: {e}")
            return
        self._armed = False
        self.logEvent("STATE", "Stop: Idle")

    @Slot(str)
    def startState(self, name):
        axis = self._axis()
        if axis is None:
            return
        state = STATE_MAP.get(name)
        if state is None:
            return
        try:
            axis.requested_state = state
        except DEVICE_EXCEPTIONS as e:
            self.logEvent("STATE", f"failed to start {name}: {e}")
            return
        self.logEvent("STATE", f"Start: {name}")

    @Slot(str)
    def setMode(self, name):
        axis = self._axis()
        if axis is None:
            return
        new_mode = MODE_VALUES.get(name)
        if new_mode is None:
            return
        controller = axis.controller
        try:
            controller.config.control_mode = new_mode
            self._active_mode_enum = new_mode
            self._write_active_setpoint()  # seed the new mode's input
            self._steer_input_mode(controller, new_mode)
            self._sync_mode()
            self.logEvent("MODE", f"control mode -> {name}")
        except DEVICE_EXCEPTIONS as e:
            self.logEvent("MODE", f"failed to set mode {name}: {e}")

    def _steer_input_mode(self, controller, control_mode):
        """Explicit user action: on a control-mode switch, move the device to
        the new mode's default input mode unless it already runs one of that
        mode's ramp/filter modes. Passthrough counts as "not set" here — it is
        a valid choice to keep while browsing, but a mode change overrides it."""
        allowed = MODES_BY_CONTROL.get(control_mode)
        cur = controller.config.input_mode
        if allowed and (cur not in allowed or cur == INPUT_MODE_PASSTHROUGH):
            default = DEFAULT_BY_CONTROL.get(control_mode, allowed[0])
            try:
                controller.config.input_mode = default
                self.logEvent(
                    "MODE",
                    f"input mode -> {INPUT_MODES[default]} "
                    f"(default for {MODE_NAMES[control_mode]})",
                )
            except DEVICE_EXCEPTIONS as e:
                logger.warning("failed to default input_mode: %s", e)

    @Slot(float)
    def setActiveSetpoint(self, value):
        """Store the visible setpoint locally (no device write)."""
        self._setpoints[self._active_mode_enum] = value
        self.setpointChanged.emit()

    @Slot()
    def applySetpoint(self):
        """Write the active mode's stored setpoint to the device."""
        self._write_active_setpoint()
        self._sync_setpoint_from_device()

    def _write_active_setpoint(self):
        axis = self._axis()
        if axis is None:
            return
        attr, label = _SETPOINT_TARGETS[self._active_mode_enum]
        value = self._setpoints[self._active_mode_enum]
        try:
            setattr(axis.controller, attr, value)
            self.logEvent("SETPOINT", f"{label} setpoint -> {value}")
        except DEVICE_EXCEPTIONS as e:
            self.logEvent("SETPOINT", f"failed to apply setpoint: {e}")

    def _sync_setpoint_from_device(self):
        """Pull the device's current input setpoint into the stored values."""
        axis = self._axis()
        if axis is None:
            return
        controller = axis.controller
        try:
            mode = controller.config.control_mode
            if (
                mode == CONTROL_MODE_VELOCITY_CONTROL
                and controller.input_vel is not None
            ):
                self._setpoints[CONTROL_MODE_VELOCITY_CONTROL] = float(
                    controller.input_vel
                )
            elif (
                mode == CONTROL_MODE_TORQUE_CONTROL
                and controller.input_torque is not None
            ):
                self._setpoints[CONTROL_MODE_TORQUE_CONTROL] = float(
                    controller.input_torque
                )
            elif (
                mode == CONTROL_MODE_POSITION_CONTROL
                and controller.input_pos is not None
            ):
                self._setpoints[CONTROL_MODE_POSITION_CONTROL] = float(
                    controller.input_pos
                )
        except DEVICE_EXCEPTIONS as e:
            logger.debug("sync setpoint failed: %s", e)
            return
        self.setpointChanged.emit()

    # -- input-mode selection ------------------------------------------

    def _input_mode_model_for(self, control_mode):
        axis = self._axis()
        controller = axis.controller if axis is not None else None
        if control_mode is None or controller is None:
            self._input_modes = []
            self._input_mode_values = []
            self._input_mode_index = -1
            self.inputModeModelChanged.emit()
            return
        allowed = MODES_BY_CONTROL.get(control_mode, [INPUT_MODE_PASSTHROUGH])
        # No local guard: callers wrap this in their own device-failure handling
        # (_sync_mode runs inside the poll's try, setMode inside its slot handler).
        cur = controller.config.input_mode
        self._input_mode_values = list(allowed)
        self._input_modes = [INPUT_MODES[v] for v in allowed]
        if cur in allowed:
            self._input_mode_index = allowed.index(cur)
        else:
            # Device runs a mode we don't list (e.g. set via CLI) — show it
            # as an extra, informational entry. Selecting it is a no-op.
            label = f"unknown (0x{cur:X})"
            self._input_modes.append(label)
            self._input_mode_index = len(self._input_mode_values)
        self.inputModeModelChanged.emit()
        # Model-building only: never write input_mode here — connect/poll are
        # read paths (ARCHITECTURE.md forbids implicit writes). Defaulting to
        # the control mode's standard input happens in setMode(), which is an
        # explicit user action.

    @Slot(int)
    def setInputMode(self, index):
        axis = self._axis()
        if axis is None or not (0 <= index < len(self._input_mode_values)):
            return
        value = self._input_mode_values[index]
        try:
            axis.controller.config.input_mode = value
            self._input_mode_index = index
            self.inputModeModelChanged.emit()
            self.logEvent("MODE", f"input mode -> {self._input_modes[index]}")
        except DEVICE_EXCEPTIONS as e:
            self.logEvent("MODE", f"failed to set input mode: {e}")

    # -- analog input mapping ------------------------------------------
    # Maps an ADC reading to a controller input endpoint via
    # odrv.config.gpio{N}_analog_mapping (firmware low_level.cpp
    # analog_polling_thread). While enabled, the mapped setpoint row is
    # driven by the input: the GUI disables it and mirrors the live value.

    def _detect_active_gpio(self):
        if self.odrive is None:
            return 3
        for gpio in ANALOG_GPIOS:
            mapping = getattr(self.odrive.config, f"gpio{gpio}_analog_mapping", None)
            mode = getattr(self.odrive.config, f"gpio{gpio}_mode", None)
            if mode == GPIO_MODE_ANALOG_IN or (
                mapping and getattr(mapping, "endpoint", None) is not None
            ):
                return gpio
        return 3

    def _analog_mapping(self):
        return (
            getattr(
                self.odrive.config,
                f"gpio{self._analog_gpio}_analog_mapping",
                None,
            )
            if self.odrive
            else None
        )

    def _analog_match_mode(self, endpoint):
        """Control mode whose input endpoint `endpoint` refers to, else None."""
        axis = self._axis()
        if endpoint is None or axis is None:
            return None
        return next(
            (
                m
                for m, a in _ANALOG_ENDPOINT_ATTR.items()
                if getattr(axis.controller, a, None) is endpoint
            ),
            None,
        )

    def _set_analog_state(self, target, value):
        if target == self._analog_target and value == self._analog_value:
            return
        self._analog_target = target
        self._analog_value = value
        self.analogChanged.emit()

    def _sync_analog(self):
        """Poll: which setpoint the analog GPIO drives + the live mapped value."""
        mapping = self._analog_mapping()
        axis = self._axis()
        if mapping is None or axis is None:
            self._set_analog_state("Disabled", 0.0)
            return
        try:
            endpoint = mapping.endpoint
        except (DEVICE_EXCEPTIONS, KeyError, AttributeError):
            endpoint = None
        mode = self._analog_match_mode(endpoint)
        target = MODE_NAMES.get(mode, "Disabled")
        value = 0.0
        if mode is not None:
            raw = getattr(axis.controller, _SETPOINT_TARGETS[mode][0], 0.0)
            value = float(raw) if raw is not None else 0.0
        self._set_analog_state(target, value)

    def _load_analog_bounds(self):
        mapping = self._analog_mapping()
        if mapping is None:
            return
        try:
            self._analog_min = float(mapping.min)
            self._analog_max = float(mapping.max)
            if hasattr(mapping, "deadband_enable"):
                self._analog_deadband_available = True
                self._analog_deadband_enable = bool(mapping.deadband_enable)
                self._analog_deadband_start = float(mapping.deadband_start)
                self._analog_deadband_end = float(mapping.deadband_end)
                self._analog_deadband_level = float(mapping.deadband_level)
                self._analog_deadband_idle = bool(
                    getattr(mapping, "deadband_idle", False)
                )
            else:
                self._analog_deadband_available = False
        except DEVICE_EXCEPTIONS:
            return
        self.analogChanged.emit()

    def _write_analog_field(self, attr, value):
        mapping = self._analog_mapping()
        if mapping is None or not hasattr(mapping, attr):
            return
        try:
            setattr(mapping, attr, value)
            setattr(self, f"_analog_{attr}", value)
            self.logEvent("ANALOG", f"analog {attr} -> {value}")
            self.analogChanged.emit()
        except DEVICE_EXCEPTIONS as e:
            self.logEvent("ANALOG", f"failed to set analog {attr}: {e}")

    @Slot(int)
    def setAnalogGpio(self, gpio):
        if gpio not in ANALOG_GPIOS or gpio == self._analog_gpio:
            return
        old_gpio = self._analog_gpio
        self._analog_gpio = gpio
        if self.odrive is None:
            self.analogChanged.emit()
            return
        old_map = getattr(self.odrive.config, f"gpio{old_gpio}_analog_mapping", None)
        new_map = getattr(self.odrive.config, f"gpio{gpio}_analog_mapping", None)
        try:
            if old_map and new_map and old_map.endpoint is not None:
                new_map.endpoint = old_map.endpoint
                new_map.min = old_map.min
                new_map.max = old_map.max
                if hasattr(old_map, "deadband_enable") and hasattr(
                    new_map, "deadband_enable"
                ):
                    new_map.deadband_enable = old_map.deadband_enable
                    new_map.deadband_start = old_map.deadband_start
                    new_map.deadband_end = old_map.deadband_end
                    new_map.deadband_level = old_map.deadband_level
                    if hasattr(old_map, "deadband_idle") and hasattr(
                        new_map, "deadband_idle"
                    ):
                        new_map.deadband_idle = old_map.deadband_idle
                old_map.endpoint = None
                setattr(self.odrive.config, f"gpio{old_gpio}_mode", GPIO_MODE_DIGITAL)
                setattr(self.odrive.config, f"gpio{gpio}_mode", GPIO_MODE_ANALOG_IN)
                self.logEvent("ANALOG", f"analog input pin moved to gpio{gpio}")
            else:
                self.logEvent("ANALOG", f"analog input pin -> gpio{gpio}")
            self._load_analog_bounds()
            self._sync_analog()
        except DEVICE_EXCEPTIONS as e:
            self.logEvent("ANALOG", f"failed to switch analog gpio: {e}")

    @Slot(str)
    def setAnalogTarget(self, target):
        """Set analog mapping target: 'Disabled', 'Velocity', etc."""
        axis = self._axis()
        mapping = self._analog_mapping()
        if axis is None or mapping is None:
            return
        mode = MODE_VALUES.get(target)
        try:
            if mode is None:
                mapping.endpoint = None
                setattr(
                    self.odrive.config,
                    f"gpio{self._analog_gpio}_mode",
                    GPIO_MODE_DIGITAL,
                )
                self.logEvent(
                    "ANALOG", f"analog input disabled (gpio{self._analog_gpio})"
                )
                self._sync_setpoint_from_device()
            else:
                setattr(
                    self.odrive.config,
                    f"gpio{self._analog_gpio}_mode",
                    GPIO_MODE_ANALOG_IN,
                )
                mapping.endpoint = getattr(axis.controller, _ANALOG_ENDPOINT_ATTR[mode])
                self.logEvent(
                    "ANALOG",
                    f"analog input -> {target} (gpio{self._analog_gpio})",
                )
            self._sync_analog()
        except DEVICE_EXCEPTIONS as e:
            self.logEvent("ANALOG", f"failed to set analog mapping: {e}")

    @Slot(float)
    def setAnalogMin(self, value):
        self._write_analog_field("min", value)

    @Slot(float)
    def setAnalogMax(self, value):
        self._write_analog_field("max", value)

    @Slot(bool)
    def setAnalogDeadbandEnable(self, value):
        self._write_analog_field("deadband_enable", value)

    @Slot(float)
    def setAnalogDeadbandStart(self, value):
        self._write_analog_field("deadband_start", value)

    @Slot(float)
    def setAnalogDeadbandEnd(self, value):
        self._write_analog_field("deadband_end", value)

    @Slot(float)
    def setAnalogDeadbandLevel(self, value):
        self._write_analog_field("deadband_level", value)

    @Slot(bool)
    def setAnalogDeadbandIdle(self, value):
        self._write_analog_field("deadband_idle", value)

    # -- mode sync / gating --------------------------------------------

    def _sync_mode(self):
        axis = self._axis()
        if axis is None:
            return
        actual = axis.controller.config.control_mode
        if actual == self._last_synced_mode:
            return
        self._last_synced_mode = actual
        self._active_mode_enum = actual
        self._current_mode = _MODE_ORDER.index(actual)
        self.modeChanged.emit()
        self._input_mode_model_for(actual)

    def _sync_closed_loop(self):
        axis = self._axis()
        running = (
            axis is not None and axis.current_state == AXIS_STATE_CLOSED_LOOP_CONTROL
        )
        if running != self._closed_loop:
            self._closed_loop = running
            self.closedLoopChanged.emit()
        idle = axis is not None and axis.current_state == AXIS_STATE_IDLE
        if idle != self._axis_idle:
            self._axis_idle = idle
            self.idleChanged.emit()  # drives the limits tabs' disabled state

    # -- settings write-back (2.5.3) -----------------------------------

    @Slot(str, str, float, result=bool)
    def setConfig(self, base, attr, value):
        """Write a settings value: base is motor/controller/axis/odrive.

        Returns True iff the device accepted the write, so a failed write
        can be resynced from the device immediately (SpinRow/CheckRow).
        Not IDLE-gated here: the Electrical/Mechanical tabs disable
        themselves while the axis runs (QML enabled cascade), so their rows
        can't fire. Only the Config Browser's long-lived editor needs the
        commit-time re-check (writeBrowserValue).
        """
        obj = self._config_obj(base)
        if obj is None or not hasattr(obj.config, attr):
            return False
        try:
            setattr(obj.config, attr, value)
        except DEVICE_EXCEPTIONS as e:
            self.logEvent("WRITE", f"failed to set {attr}: {e}")
            return False
        self.logEvent("WRITE", f"{base}.{attr} -> {value}")
        return True

    def _ensure_idle_for_write(self, label):
        """Commit-time IDLE gate: True iff the axis is IDLE right now."""
        axis = self._axis()
        try:
            idle = axis is not None and axis.current_state == AXIS_STATE_IDLE
        except DEVICE_EXCEPTIONS:
            idle = False
        if not idle:
            self.logEvent("WRITE", f"refused {label} (axis not IDLE)")
        return idle

    @Slot(str, str, result=bool)
    def hasConfig(self, base, attr):
        obj = self._config_obj(base)
        return obj is not None and hasattr(obj.config, attr)

    @Slot(str, str, result=float)
    def getConfig(self, base, attr):
        obj = self._config_obj(base)
        if obj is None or not hasattr(obj.config, attr):
            return 0.0
        try:
            value = getattr(obj.config, attr)
            return float(value) if value is not None else 0.0
        except DEVICE_EXCEPTIONS:
            return 0.0

    def _config_obj(self, base):
        axis = self._axis()
        if axis is None:
            return None
        if base == "odrive":
            return self.odrive
        return getattr(axis, base, None)

    # -- config browser (Plan §2.4) --------------------------------------

    @Property(QObject, constant=True)
    def browserModel(self):
        return self._config_model

    @staticmethod
    def _parse_like(text, cur):
        """Parse `text` as the same type as `cur` (bool/int/float); raises
        ValueError. Ints accept 0x hex for odrivetool parity (enum leaves)."""
        s = text.strip()
        if isinstance(cur, bool):
            low = s.lower()
            if low in ("true", "1"):
                return True
            if low in ("false", "0"):
                return False
            raise ValueError(s)
        if isinstance(cur, int):
            return int(s, 0)
        value = float(s)
        if not math.isfinite(value):  # nan/inf must never reach the device
            raise ValueError(s)
        return value

    @Slot(str, str, result=bool)
    def writeBrowserValue(self, path, text):
        """Commit a Config Browser edit: type-checked against the current
        value, IDLE-gated with commit-time re-check (§2.4)."""
        target = self._config_model.resolve(path)
        if target is None:
            self.logEvent("WRITE", f"refused {path}: not an editable config value")
            return False
        container, attr, cur = target
        try:
            value = self._parse_like(text, cur)
        except ValueError:
            self.logEvent(
                "WRITE", f"refused {path}: '{text}' is not {type(cur).__name__}"
            )
            return False
        if not self._ensure_idle_for_write(path):
            return False
        try:
            setattr(container, attr, value)
        except DEVICE_EXCEPTIONS as e:
            self.logEvent("WRITE", f"failed to write {path}: {e}")
            return False
        self.logEvent("WRITE", f"{path} -> {value}")
        self._config_model.invalidate(path, value)
        return True

    # -- device menu actions -------------------------------------------

    @Slot()
    def saveConfig(self):
        if self.odrive is None:
            return
        try:
            self.odrive.save_configuration()
            self.logEvent("CFG", "saved config to NVM")
        except DEVICE_EXCEPTIONS as e:
            QMessageBox.critical(
                None, "Save Error", f"Failed to save configuration: {e}"
            )

    @Slot()
    def exportConfig(self):
        if self.odrive is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            None, "Export Configuration", "", "JSON Files (*.json);;All Files (*)"
        )
        if not path:
            return
        try:
            odrive.configuration.backup_config(self.odrive, path, logger)
            self.logEvent("CFG", f"exported config to {path}")
        except DEVICE_EXCEPTIONS as e:
            QMessageBox.critical(
                None, "Export Error", f"Failed to export configuration: {e}"
            )

    @Slot()
    def importConfig(self):
        if self.odrive is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            None, "Import Configuration", "", "JSON Files (*.json);;All Files (*)"
        )
        if not path:
            return
        reply = QMessageBox.question(
            None,
            "Confirm Import",
            "Importing will overwrite the device configuration and reboot. Continue?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            odrive.configuration.restore_config(self.odrive, path, logger)
            self.logEvent("CFG", f"imported config from {path} (rebooting)")
        except DEVICE_EXCEPTIONS as e:
            QMessageBox.critical(
                None, "Import Error", f"Failed to import configuration: {e}"
            )

    @Slot()
    def reboot(self):
        if self.odrive is None:
            return
        reply = QMessageBox.question(
            None,
            "Confirm Reboot",
            "Reboot the ODrive device?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            if not hasattr(self.odrive, "reboot"):
                QMessageBox.critical(
                    None,
                    "Reboot Error",
                    "This firmware does not expose a reboot command.",
                )
                return
            self.status_backend.set_conn("\u25cf Rebooting\u2026", "orange", False)
            self.odrive.reboot()
            self.logEvent("CFG", "device rebooting")
        except DEVICE_EXCEPTIONS as e:
            QMessageBox.critical(None, "Reboot Error", f"Failed to reboot: {e}")

    @Slot(bool)
    def setVerbose(self, checked):
        logging.getLogger().setLevel(logging.DEBUG if checked else logging.INFO)
        if checked != self._verbose:
            self._verbose = checked
            self.verboseChanged.emit()
        self.logEvent("APP", f"verbose logging {'enabled' if checked else 'disabled'}")

    # -- QML dialog data ----------------------------------------------

    @Slot(result=str)
    def deviceInfoText(self):
        if self.odrive is None:
            return "Not connected"
        try:
            serial = odrive.get_serial_number_str_sync(self.odrive)
            parts = (
                self.odrive.fw_version_major,
                self.odrive.fw_version_minor,
                self.odrive.fw_version_revision,
            )
            hw_major, hw_minor, hw_variant = (
                self.odrive.hw_version_major,
                self.odrive.hw_version_minor,
                self.odrive.hw_version_variant,
            )
        except DEVICE_EXCEPTIONS:
            return "Device lost while reading info"
        fw = ".".join(str(x) for x in parts) if None not in parts else "unknown"
        hw = "unknown"
        if hw_major is not None and hw_minor is not None:
            hw = f"v{hw_major}.{hw_minor}"
            if hw_variant:
                hw += f"-{hw_variant}V"
        return f"Serial number: {serial}\nFirmware: {fw}\nHardware: {hw}"

    # Live content for the QML error / event-log dialogs.
    @Property(str, notify=errorsChanged)
    def errorsText(self):
        return (
            self.status_backend.rendered_errors
            or "(no current-error snapshot — device not connected)"
        )

    @Property(str, notify=logUpdated)
    def logText(self):
        return format_log(self.event_log)

    @Slot()
    def clearErrors(self):
        if self.odrive is None:
            return
        try:
            self.odrive.clear_errors()
            self.logEvent("CLEAR", "cleared all errors")
        except DEVICE_EXCEPTIONS as e:
            self.logEvent("CLEAR", f"failed to clear errors: {e}")

    @Slot()
    def exportLog(self):
        path, _ = QFileDialog.getSaveFileName(
            None, "Export Log", "odrive_log.txt", "Text Files (*.txt);;All Files (*)"
        )
        if not path:
            return
        try:
            with open(path, "w") as f:
                f.write(self.logText)
        except OSError as e:
            logger.warning("export log failed: %s", e)

    # -- event log -----------------------------------------------------

    def logEvent(self, category, message):
        self.event_log.append(LogEntry(time.time(), category, message))
        self.logUpdated.emit()
        logger.debug("[%s] %s", category, message)

    # -- readings poll -------------------------------------------------

    def updateReadings(self):
        """Single guarded poll: every display read happens here.

        Any transport failure drops the link and auto-reconnects, so nothing
        below this method needs its own handling for device reads.
        """
        if self.odrive is None:
            return
        try:
            self._sync_mode()
            self._sync_closed_loop()
            axis = self._axis()
            if axis is not None:
                if axis.current_state == AXIS_STATE_CLOSED_LOOP_CONTROL:
                    self._armed = True
                elif getattr(axis, "error", 0) != 0:
                    self._armed = False
            if self.status_backend.update_readings(
                self.odrive,
                axis,
                self.logEvent,
                self._armed,
                self._analog_deadband_idle,
            ):
                self.errorsChanged.emit()
            self._read_estimates()
            self._sync_analog()
        except DEVICE_EXCEPTIONS as e:
            # 0.5.7-hardened: all "link gone" paths raise ObjectLostError, so
            # this catch is uniform. AttributeError/TypeError here would be a
            # bug or a missing endpoint and deliberately surfaces.
            # ponytail: widen to an N-strike counter if USB proves flaky.
            self._drop_link("updateReadings", e)

    def plotTick(self):
        if self.odrive is None:
            return
        try:
            self._sample_plot()
        except DEVICE_EXCEPTIONS as e:
            self._drop_link("plotTick", e)

    def _drop_link(self, where, exc):
        """Shared transport-failure path: drop the link and auto-reconnect.

        One line with type+message, no traceback: on_device_lost already logs
        the cause, and at plot tick rate this fires within milliseconds of
        any disconnect — a stack trace per disconnect is pure noise.
        """
        if self.odrive is None:
            return  # already dropped by an earlier site in this unwind
        logger.warning(
            "%s: read failed (%s: %s), reconnecting", where, type(exc).__name__, exc
        )
        self.status_backend.set_conn("\u25cf Offline", "gray", False)
        self.odrive = None
        self.connectOdrive()

    # -- live plot (Plan §3.1) -----------------------------------------

    @Slot()
    def showPlot(self):
        """Open (or raise) the native pyqtgraph plot window."""
        if self._plot_window is None:
            try:
                self._plot_window = PlotWindow(self)
            except ImportError:
                self.logEvent(
                    "ERROR", "pyqtgraph not installed - live plot unavailable"
                )
                logger.warning("live plot requested but pyqtgraph is missing")
                return
        self._plot_window.show()
        self._plot_window.raise_()
        self._plot_window.activateWindow()

    def setActiveChannels(self, keys):
        """Set of channel keys the plot window wants sampled (checkbox +
        control-mode gated). Anything beyond ACTIVE_CHANNEL_LIMIT is logged
        and dropped, not silently over-subscribed on USB."""
        keys = list(keys)
        if len(keys) > ACTIVE_CHANNEL_LIMIT:
            self.logEvent(
                "ERROR",
                f"live plot: max {ACTIVE_CHANNEL_LIMIT} channels active,"
                f" ignoring extras ({len(keys)} requested)",
            )
            keys = keys[:ACTIVE_CHANNEL_LIMIT]
        self._active_channels = set(keys)

    def _sample_plot(self):
        """Append one plot sample per plot tick; missing endpoints
        become NaN. Only channels the plot window marked active are read —
        unchecked/mode-gated ones stay NaN in buffer and CSV. Runs regardless
        of the window being open, so opening the plot later already shows
        history. getattr-gated like everything else
        (AttributeError here would falsely trigger a reconnect).
        """
        axis = self._axis()
        if axis is None:
            return
        self.samples.append(
            time.time(),
            {
                key: read(axis, self.odrive)
                for key, read in _PLOT_READERS.items()
                if key in self._active_channels
            },
        )

    def _read_estimates(self):
        axis = self._axis()
        if axis is None:
            return
        vel = axis.encoder.vel_estimate
        new_vel = f"est: {vel:.3f} rps"
        pos = axis.encoder.pos_estimate
        rng = self._position_circular_range()
        if rng is not None:
            pos = pos % rng
        new_pos = f"est: {pos:.4f} rev"
        if new_vel != self._vel_est_text or new_pos != self._pos_est_text:
            self._vel_est_text = new_vel
            self._pos_est_text = new_pos
            self.estimatesChanged.emit()

    def plotMode(self):
        """Raw control-mode enum value the live plot's mode gating matches on."""
        return self._active_mode_enum

    def _position_circular_range(self):
        axis = self._axis()
        if axis is None:
            return None
        controller = axis.controller
        if controller.config.circular_setpoints:
            rng = controller.config.circular_setpoint_range
            if rng and float(rng) > 0:
                return float(rng)
        return None
