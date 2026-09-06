import json
import math
import os
import sys
import tempfile
from pathlib import Path
import pytest

TOOLS_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(TOOLS_DIR))
sys.path.insert(0, str(TOOLS_DIR / "odrive" / "pyfibre"))

import fibre.libfibre
from odrive.configuration import (
    _sanitize_for_json,
    backup_config,
    get_dict,
    obj_to_path,
    path_to_obj,
    restore_config,
    set_dict,
)


class DummyRemote(fibre.libfibre.RemoteObject):
    _refcount = 0

    def __init__(self):
        # Avoid native libfibre calls for unit mock
        pass


class MockProperty(fibre.libfibre.RemoteObject):
    _refcount = 0

    def __init__(self, val, writable=True, is_object_ref=False):
        self.val = val
        self._is_object_ref = is_object_ref
        self._intf_name = (
            "fibre.Property<readwrite object_ref>"
            if is_object_ref
            else ("fibre.Property<readwrite float>" if writable else "fibre.Property<readonly bool>")
        )
        if writable:
            self.exchange = self._exchange

    def read(self):
        return self.val

    def _exchange(self, val):
        if self._is_object_ref:
            if val is not None and not isinstance(val, fibre.libfibre.RemoteObject):
                raise TypeError(
                    f"Expected value of type RemoteObject or None but got '{type(val).__name__}'"
                )
        elif val is None:
            raise TypeError("float() argument must be a string or a real number, not 'NoneType'")
        self.val = val


class MockNode(fibre.libfibre.RemoteObject):
    _refcount = 0

    def __init__(self):
        pass


def test_sanitize_for_json():
    data = {
        "finite": 42.0,
        "inf": float("inf"),
        "-inf": float("-inf"),
        "nan": float("nan"),
        "nested": {"nested_inf": float("inf")},
        "list": [1.0, float("inf")],
    }
    sanitized = _sanitize_for_json(data)
    assert sanitized["finite"] == 42.0
    assert sanitized["inf"] == "Infinity"
    assert sanitized["-inf"] == "-Infinity"
    assert sanitized["nan"] == "NaN"
    assert sanitized["nested"]["nested_inf"] == "Infinity"
    assert sanitized["list"] == [1.0, "Infinity"]

    # Verify standard json can dump without error
    dumped = json.dumps(sanitized)
    assert "Infinity" in dumped


def test_obj_to_path_and_path_to_obj():
    dev = MockNode()
    dev.axis0 = MockNode()
    dev.axis0.controller = MockNode()
    dev.axis0.controller._input_pos_property = MockProperty(0.0)

    path = obj_to_path(dev, dev.axis0.controller._input_pos_property)
    assert path == "axis0.controller._input_pos_property"

    resolved = path_to_obj(dev, path)
    assert resolved is dev.axis0.controller._input_pos_property


def test_restore_ignores_readonly_properties():
    dev = MockNode()
    dev.config = MockNode()
    dev.config.anticogging = MockNode()
    dev.config.anticogging.calib_anticogging = False
    dev.config.anticogging._calib_anticogging_property = MockProperty(False, writable=False)

    data = {"config": {"anticogging": {"calib_anticogging": True}}}
    errors = set_dict(dev, "", data)
    assert errors == []


def test_restore_handles_null_float_gracefully():
    dev = MockNode()
    dev.config = MockNode()
    dev.config.vel_integrator_limit = float("inf")
    dev.config._vel_integrator_limit_property = MockProperty(float("inf"), writable=True)

    # Legacy config file where non-finite float was exported as null
    data = {"config": {"vel_integrator_limit": None}}
    errors = set_dict(dev, "", data)
    assert errors == []
    # Retains current default instead of crashing with TypeError
    assert dev.config._vel_integrator_limit_property.val == float("inf")


def test_restore_handles_string_infinity():
    dev = MockNode()
    dev.config = MockNode()
    dev.config.torque_lim = 10.0
    dev.config._torque_lim_property = MockProperty(10.0, writable=True)

    data = {"config": {"torque_lim": "Infinity"}}
    errors = set_dict(dev, "", data)
    assert errors == []
    assert math.isinf(dev.config._torque_lim_property.val)


def test_restore_resolves_endpoint_string_to_remote_object():
    dev = MockNode()
    dev.axis0 = MockNode()
    dev.axis0.controller = MockNode()
    dev.axis0.controller._input_vel_property = MockProperty(0.0)

    dev.config = MockNode()
    dev.config.gpio3_analog_mapping = MockNode()
    dev.config.gpio3_analog_mapping.endpoint = None
    dev.config.gpio3_analog_mapping._endpoint_property = MockProperty(
        None, writable=True, is_object_ref=True
    )

    data = {
        "config": {
            "gpio3_analog_mapping": {
                "endpoint": "axis0.controller._input_vel_property"
            }
        }
    }
    errors = set_dict(dev, "", data)
    assert errors == []
    assert (
        dev.config.gpio3_analog_mapping._endpoint_property.val
        is dev.axis0.controller._input_vel_property
    )


def test_restore_allows_null_for_endpoint_object_ref():
    dev = MockNode()
    dev.axis0 = MockNode()
    dev.axis0.controller = MockNode()
    dev.axis0.controller._input_vel_property = MockProperty(0.0)

    dev.config = MockNode()
    dev.config.gpio3_analog_mapping = MockNode()
    dev.config.gpio3_analog_mapping.endpoint = None
    dev.config.gpio3_analog_mapping._endpoint_property = MockProperty(
        dev.axis0.controller._input_vel_property, writable=True, is_object_ref=True
    )

    data = {"config": {"gpio3_analog_mapping": {"endpoint": None}}}
    errors = set_dict(dev, "", data)
    assert errors == []
    assert dev.config.gpio3_analog_mapping._endpoint_property.val is None


def test_backup_and_restore_round_trip(tmp_path):
    class FakeLogger:
        def info(self, *a, **k): pass
        def warn(self, *a, **k): pass

    dev = MockNode()
    dev.axis0 = MockNode()
    dev.axis0.controller = MockNode()
    dev.axis0.controller._input_pos_property = MockProperty(0.0)

    dev.config = MockNode()
    dev.config.torque_lim = float("inf")
    dev.config._torque_lim_property = MockProperty(float("inf"), writable=True)
    dev.config.counts_per_step = 42
    dev.config._counts_per_step_property = MockProperty(42, writable=True)
    dev.config.gpio3_analog_mapping = MockNode()
    dev.config.gpio3_analog_mapping.endpoint = dev.axis0.controller._input_pos_property
    dev.config.gpio3_analog_mapping._endpoint_property = MockProperty(
        dev.axis0.controller._input_pos_property, writable=True, is_object_ref=True
    )
    dev.config.anticogging = MockNode()
    dev.config.anticogging.calib_anticogging = False
    dev.config.anticogging._calib_anticogging_property = MockProperty(False, writable=False)
    dev.save_configuration = lambda: None

    path = str(tmp_path / "config.json")
    backup_config(dev, path, FakeLogger())

    with open(path) as f:
        saved_json = json.load(f)

    assert saved_json["config"]["torque_lim"] == "Infinity"
    assert saved_json["config"]["counts_per_step"] == 42
    assert (
        saved_json["config"]["gpio3_analog_mapping"]["endpoint"]
        == "axis0.controller._input_pos_property"
    )
    # Read-only property was not backed up
    assert "anticogging" not in saved_json["config"] or "calib_anticogging" not in saved_json["config"]["anticogging"]

    # Reset values on dev to verify restore overwrites
    dev.config._torque_lim_property.val = 5.0
    dev.config._counts_per_step_property.val = 1
    dev.config._endpoint_property = MockProperty(None, writable=True, is_object_ref=True)

    restore_config(dev, path, FakeLogger())
    assert math.isinf(dev.config._torque_lim_property.val)
    assert dev.config._counts_per_step_property.val == 42

