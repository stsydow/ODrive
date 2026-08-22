"""Headless QML tests: the UI loads offscreen and reacts to the backend.

These exercise the QML->backend wiring (the part the unit tests can't reach):
context property bindings, signal->slot linkage, and the mode/input-mode
selector coupling. They use the offscreen platform, so no display or hardware
is required.
"""

from odrive.enums import INPUT_MODE_POS_FILTER, INPUT_MODE_TRAP_TRAJ
from PySide6.QtCore import QEventLoop, QObject, QTimer


def _find_qml(root, object_name):
    """Find a QML object under `root` by objectName (recursive)."""
    return root.findChild(QObject, object_name)


def test_main_window_loads(qml):
    root = qml.rootObjects()[0]
    assert root.property("title").startswith("ODrive")


def test_dialogs_and_combos_present(qml):
    root = qml.rootObjects()[0]
    assert _find_qml(root, "errorDialog") is not None
    assert _find_qml(root, "eventLogDialog") is not None
    assert _find_qml(root, "deviceInfoDialog") is not None
    assert _find_qml(root, "modeCombo") is not None
    assert _find_qml(root, "inputCombo") is not None


def test_device_info_dialog_fetches_on_open(qml, backend):
    """The info text must be read when the dialog opens (device online), not
    frozen at the startup snapshot — regression for the 'Not connected'
    label shown while online."""
    root = qml.rootObjects()[0]
    dialog = _find_qml(root, "deviceInfoDialog")
    dialog.setProperty("visible", True)
    _process_events(qml)
    label = _find_qml(root, "deviceInfoLabel")
    assert "Serial number" in label.property("text")
    assert "Not connected" not in label.property("text")


def _process_events(qml):
    loop = QEventLoop()
    QTimer.singleShot(0, loop.quit)
    loop.exec()


def test_mode_combo_follows_backend(qml):
    root = qml.rootObjects()[0]
    mode = _find_qml(root, "modeCombo")
    _process_events(qml)
    assert mode.property("currentText") == "Velocity Control"


def test_input_mode_combo_links_to_control_mode(qml, backend):
    root = qml.rootObjects()[0]
    input_combo = _find_qml(root, "inputCombo")
    _process_events(qml)
    assert input_combo.property("currentText") == "Velocity Ramp"
    # Explicit control-mode switch: device still runs VEL_RAMP (2), which
    # position mode doesn't list — setMode() steers it to the mode default.
    backend.setMode("Position Control")
    _process_events(qml)
    assert input_combo.property("currentText") == "Trapezoidal Trajectory"
    assert input_combo.property("count") == 3
    assert backend.odrive.axis0.controller.config.input_mode == INPUT_MODE_TRAP_TRAJ
    # An explicit user pick writes the device directly.
    backend.setInputMode(0)
    _process_events(qml)
    assert input_combo.property("currentText") == "Position Filter"
    assert backend.odrive.axis0.controller.config.input_mode == INPUT_MODE_POS_FILTER
