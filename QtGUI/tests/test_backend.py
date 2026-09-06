"""Unit tests for GuiBackend (device logic) against a mock ODrive."""

from odrive.enums import (
    AXIS_STATE_CLOSED_LOOP_CONTROL,
    AXIS_STATE_IDLE,
    CONTROL_MODE_POSITION_CONTROL,
    CONTROL_MODE_TORQUE_CONTROL,
    CONTROL_MODE_VELOCITY_CONTROL,
    GPIO_MODE_ANALOG_IN,
    GPIO_MODE_DIGITAL,
    INPUT_MODE_PASSTHROUGH,
    INPUT_MODE_VEL_RAMP,
)
from PySide6.QtCore import QEventLoop, QTimer

from backend import INPUT_MODES, MODES_BY_CONTROL, GuiBackend


def test_initial_state_offline():
    b = GuiBackend(verbose=False)
    b.connectOdrive = lambda: None  # don't spawn discovery thread
    assert b.status_backend.connected is False
    assert "Offline" in b.status_backend.connText
    b.update_timer.stop()


def test_static_models(backend):
    assert backend.modeNames == ["Velocity", "Position", "Torque"]
    assert "Full Calibration Sequence" in backend.stateNames


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
    backend.setMode("Position")
    assert (
        backend.odrive.axis0.controller.config.control_mode
        == CONTROL_MODE_POSITION_CONTROL
    )
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
    backend.setMode("Torque")
    assert backend.odrive.axis0.controller.input_vel == 1.0  # old input untouched
    assert (
        backend.odrive.axis0.controller.config.control_mode
        == CONTROL_MODE_TORQUE_CONTROL
    )


def _expected_inputs(mode):
    return [INPUT_MODES[v] for v in MODES_BY_CONTROL[mode]]


def test_input_mode_model_follows_control_mode(backend):
    assert backend.inputModes == _expected_inputs(CONTROL_MODE_VELOCITY_CONTROL)
    backend.setMode("Position")
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
    axis.controller.config.control_mode = (
        CONTROL_MODE_POSITION_CONTROL  # device-side state
    )
    backend.setMode("Velocity")
    assert axis.controller.config.input_mode == INPUT_MODE_VEL_RAMP
    assert "input mode -> Velocity Ramp" in backend.logText


def test_unknown_input_mode_shown_as_extra_entry(backend):
    backend.odrive.axis0.controller.config.input_mode = 0x7F
    backend._input_mode_model_for(CONTROL_MODE_VELOCITY_CONTROL)
    assert backend.inputModes[-1] == "unknown (0x7F)"
    assert backend.currentInputMode == len(
        MODES_BY_CONTROL[CONTROL_MODE_VELOCITY_CONTROL]
    )


def test_transport_error_triggers_reconnect(backend):
    hits = []
    backend.connectOdrive = lambda: hits.append(1)  # fixture default is a no-op

    def boom(*a):
        raise TimeoutError("bus hiccup")  # transient failure: _on_lost would NOT fire

    backend.status_backend.update_readings = boom
    backend.updateReadings()
    assert hits and backend.odrive is None


def test_device_loss_drops_link_and_schedules_one_reconnect(backend):
    """_on_device_lost: odrive dropped immediately, one reconnect queued."""
    hits = []
    backend.connectOdrive = lambda: hits.append(1)
    # The fixture attaches the device directly (bypassing _on_connected), so
    # register the loss callback here exactly as connect does.
    fut = backend.odrive._on_lost
    fut.add_done_callback(backend._on_device_lost)
    for cb in list(fut._cbs):  # fire the loss callback like the fibre thread would
        cb(fut)
    assert backend.odrive is None
    loop = QEventLoop()
    QTimer.singleShot(0, loop.quit)
    loop.exec()  # flush the singleShot(0) reconnect
    assert len(hits) == 1  # a second path would double-connect


def test_object_lost_tick_drops_link_once(backend):
    """0.5.7-hardened: the EmptyInterface race raises ObjectLostError, which
    must drop the link exactly once like any other device-gone signal."""
    import fibre.libfibre as lf

    hits = []
    backend.connectOdrive = lambda: hits.append(1)

    def gone():
        raise lf.ObjectLostError()

    backend._sample_plot = gone
    backend.plotTick()
    assert backend.odrive is None and len(hits) == 1
    backend.plotTick()  # offline now: must early-return silently


def test_analog_sync_reads_mapping_and_live_value(backend):
    axis = backend.odrive.axis0
    mapping = backend.odrive.config.gpio3_analog_mapping
    mapping.endpoint = axis.controller._input_vel_property
    mapping.min = -1.7
    mapping.max = 30.0
    axis.controller.input_vel = 5.5
    backend._load_analog_bounds()
    backend._sync_analog()
    assert backend.analogTarget == "Velocity"
    assert backend.analogValue == 5.5
    assert backend.analogMin == -1.7
    assert backend.analogMax == 30.0


def test_analog_disabled_when_no_endpoint(backend):
    backend.odrive.config.gpio3_analog_mapping.endpoint = None
    backend._sync_analog()
    assert backend.analogTarget == "Disabled"
    assert backend.analogValue == 0.0


def test_set_analog_target_enables_and_disables(backend):
    axis = backend.odrive.axis0
    cfg = backend.odrive.config
    mapping = cfg.gpio3_analog_mapping
    backend.setAnalogTarget("Velocity")
    assert cfg.gpio3_mode == GPIO_MODE_ANALOG_IN
    assert mapping.endpoint is axis.controller._input_vel_property
    assert backend.analogTarget == "Velocity"
    # While analog input drove velocity, input_vel changed to 18.0
    axis.controller.input_vel = 18.0
    backend.setAnalogTarget("Disabled")
    assert cfg.gpio3_mode == GPIO_MODE_DIGITAL
    assert mapping.endpoint is None
    assert backend.analogTarget == "Disabled"
    # Setpoints must be re-synced from device on disable
    assert backend.velSetpoint == 18.0


def test_set_analog_bounds_writes_device(backend):
    backend.setAnalogMin(-2.5)
    backend.setAnalogMax(40.0)
    mapping = backend.odrive.config.gpio3_analog_mapping
    assert mapping.min == -2.5
    assert mapping.max == 40.0
    assert backend.analogMin == -2.5
    assert backend.analogMax == 40.0


def test_switch_analog_gpio_moves_mapping(backend):
    axis = backend.odrive.axis0
    cfg = backend.odrive.config
    backend.setAnalogTarget("Velocity")
    backend.setAnalogMin(-10.0)
    backend.setAnalogMax(10.0)
    assert cfg.gpio3_mode == GPIO_MODE_ANALOG_IN
    assert cfg.gpio3_analog_mapping.endpoint is axis.controller._input_vel_property

    # Switch from GPIO 3 to GPIO 4
    backend.setAnalogGpio(4)
    assert backend.analogGpio == 4
    assert cfg.gpio3_mode == GPIO_MODE_DIGITAL
    assert cfg.gpio3_analog_mapping.endpoint is None
    assert cfg.gpio4_mode == GPIO_MODE_ANALOG_IN
    assert cfg.gpio4_analog_mapping.endpoint is axis.controller._input_vel_property
    assert cfg.gpio4_analog_mapping.min == -10.0
    assert cfg.gpio4_analog_mapping.max == 10.0


def test_analog_deadband_controls(backend):
    mapping = backend.odrive.config.gpio3_analog_mapping
    assert backend.analogDeadbandAvailable is True
    backend.setAnalogDeadbandEnable(True)
    backend.setAnalogDeadbandStart(0.4)
    backend.setAnalogDeadbandEnd(0.6)
    backend.setAnalogDeadbandLevel(0.0)
    backend.setAnalogDeadbandIdle(True)
    assert mapping.deadband_enable is True
    assert mapping.deadband_start == 0.4
    assert mapping.deadband_end == 0.6
    assert mapping.deadband_level == 0.0
    assert mapping.deadband_idle is True
    assert backend.analogDeadbandEnable is True
    assert backend.analogDeadbandStart == 0.4
    assert backend.analogDeadbandEnd == 0.6
    assert backend.analogDeadbandLevel == 0.0
    assert backend.analogDeadbandIdle is True


def test_unexpected_error_surfaces_and_keeps_link(backend):
    """Post-hardening, AttributeError/TypeError are no longer disconnect
    signals -- they are bugs or missing endpoints and must surface instead of
    being conflated into a reconnect."""
    import pytest

    def buggy():
        raise TypeError("genuine bug")

    backend._sample_plot = buggy
    with pytest.raises(TypeError):
        backend.plotTick()
    assert backend.odrive is not None  # link stays up
