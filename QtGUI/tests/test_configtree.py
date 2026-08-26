"""Unit tests for the Config Browser (Plan §2.4): tree walking, filtering,
editability, and the IDLE-gated write path."""

from odrive.enums import AXIS_STATE_CLOSED_LOOP_CONTROL, AXIS_STATE_IDLE
from PySide6.QtCore import QModelIndex

from configtree import _EDITABLE_ROLE, _NAME_ROLE, _VALUE_ROLE


def _walk(model, path):
    """Model index at dotted `path`; QModelIndex() for the root."""
    idx = QModelIndex()
    if not path:
        return idx
    for part in path.split("."):
        for r in range(model.rowCount(idx)):
            child = model.index(r, 0, idx)
            if model.data(child, _NAME_ROLE) == part:
                idx = child
                break
        else:
            raise AssertionError(f"path component {part!r} missing in {path}")
    return idx


def test_tree_walks_object_graph(backend):
    backend.browserModel.reset()
    m = backend.browserModel
    root_children = [m.data(m.index(r, 0), _NAME_ROLE) for r in range(m.rowCount())]
    assert "axis0" in root_children and "config" in root_children
    # callables (save_configuration, ...) are never traversed
    assert "save_configuration" not in root_children
    # leaf value readable via the walk
    idx = _walk(m, "axis0.motor.config.current_lim")
    assert m.data(idx, _VALUE_ROLE) == "12.5"


def test_editable_only_for_config_scalars(backend):
    backend.browserModel.reset()
    m = backend.browserModel
    editable = _walk(m, "axis0.motor.config.current_lim")
    assert m.data(editable, _EDITABLE_ROLE) is True
    # estimate is not under .config -> read-only display
    estimate = _walk(m, "axis0.encoder.vel_estimate")
    assert m.data(estimate, _EDITABLE_ROLE) is False
    # string config leaves are read-only too (editable = bool/int/float)
    str_leaf = _walk(m, "axis0.motor.config.motor_name")
    assert m.data(str_leaf, _EDITABLE_ROLE) is False
    assert m.data(str_leaf, _VALUE_ROLE) == "sew motor"
    # branches are marked as such: expandable, i.e. they have children
    assert m.rowCount(_walk(m, "axis0.motor")) > 0


def test_filter_keeps_matching_paths_only(backend):
    backend.browserModel.reset()
    m = backend.browserModel
    backend.browserModel.set_filter("Current_Lim")  # case-insensitive
    root_children = [m.data(m.index(r, 0), _NAME_ROLE) for r in range(m.rowCount())]
    assert root_children == ["axis0"]  # only the chain holding matches
    cfg = _walk(m, "axis0.motor.config")
    names = [m.data(m.index(r, 0, cfg), _NAME_ROLE) for r in range(m.rowCount(cfg))]
    assert names == ["current_lim", "current_lim_margin"]


def test_write_commits_when_idle(backend):
    backend.browserModel.reset()
    backend.odrive.axis0.current_state = AXIS_STATE_IDLE
    assert backend.writeBrowserValue("axis0.motor.config.current_lim", "20")
    assert backend.odrive.axis0.motor.config.current_lim == 20.0
    assert "WRITE" in backend.logText
    # cached view value invalidated by the write
    assert (
        backend.browserModel.data(
            _walk(backend.browserModel, "axis0.motor.config.current_lim"), _VALUE_ROLE
        )
        == "20.0"
    )


def test_write_refused_while_running(backend):
    """Commit-time re-check: refusal when the axis left IDLE."""
    backend.browserModel.reset()
    backend.odrive.axis0.current_state = AXIS_STATE_CLOSED_LOOP_CONTROL
    assert not backend.writeBrowserValue("axis0.motor.config.current_lim", "20")
    assert backend.odrive.axis0.motor.config.current_lim == 12.5
    assert "refused" in backend.logText


def test_write_type_checked_and_config_gated(backend):
    backend.browserModel.reset()
    backend.odrive.axis0.current_state = AXIS_STATE_IDLE
    # bool parses true/false; int accepts hex (enum leaves, odrivetool parity)
    assert backend.writeBrowserValue(
        "axis0.controller.config.enable_vel_limit", "false"
    )
    assert backend.odrive.axis0.controller.config.enable_vel_limit is False
    assert backend.writeBrowserValue("axis0.controller.config.input_mode", "0x1")
    assert backend.odrive.axis0.controller.config.input_mode == 1
    # garbage -> refuse
    assert not backend.writeBrowserValue("axis0.motor.config.current_lim", "abc")
    # non-finite floats never reach the device
    assert not backend.writeBrowserValue("axis0.motor.config.current_lim", "nan")
    assert backend.odrive.axis0.motor.config.current_lim == 12.5
    # read-only endpoint -> refuse even though IDLE
    assert not backend.writeBrowserValue("axis0.encoder.vel_estimate", "0")


def test_axis_idle_reflects_device_state(backend):
    """Drives the limits tabs' disabled state in QML."""
    backend.odrive.axis0.current_state = AXIS_STATE_CLOSED_LOOP_CONTROL
    backend.updateReadings()
    assert backend.axisIdle is False
    backend.odrive.axis0.current_state = AXIS_STATE_IDLE
    backend.updateReadings()
    assert backend.axisIdle is True
