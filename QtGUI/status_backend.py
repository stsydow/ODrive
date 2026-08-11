import logging

import odrive.enums
from PySide6.QtCore import Property, QObject, Signal

from errors import DEVICE_EXCEPTIONS, format_current, read_error_report

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
    if short and any(k in short for k in ("CALIBRATION", "DIR_FIND", "INDEX_SEARCH", "LOCKIN")):
        for label, enum_val in STATE_MAP.items():
            if enum_val == value:
                return "Calibration: " + label
        return "Calibration: " + short.replace("_", " ").title()
    return short or str(value)

class StatusBackend(QObject):
    connChanged = Signal()
    stateChanged = Signal()
    errorsChanged = Signal()
    powerChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._conn_text = "\u25cf Offline"
        self._conn_color = "gray"
        self._state_text = "--"
        self._error_text = "Err: OK"
        self._error_color = "green"
        self._vbus_text = "-- V"
        self._power_text = "-- W"
        self._connected = False
        self._last_error_key = None
        self._rendered_errors = None

    @Property(str, notify=connChanged)
    def connText(self): return self._conn_text

    @Property(str, notify=connChanged)
    def connColor(self): return self._conn_color

    @Property(bool, notify=connChanged)
    def connected(self): return self._connected

    @Property(str, notify=stateChanged)
    def stateText(self): return self._state_text

    @Property(str, notify=errorsChanged)
    def errorText(self): return self._error_text

    @Property(str, notify=errorsChanged)
    def errorColor(self): return self._error_color

    @Property(str, notify=powerChanged)
    def vbusText(self): return self._vbus_text

    @Property(str, notify=powerChanged)
    def powerText(self): return self._power_text

    def set_conn(self, text, color, connected):
        changed = (self._conn_text != text or self._conn_color != color or self._connected != connected)
        self._conn_text = text
        self._conn_color = color
        self._connected = connected
        if changed:
            self.connChanged.emit()

    def update_readings(self, odrv, axis, log_event_func):
        if odrv is None:
            return

        self._update_state(axis)
        self._update_voltages(odrv)
        self._update_errors(odrv, log_event_func)

    def _update_state(self, axis):
        st = axis.current_state if axis is not None else None
        if st is not None:
            text = _state_display(st)
            if text != self._state_text:
                self._state_text = text
                self.stateChanged.emit()

    def _update_voltages(self, odrv):
        try:
            vbus = odrv.vbus_voltage
            new_v = f"{vbus:.1f} V"
            new_p = f"{vbus * odrv.ibus:.1f} W"
            if new_v != self._vbus_text or new_p != self._power_text:
                self._vbus_text = new_v
                self._power_text = new_p
                self.powerChanged.emit()
        except DEVICE_EXCEPTIONS:
            pass

    def _update_errors(self, odrv, log_event_func):
        report = read_error_report(odrv)
        key = tuple(sorted((s.name, tuple(s.errors)) for s in report.sources))
        if key and key != self._last_error_key:
            detail = "; ".join(f"{s.name}: {' | '.join(s.errors) if s.errors else f'0x{s.value:X}'}"
                             for s in report.sources)
            log_event_func("ERROR", f"axis errors -> {detail}")
            self._last_error_key = key
        elif not key and self._last_error_key:
            log_event_func("CLEAR", "errors cleared")
            self._last_error_key = None

        if report.any:
            union = 0
            for s in report.sources:
                union |= s.value
            new_text = f"Err: 0x{union:X}"
            new_color = "red"
        else:
            new_text = "Err: OK"
            new_color = "green"

        if new_text != self._error_text or new_color != self._error_color:
            self._error_text = new_text
            self._error_color = new_color
            self.errorsChanged.emit()

        rendered = format_current(report)
        if rendered != self._rendered_errors:
            self._rendered_errors = rendered
            self.errorsChanged.emit()
