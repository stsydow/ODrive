"""Unit tests for StatusBackend (status footer) against the mock ODrive."""

import odrive.enums

from status_backend import _state_display


def test_state_display_names():
    assert _state_display(odrive.enums.AXIS_STATE_IDLE) == "Idle"
    assert _state_display(odrive.enums.AXIS_STATE_CLOSED_LOOP_CONTROL) == "Control Loop"
    assert _state_display(odrive.enums.AXIS_STATE_STARTUP_SEQUENCE) == "Startup"
    assert _state_display(odrive.enums.AXIS_STATE_MOTOR_CALIBRATION) == "Calibration: Motor Calibration"


def test_set_conn_updates_properties(backend):
    sb = backend.status_backend
    sb.set_conn("\u25cf Connecting\u2026", "orange", False)
    assert sb.connected is False
    assert sb.connText == "\u25cf Connecting\u2026"
    assert sb.connColor == "orange"


def test_readings_update_footer(backend):
    sb = backend.status_backend
    sb.update_readings(backend.odrive, backend._axis(), backend.logEvent)
    assert sb.stateText == "Idle"
    assert sb.vbusText == "24.0 V"
    assert sb.powerText == "7.2 W"
    assert sb.errorText == "Err: OK"
    assert sb.errorColor == "green"


def test_error_transitions_log_events(backend):
    sb = backend.status_backend
    sb.update_readings(backend.odrive, backend._axis(), backend.logEvent)
    backend.odrive.axis0.error = 1
    sb.update_readings(backend.odrive, backend._axis(), backend.logEvent)
    assert sb.errorText == "Err: 0x1"
    assert sb.errorColor == "red"
    assert any(e.category == "ERROR" and "axis errors" in e.message for e in backend.event_log)
    backend.odrive.axis0.error = 0
    sb.update_readings(backend.odrive, backend._axis(), backend.logEvent)
    assert sb.errorText == "Err: OK"
    assert any(e.category == "CLEAR" for e in backend.event_log)
