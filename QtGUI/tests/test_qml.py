"""Headless QML tests: the UI loads offscreen and reacts to the backend.

These exercise the QML->backend wiring (the part the unit tests can't reach):
context property bindings, signal->slot linkage, and the mode/input-mode
selector coupling. They use the offscreen platform, so no display or hardware
is required.
"""

from odrive.enums import INPUT_MODE_POS_FILTER, INPUT_MODE_TRAP_TRAJ
from PySide6.QtCore import QEvent, QEventLoop, QObject, Qt, QTimer, QUrl
from PySide6.QtGui import QGuiApplication, QKeyEvent
from PySide6.QtQml import QQmlApplicationEngine


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
    assert _find_qml(root, "configBrowserDialog") is not None
    assert _find_qml(root, "modeCombo") is not None
    assert _find_qml(root, "inputCombo") is not None


def test_device_info_dialog_fetches_on_open(qml, backend):
    """The info text must be read when the dialog opens (device online), not
    frozen at the startup snapshot — regression for the 'Not connected'
    label shown while online."""
    root = qml.rootObjects()[0]
    dialog = _find_qml(root, "deviceInfoDialog")
    dialog.setProperty("visible", True)
    try:
        label = _find_qml(root, "deviceInfoLabel")
        assert "Serial number" in label.property("text")
        assert "Not connected" not in label.property("text")
    finally:
        # Application-modal Window: close even on failure, or it swallows
        # the keyboard for every later test.
        dialog.setProperty("visible", False)


def _process_events(qml):
    loop = QEventLoop()
    QTimer.singleShot(0, loop.quit)
    loop.exec()


def _send_key(item, key, text=""):
    """Deliver a raw KeyPress straight to a QML item. QTest is widget-only;
    window-routed delivery depends on window-activation state, and paired
    KeyReleases make the spinbox template's text binding revert the edit —
    hence direct, KeyPress-only delivery."""
    QGuiApplication.sendEvent(
        item, QKeyEvent(QEvent.KeyPress, key, Qt.KeyboardModifier.NoModifier, text)
    )


def test_enter_in_setpoint_applies_to_device(qml, backend):
    """Return pressed inside the velocity spinbox must commit AND write the
    device (apply) — also in Idle, so a setpoint can be pre-set before Run."""
    root = qml.rootObjects()[0]
    row = _find_qml(root, "velSetpoint")
    assert row is not None, "velocity setpoint row not found"
    spin = next(
        c
        for c in row.findChildren(QObject, None)
        if c.metaObject().className().startswith("DoubleSpinBox")
    )
    content = spin.property("contentItem")
    assert content is not None
    content.forceActiveFocus()
    # Synthetic typing is unreliable offscreen (key events don't insert into
    # the validator-guarded TextInput); place the text and exercise the Enter
    # path: accepted() -> parse displayed text -> store -> apply.
    content.setProperty("text", "2.5")
    _send_key(content, Qt.Key_Return)
    _process_events(qml)
    assert backend.odrive.axis0.controller.input_vel == 2.5


def test_settings_spinboxes_editable(qml):
    """Keyboard accessibility: settings spinboxes accept typed input, not just
    mouse clicks on the +/- buttons (the setpoint boxes already were). One
    row per settings tab, found by objectName — scanning the whole window
    tree is unreliable offscreen."""
    root = qml.rootObjects()[0]
    for name in [
        "motor.current_lim",  # Electrical Limits
        "controller.vel_limit",  # Mechanical Limits
        "controller.vel_gain",
    ]:  # Control Parameters
        row = _find_qml(root, name)
        assert row is not None, f"{name} row not found"
        spins = [
            c
            for c in row.findChildren(QObject, None)
            if c.metaObject().className().startswith("DoubleSpinBox")
        ]
        assert len(spins) == 1 and spins[0].property("editable"), (
            f"{name}: spinbox missing or not editable"
        )


def test_mode_combo_follows_backend(qml):
    root = qml.rootObjects()[0]
    mode = _find_qml(root, "modeCombo")
    _process_events(qml)
    assert mode.property("currentText") == "Velocity"


def test_input_mode_combo_links_to_control_mode(qml, backend):
    root = qml.rootObjects()[0]
    input_combo = _find_qml(root, "inputCombo")
    _process_events(qml)
    assert input_combo.property("currentText") == "Velocity Ramp"
    # Explicit control-mode switch: device still runs VEL_RAMP (2), which
    # position mode doesn't list — setMode() steers it to the mode default.
    backend.setMode("Position")
    _process_events(qml)
    assert input_combo.property("currentText") == "Trapezoidal Trajectory"
    assert input_combo.property("count") == 3
    assert backend.odrive.axis0.controller.config.input_mode == INPUT_MODE_TRAP_TRAJ
    # An explicit user pick writes the device directly.
    backend.setInputMode(0)
    _process_events(qml)
    assert input_combo.property("currentText") == "Position Filter"
    assert backend.odrive.axis0.controller.config.input_mode == INPUT_MODE_POS_FILTER


def test_analog_mapping_disables_setpoint_row(qml, backend):
    """While an analog mapping drives the active setpoint, that setpoint row
    is disabled and mirrors the live mapped value; disabling re-enables it."""
    root = qml.rootObjects()[0]
    row = _find_qml(root, "velSetpoint")
    spin = next(
        c
        for c in row.findChildren(QObject, None)
        if c.metaObject().className().startswith("DoubleSpinBox")
    )
    # Not externally driven by default: the row is editable.
    assert not row.property("externallyDriven")
    assert spin.property("enabled")
    # Enable the analog input -> velocity mapping, then move the live value.
    backend.setAnalogTarget("Velocity")
    backend.odrive.axis0.controller.input_vel = 7.25
    backend._sync_analog()
    _process_events(qml)
    assert row.property("externallyDriven")
    assert not spin.property("enabled")
    assert spin.property("value") == 7.25
    # Disabling the mapping re-enables the row.
    backend.setAnalogTarget("Disabled")
    _process_events(qml)
    assert not row.property("externallyDriven")
    assert spin.property("enabled")


def test_analog_update_does_not_clobber_undriven_row(qml, backend):
    """Live analog updates must never overwrite in-progress edits in an
    un-driven setpoint row."""
    root = qml.rootObjects()[0]
    row = _find_qml(root, "posSetpoint")
    spin = next(
        c
        for c in row.findChildren(QObject, None)
        if c.metaObject().className().startswith("DoubleSpinBox")
    )
    spin.setProperty("value", 42.0)
    _process_events(qml)
    # Simulate high-frequency analog updates (ADC noise / input moving)
    backend._set_analog_state("Disabled", 15.0)
    _process_events(qml)
    assert spin.property("value") == 42.0


def test_setpoint_row_analog_checkbox_and_bounds(qml, backend):
    root = qml.rootObjects()[0]
    row = _find_qml(root, "velSetpoint")
    cb = next(
        c
        for c in row.findChildren(QObject, None)
        if "CheckBox" in c.metaObject().className()
    )
    assert not cb.property("checked")
    # Toggle on
    cb.setProperty("checked", True)
    cb.toggled.emit()
    _process_events(qml)
    assert backend.analogTarget == "Velocity"

    # Toggle off
    cb.setProperty("checked", False)
    cb.toggled.emit()
    _process_events(qml)
    assert backend.analogTarget == "Disabled"


def test_all_spinboxes_keyboard_editable(qml):
    """Accessibility: every DoubleSpinBox across the UI must have editable=True
    to accept typed keyboard input."""
    root = qml.rootObjects()[0]
    spins = [
        c
        for c in root.findChildren(QObject, None)
        if c.metaObject().className().startswith("DoubleSpinBox")
    ]
    assert len(spins) >= 12  # setpoints (3*3) + settings (3) + deadband (3)
    for spin in spins:
        assert spin.property("editable") is True, (
            f"Spinbox {spin} is not keyboard editable"
        )


def test_browser_model_methods_callable_from_qml(backend, qapp, tmp_path):
    """P1 regression guard: ConfigTreeModel.reset()/set_filter() must stay
    @Slot-decorated. Undecorated Python methods on a QObject are invisible
    to QML ("Property 'reset' ... is not a function"), which silently broke
    the browser dialog's Refresh button and name filter once."""
    probe = """
import QtQuick
Item {
    property string result: ""
    Component.onCompleted: {
        try {
            backend.browserModel.reset()
            backend.browserModel.set_filter("vel")
            result = "OK callable from QML"
        } catch (e) {
            result = "FAIL: " + e
        }
    }
}
"""
    path = tmp_path / "browser_probe.qml"
    path.write_text(probe)
    eng = QQmlApplicationEngine()
    eng.rootContext().setContextProperty("backend", backend)
    eng.rootContext().setContextProperty("statusBackend", backend.status_backend)
    eng.load(QUrl.fromLocalFile(str(path)))
    assert eng.rootObjects(), "probe QML failed to load"
    assert eng.rootObjects()[0].property("result") == "OK callable from QML"
    eng.deleteLater()
