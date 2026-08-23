"""Unit tests for GuiBackend (device logic) against a mock ODrive."""

from odrive.enums import (
    AXIS_STATE_CLOSED_LOOP_CONTROL,
    AXIS_STATE_IDLE,
    CONTROL_MODE_POSITION_CONTROL,
    CONTROL_MODE_TORQUE_CONTROL,
    CONTROL_MODE_VELOCITY_CONTROL,
    INPUT_MODE_PASSTHROUGH,
    INPUT_MODE_VEL_RAMP,
)

from backend import INPUT_MODES, MODES_BY_CONTROL, GuiBackend


def test_initial_state_offline():
    b = GuiBackend(verbose=False)
    b.connectOdrive = lambda: None  # don't spawn discovery thread
    assert b.status_backend.connected is False
    assert "Offline" in b.status_backend.connText
    b.update_timer.stop()


def test_static_models(backend):
    assert backend.modeNames == ["Velocity Control", "Position Control", "Torque Control"]
    assert "Full Calibration Sequence" in backend.stateNames
    assert backend.stateNames == list(backend.stateNames)  # stable order


def test_run_and_stop_command_device(backend):
    backend.run()
    assert backend.odrive.axis0.requested_state == AXIS_STATE_CLOSED_LOOP_CONTROL
    backend.stop()
    assert backend.odrive.axis0.requested_state == AXIS_STATE_IDLE


def test_set_active_setpoint_then_apply_writes_device(backend):
    backend.setActiveSetpoint(12.5)
    assert backend.odrive.axis0.controller.input_vel == 1.0  # not written yet
    backend.applySetpoint()
    assert backend.odrive.axis0.controller.input_vel == 12.5


def test_set_mode_seeds_new_modes_setpoint(backend):
    # Start in velocity, set a velocity setpoint, then switch to position.
    backend.setActiveSetpoint(3.25)  # stored under the active (velocity) mode
    backend.setMode("Position Control")
    assert backend.odrive.axis0.controller.config.control_mode == CONTROL_MODE_POSITION_CONTROL
    # The previous mode's input is not written (regression guard).
    assert backend.odrive.axis0.controller.input_vel == 1.0
    # Position's stored setpoint (init 0.0) is the seed for the new mode.
    assert backend.currentMode == 1  # index of position in _MODE_ORDER
    # Now a position setpoint stored in position mode is what gets applied.
    backend.setActiveSetpoint(7.0)
    backend.applySetpoint()
    assert backend.odrive.axis0.controller.input_pos == 7.0


def test_set_mode_not_writing_to_previous_mode(backend):
    # Regression: on mode switch, the OLD mode's input must not be touched.
    backend.setActiveSetpoint(5.0)
    backend.setMode("Torque Control")
    assert backend.odrive.axis0.controller.input_vel == 1.0  # old input untouched
    assert backend.odrive.axis0.controller.config.control_mode == CONTROL_MODE_TORQUE_CONTROL


def _expected_inputs(mode):
    return [INPUT_MODES[v] for v in MODES_BY_CONTROL[mode]]


def test_input_mode_model_follows_control_mode(backend):
    assert backend.inputModes == _expected_inputs(CONTROL_MODE_VELOCITY_CONTROL)
    backend.setMode("Position Control")
    assert backend.inputModes == _expected_inputs(CONTROL_MODE_POSITION_CONTROL)


def test_config_api_read_write_gate(backend):
    assert backend.hasConfig("motor", "current_lim")
    assert backend.getConfig("motor", "current_lim") == 12.5
    backend.setConfig("motor", "current_lim", 20.0)
    assert backend.getConfig("motor", "current_lim") == 20.0
    # Missing attribute -> gated off, safe defaults.
    assert backend.hasConfig("controller", "does_not_exist") is False
    assert backend.getConfig("controller", "does_not_exist") == 0.0


def test_errors_text_no_errors(backend):
    backend.updateReadings()
    assert backend.errorsText == "No errors."


def test_device_info_text(backend):
    text = backend.deviceInfoText()
    assert "Firmware" in text
    assert "Hardware" in text
    assert "1.2.3" in text


def test_log_text_tracks_events(backend):
    backend.logEvent("TEST", "hello")
    assert "TEST" in backend.logText
    assert "hello" in backend.logText


def test_mode_change_overrides_passthrough(backend):
    axis = backend.odrive.axis0
    axis.controller.config.input_mode = INPUT_MODE_PASSTHROUGH
    axis.controller.config.control_mode = CONTROL_MODE_POSITION_CONTROL  # device-side state
    backend.setMode("Velocity Control")
    assert axis.controller.config.input_mode == INPUT_MODE_VEL_RAMP
    assert "input mode -> Velocity Ramp" in backend.logText


def test_unknown_input_mode_shown_as_extra_entry(backend):
    backend.odrive.axis0.controller.config.input_mode = 0x7F
    backend._input_mode_model_for(CONTROL_MODE_VELOCITY_CONTROL)
    assert backend.inputModes[-1] == "unknown (0x7F)"
    assert backend.currentInputMode == len(MODES_BY_CONTROL[CONTROL_MODE_VELOCITY_CONTROL])


def test_transport_error_triggers_reconnect(backend):
    hits = []
    backend.connectOdrive = lambda: hits.append(1)  # fixture default is a no-op

    def boom(*a):
        raise TimeoutError("bus hiccup")  # transient failure: _on_lost would NOT fire

    backend.status_backend.update_readings = boom
    backend.updateReadings()
    assert hits and backend.odrive is None
