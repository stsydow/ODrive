"""pytest fixtures for the QtGUI tests.

Runs headless with the Qt offscreen platform plugin (no display needed) and a
mock ODrive device (no hardware). `QT_QPA_PLATFORM` must be set before any Qt
application is created, and the ODrive tools path must be on sys.path before
`backend`/`odrive` are imported — both are done here at import time, mirroring
the runtime bootstrap in `main.py`.
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QTGUI = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(QTGUI))
sys.path.insert(0, str(QTGUI.parent / "tools"))
sys.path.insert(0, str(QTGUI.parent / "tools" / "odrive" / "pyfibre"))

import odrive  # noqa: E402,F401  (import side effects: register discovery/types)
import pytest  # noqa: E402
from PySide6.QtCore import QLocale, QUrl  # noqa: E402

# Pin the locale so QML's Qt.locale() parsing (used by the Enter/Apply path)
# is deterministic regardless of host OS language — de_DE would reject "2.5"
# (comma decimal) and break the setpoint tests. Must precede QApplication.
QLocale.setDefault(QLocale(QLocale.English, QLocale.UnitedStates))
from PySide6.QtQml import QQmlApplicationEngine  # noqa: E402
from PySide6.QtQuickControls2 import QQuickStyle  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from backend import GuiBackend  # noqa: E402

# ── Mock ODrive device ──────────────────────────────────────────────────
# Faithful enough to exercise GuiBackend's controls without hardware: a
# device root with axis0 -> controller/motor/encoder/config, and the
# `_on_lost` lifecycle hook the backend registers on connect.


class MockConfig:
    """Simple attribute holder for a `*.config` object."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class MockController:
    def __init__(self):
        self.config = MockConfig(
            control_mode=2,  # VELOCITY_CONTROL
            input_mode=2,  # VEL_RAMP
            vel_gain=0.5,
            vel_limit=88.0,
            circular_setpoints=False,
            enable_vel_limit=True,
            enable_torque_mode_vel_limit=False,
            enable_gain_scheduling=False,
        )
        self.input_vel = 1.0
        self.input_torque = 0.0
        self.input_pos = 0.0
        # Target endpoints the analog (pedal) mapping can point at.
        self._input_vel_property = object()
        self._input_torque_property = object()
        self._input_pos_property = object()
        self.error = 0


class MockMotor:
    def __init__(self):
        # motor_name: a string-valued config leaf (read-only display rule).
        self.config = MockConfig(
            current_lim=12.5, current_lim_margin=2.0, motor_name="sew motor"
        )
        self.error = 0


class MockEncoder:
    def __init__(self):
        self.vel_estimate = 1.5
        self.pos_estimate = 2.5
        self.error = 0


class MockGpioMapping:
    """gpioN_analog_mapping: an ADC->endpoint mapping (firmware low_level)."""

    def __init__(self):
        self.endpoint = None
        self.min = 0.0
        self.max = 0.0
        self.deadband_enable = False
        self.deadband_start = 0.0
        self.deadband_end = 0.0
        self.deadband_level = 0.0
        self.deadband_idle = False


class MockAxis:
    def __init__(self, current_state=1):  # 1 = IDLE
        self.config = MockConfig()
        self.controller = MockController()
        self.motor = MockMotor()
        self.encoder = MockEncoder()
        self.sensorless_estimator = MockConfig(error=0)
        self.current_state = current_state
        self.error = 0
        self.requested_state = None


class MockDevice:
    """Minimal ODrive root: axis0 + bus readouts + _on_lost lifecycle hook."""

    def __init__(self):
        self.axis0 = MockAxis()
        self.config = MockConfig(
            gpio3_mode=0,
            gpio3_analog_mapping=MockGpioMapping(),
            gpio4_mode=0,
            gpio4_analog_mapping=MockGpioMapping(),
        )
        self.error = 0
        self.vbus_voltage = 24.0
        self.ibus = 0.3
        self.fw_version_major = 1
        self.fw_version_minor = 2
        self.fw_version_revision = 3
        self.hw_version_major = 3
        self.hw_version_minor = 6
        self.hw_version_variant = "6"
        self._on_lost = _Future()

    def save_configuration(self):
        pass

    def clear_errors(self):
        pass

    def get_serial_number_str_sync(self):
        return "MOCK-SERIAL"


class _Future:
    """Stand-in for the odrive library's _on_lost future (callbacks storable
    so tests can simulate device loss)."""

    def __init__(self):
        self._cbs = []

    def done(self):
        return False

    def add_done_callback(self, cb):
        self._cbs.append(cb)


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def qapp():
    """One QApplication for the whole session (Qt allows only one)."""
    app = QApplication.instance() or QApplication([])
    if QQuickStyle:
        QQuickStyle.setStyle("Fusion")
    yield app


@pytest.fixture
def mock_device():
    return MockDevice()


@pytest.fixture
def backend(mock_device, qapp):
    """A GuiBackend wired to a mock device, ready to act on it.

    Depends on `qapp` first so the QApplication exists before GuiBackend's
    QTimer is created (QTimer/QQmlEngine require a runnable app). The
    auto-connect thread is disabled (connectOdrive no-ops) and the device is
    attached directly, so no hardware/networking is involved.
    """
    b = GuiBackend()
    b.connectOdrive = lambda: None  # never spawn the discovery thread
    b.odrive = mock_device
    b._load_analog_bounds()
    b.status_backend.set_conn("\u25cf Online", "green", True)
    b._input_mode_model_for(2)
    yield b
    b.update_timer.stop()


@pytest.fixture
def qml(backend):
    """Load qml/main.qml with `backend` + `statusBackend` as context properties."""
    eng = QQmlApplicationEngine()
    eng.rootContext().setContextProperty("backend", backend)
    eng.rootContext().setContextProperty("statusBackend", backend.status_backend)
    eng.load(QUrl.fromLocalFile(str(QTGUI / "qml" / "main.qml")))
    assert eng.rootObjects(), "QML failed to load"
    yield eng
    # Close any open dialog windows so a later test starts clean.
    for w in eng.rootObjects():
        if hasattr(w, "close"):
            w.close()
    eng.deleteLater()
    # Process deferred deletes NOW, while `backend`/`statusBackend` are still
    # alive. If destruction is left to the next event-loop spin (or process
    # exit), QML re-evaluates bindings against the already-garbage-collected
    # backend and floods stderr with "Cannot read property ... of null" +
    # StatusBackend AttributeErrors.
    from PySide6.QtCore import QCoreApplication, QEvent

    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
