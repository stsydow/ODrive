import logging

import odrive.enums
from PySide6.QtCore import Property, QObject, Signal

from errors import format_current, read_error_report

logger = logging.getLogger(__name__)

AXIS_STATE_NAMES = {
    v: n.replace("AXIS_STATE_", "")
    for n, v in vars(odrive.enums).items()
    if n.startswith("AXIS_STATE_")
}

STATE_MAP = {
    "Full Calibration Sequence": odrive.enums.AXIS_STATE_FULL_CALIBRATION_SEQUENCE,
    "Motor Calibration": odrive.enums.AXIS_STATE_MOTOR_CALIBRATION,
    "Encoder Index Search": odrive.enums.AXIS_STATE_ENCODER_INDEX_SEARCH,
    "Encoder Offset Calibration": odrive.enums.AXIS_STATE_ENCODER_OFFSET_CALIBRATION,
    "Encoder Direction Find": odrive.enums.AXIS_STATE_ENCODER_DIR_FIND,
    "Homing": odrive.enums.AXIS_STATE_HOMING,
    "Lock-In Spin": odrive.enums.AXIS_STATE_LOCKIN_SPIN,
}

# Reverse lookup for calibration-state labels (value -> "Full Calibration Sequence" ...)
_CALIB_LABELS = {v: k for k, v in STATE_MAP.items()}


def decode_error_summary(report) -> str:
    """Return human-readable decoded error names without module prefixes."""
    if not report.any:
        return "OK"
    items = []
    for s in report.sources:
        if not s.value:
            continue
        prefix = f"{s.name.upper()}_ERROR_"
        for err in s.errors:
            if err.startswith(prefix):
                err = err[len(prefix) :]
            elif err.startswith("ODRIVE_ERROR_"):
                err = err[len("ODRIVE_ERROR_") :]
            items.append(err)
        if not s.errors:
            items.append(f"{s.name}: 0x{s.value:X}")
    return " | ".join(items) if items else f"0x{sum(s.value for s in report.sources):X}"


def _state_display(value):
    if value in (odrive.enums.AXIS_STATE_IDLE, odrive.enums.AXIS_STATE_UNDEFINED):
        return "Idle"
    if value == odrive.enums.AXIS_STATE_CLOSED_LOOP_CONTROL:
        return "Control Loop"
    if value == odrive.enums.AXIS_STATE_STARTUP_SEQUENCE:
        return "Startup"
    if value == odrive.enums.AXIS_STATE_HOMING:
        return "Homing"
    short = AXIS_STATE_NAMES.get(value)
    if short and any(
        k in short for k in ("CALIBRATION", "DIR_FIND", "INDEX_SEARCH", "LOCKIN")
    ):
        return "Calibration: " + _CALIB_LABELS.get(
            value, short.replace("_", " ").title()
        )
    return short or str(value)


class StatusBackend(QObject):
    connChanged = Signal()
    stateChanged = Signal()
    errorsChanged = Signal()
    powerChanged = Signal()
    statusChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._conn_text = "\u25cf Offline"
        self._conn_color = "gray"
        self._state_text = "--"
        self._error_text = "Err: OK"
        self._error_color = "green"
        self._vbus_text = "-- V"
        self._power_text = "-- W"
        self._status_text = "\u25cf Offline"
        self._status_color = "gray"
        self._has_error = False
        self._error_summary = "OK"
        self._axis_state = None
        self._armed = False
        self._deadband_idle = False
        self._connected = False
        self._last_error_key = None
        self.rendered_errors = None  # full rendered error text for the dialog

    @Property(str, notify=statusChanged)
    def statusText(self):
        return self._status_text

    @Property(str, notify=statusChanged)
    def statusColor(self):
        return self._status_color

    @Property(bool, notify=statusChanged)
    def hasError(self):
        return self._has_error

    @Property(str, notify=connChanged)
    def connText(self):
        return self._conn_text

    @Property(str, notify=connChanged)
    def connColor(self):
        return self._conn_color

    @Property(bool, notify=connChanged)
    def connected(self):
        return self._connected

    @Property(str, notify=stateChanged)
    def stateText(self):
        return self._state_text

    @Property(str, notify=errorsChanged)
    def errorText(self):
        return self._error_text

    @Property(str, notify=errorsChanged)
    def errorColor(self):
        return self._error_color

    @Property(str, notify=powerChanged)
    def vbusText(self):
        return self._vbus_text

    @Property(str, notify=powerChanged)
    def powerText(self):
        return self._power_text

    def set_conn(self, text, color, connected):
        changed = (
            self._conn_text != text
            or self._conn_color != color
            or self._connected != connected
        )
        self._conn_text = text
        self._conn_color = color
        self._connected = connected
        if changed:
            self.connChanged.emit()
        self._recompute_merged_status()

    def update_readings(
        self,
        odrv,
        axis,
        log_event_func,
        armed: bool = False,
        deadband_idle: bool = False,
    ) -> bool:
        """Refresh footer state; returns True if the rendered error text changed."""
        if odrv is None:
            return False

        self._armed = armed
        self._deadband_idle = deadband_idle
        self._update_state(axis)
        self._update_voltages(odrv)
        err_changed = self._update_errors(odrv, log_event_func)
        self._recompute_merged_status()
        return err_changed

    def _update_state(self, axis):
        st = axis.current_state if axis is not None else None
        self._axis_state = st
        if st is not None:
            text = _state_display(st)
            if text != self._state_text:
                self._state_text = text
                self.stateChanged.emit()

    def _update_voltages(self, odrv):
        # Transport failures propagate to the caller (the poll's guarded
        # fetch in GuiBackend.updateReadings) instead of being swallowed here.
        vbus = odrv.vbus_voltage
        new_v = f"{vbus:.1f} V"
        new_p = f"{vbus * odrv.ibus:.1f} W"
        if new_v != self._vbus_text or new_p != self._power_text:
            self._vbus_text = new_v
            self._power_text = new_p
            self.powerChanged.emit()

    def _update_errors(self, odrv, log_event_func) -> bool:
        report = read_error_report(odrv)
        key = tuple(sorted((s.name, tuple(s.errors)) for s in report.sources))
        if key and key != self._last_error_key:
            detail = "; ".join(
                f"{s.name}: {' | '.join(s.errors) if s.errors else f'0x{s.value:X}'}"
                for s in report.sources
            )
            log_event_func("ERROR", f"axis errors -> {detail}")
            self._last_error_key = key
        elif not key and self._last_error_key:
            log_event_func("CLEAR", "errors cleared")
            self._last_error_key = None

        if report.any:
            self._error_summary = decode_error_summary(report)
            new_text = f"Err: {self._error_summary}"
            new_color = "red"
            self._has_error = True
        else:
            self._error_summary = "OK"
            new_text = "Err: OK"
            new_color = "green"
            self._has_error = False

        if new_text != self._error_text or new_color != self._error_color:
            self._error_text = new_text
            self._error_color = new_color
            self.errorsChanged.emit()

        rendered = format_current(report)
        if rendered != self.rendered_errors:
            self.rendered_errors = rendered
            return True
        return False

    def _recompute_merged_status(self):
        if not self._connected:
            text = self._conn_text
            color = self._conn_color
        elif self._has_error:
            text = f"\u25cf Error: {self._error_summary}"
            color = "red"
        elif self._axis_state == odrive.enums.AXIS_STATE_CLOSED_LOOP_CONTROL:
            text = "\u25cf Running"
            color = "#00cc44"
        elif self._axis_state in (
            odrive.enums.AXIS_STATE_IDLE,
            odrive.enums.AXIS_STATE_UNDEFINED,
            None,
        ):
            if self._deadband_idle:
                text = (
                    "\u25cf Idle (Armed)" if self._armed else "\u25cf Idle (Disarmed)"
                )
                color = "#ffaa00" if self._armed else "gray"
            else:
                text = "\u25cf Idle"
                color = "gray"
        elif self._axis_state == odrive.enums.AXIS_STATE_STARTUP_SEQUENCE:
            text = "\u25cf Startup"
            color = "#3399ff"
        elif self._axis_state == odrive.enums.AXIS_STATE_HOMING:
            text = "\u25cf Homing"
            color = "#3399ff"
        else:
            short = AXIS_STATE_NAMES.get(self._axis_state)
            if short and any(
                k in short
                for k in ("CALIBRATION", "DIR_FIND", "INDEX_SEARCH", "LOCKIN")
            ):
                calib = _CALIB_LABELS.get(
                    self._axis_state, short.replace("_", " ").title()
                )
                text = f"\u25cf Calibration: {calib}"
                color = "#3399ff"
            else:
                text = f"\u25cf {short or self._axis_state}"
                color = "gray"

        if text != self._status_text or color != self._status_color:
            self._status_text = text
            self._status_color = color
            self.statusChanged.emit()
