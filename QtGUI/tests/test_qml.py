"""Headless QML tests: the UI loads offscreen and reacts to the backend.

These exercise the QML->backend wiring (the part the unit tests can't reach):
context property bindings, signal->slot linkage, and the mode/input-mode
selector coupling. They use the offscreen platform, so no display or hardware
is required.
"""

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
    # Switch control mode on the backend; the input-mode selector must follow.
    backend.setMode("Position Control")
    _process_events(qml)
    assert input_combo.property("currentText") == "Trapezoidal Trajectory"
    assert input_combo.property("count") == 3
