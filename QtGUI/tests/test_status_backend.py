"""Unit tests for StatusBackend (status footer) against the mock ODrive."""

import odrive.enums

from status_backend import _state_display


def test_state_display_names():
    assert _state_display(odrive.enums.AXIS_STATE_IDLE) == "Idle"
    assert _state_display(odrive.enums.AXIS_STATE_CLOSED_LOOP_CONTROL) == "Control Loop"
    assert _state_display(odrive.enums.AXIS_STATE_STARTUP_SEQUENCE) == "Startup"
    assert (
        _state_display(odrive.enums.AXIS_STATE_MOTOR_CALIBRATION)
        == "Calibration: Motor Calibration"
    )


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
    assert sb.errorText == "Err: INVALID_STATE"
    assert sb.errorColor == "red"
    assert sb.statusText == "\u25cf Error: INVALID_STATE"
    assert sb.statusColor == "red"
    assert sb.hasError is True
    assert any(
        e.category == "ERROR" and "axis errors" in e.message for e in backend.event_log
    )
    backend.odrive.axis0.error = 0
    sb.update_readings(backend.odrive, backend._axis(), backend.logEvent)
    assert sb.errorText == "Err: OK"
    assert sb.hasError is False
    assert any(e.category == "CLEAR" for e in backend.event_log)


def test_merged_status_states(backend):
    sb = backend.status_backend

    # 1. Connecting
    sb.set_conn("\u25cf Connecting\u2026", "orange", False)
    assert sb.statusText == "\u25cf Connecting\u2026"
    assert sb.statusColor == "orange"

    # 2. Idle (normal)
    sb.set_conn("\u25cf Online", "green", True)
    backend.odrive.axis0.current_state = odrive.enums.AXIS_STATE_IDLE
    sb.update_readings(backend.odrive, backend._axis(), backend.logEvent)
    assert sb.statusText == "\u25cf Idle"

    # 3. Idle (Armed)
    sb.update_readings(
        backend.odrive,
        backend._axis(),
        backend.logEvent,
        armed=True,
        deadband_idle=True,
    )
    assert sb.statusText == "\u25cf Idle (Armed)"
    assert sb.statusColor == "#ffaa00"

    # 4. Idle (Disarmed)
    sb.update_readings(
        backend.odrive,
        backend._axis(),
        backend.logEvent,
        armed=False,
        deadband_idle=True,
    )
    assert sb.statusText == "\u25cf Idle (Disarmed)"
    assert sb.statusColor == "gray"

    # 5. Running
    backend.odrive.axis0.current_state = odrive.enums.AXIS_STATE_CLOSED_LOOP_CONTROL
    sb.update_readings(backend.odrive, backend._axis(), backend.logEvent)
    assert sb.statusText == "\u25cf Running"
    assert sb.statusColor == "#00cc44"

    # 6. Calibration
    backend.odrive.axis0.current_state = odrive.enums.AXIS_STATE_MOTOR_CALIBRATION
    sb.update_readings(backend.odrive, backend._axis(), backend.logEvent)
    assert sb.statusText == "\u25cf Calibration: Motor Calibration"
    assert sb.statusColor == "#3399ff"

    # 7. Error overrides state
    backend.odrive.axis0.error = 1
    sb.update_readings(backend.odrive, backend._axis(), backend.logEvent)
    assert sb.statusText == "\u25cf Error: INVALID_STATE"
    assert sb.statusColor == "red"
    assert sb.hasError is True
