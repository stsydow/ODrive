#!/usr/bin/env python3
"""
ODrive Qt GUI - Velocity control focused interface for ODrive Axis 0.
"""

import logging
import os
import sys
import threading

from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGroupBox,
    QLabel,
    QPushButton,
    QComboBox,
    QDoubleSpinBox,
    QMessageBox,
)
from PySide6.QtCore import QTimer, Slot
from PySide6.QtGui import QAction, QFont

# Make the ODrive tools package (tools/odrive) importable when running
# directly from the QtGUI directory without prior installation.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "tools"))

import odrive
import odrive.configuration
from odrive.enums import (
    AXIS_STATE_CLOSED_LOOP_CONTROL,
    AXIS_STATE_ENCODER_DIR_FIND,
    AXIS_STATE_ENCODER_INDEX_SEARCH,
    AXIS_STATE_ENCODER_OFFSET_CALIBRATION,
    AXIS_STATE_FULL_CALIBRATION_SEQUENCE,
    AXIS_STATE_HOMING,
    AXIS_STATE_IDLE,
    AXIS_STATE_LOCKIN_SPIN,
    AXIS_STATE_MOTOR_CALIBRATION,
    CONTROL_MODE_POSITION_CONTROL,
    CONTROL_MODE_TORQUE_CONTROL,
    CONTROL_MODE_VELOCITY_CONTROL,
)

logger = logging.getLogger(__name__)

# Fallback safety net: consecutive read failures (at 100ms poll) before reconnect.
# The odrive library's _on_lost callback is the primary disconnect detection.
RECONNECT_FAIL_THRESHOLD = 50  # ~5 seconds
RECONNECT_RETRY_DELAY_MS = 1000

# Control mode <-> display name
MODE_NAMES = {
    CONTROL_MODE_VELOCITY_CONTROL: "Velocity Control",
    CONTROL_MODE_POSITION_CONTROL: "Position Control",
    CONTROL_MODE_TORQUE_CONTROL: "Torque Control",
}
MODE_VALUES = {name: value for value, name in MODE_NAMES.items()}

# Axis states selectable from the dropdown
STATE_MAP = {
    "AXIS_STATE_FULL_CALIBRATION_SEQUENCE": AXIS_STATE_FULL_CALIBRATION_SEQUENCE,
    "AXIS_STATE_MOTOR_CALIBRATION": AXIS_STATE_MOTOR_CALIBRATION,
    "AXIS_STATE_ENCODER_INDEX_SEARCH": AXIS_STATE_ENCODER_INDEX_SEARCH,
    "AXIS_STATE_ENCODER_OFFSET_CALIBRATION": AXIS_STATE_ENCODER_OFFSET_CALIBRATION,
    "AXIS_STATE_ENCODER_DIR_FIND": AXIS_STATE_ENCODER_DIR_FIND,
    "AXIS_STATE_HOMING": AXIS_STATE_HOMING,
    "AXIS_STATE_LOCKIN_SPIN": AXIS_STATE_LOCKIN_SPIN,
}


class ODriveGUI(QMainWindow):
    """Main ODrive GUI window - Axis 0 velocity control focused."""

    def __init__(self):
        super().__init__()
        self.odrive = None
        self.axis = None
        self.motor = None
        self.encoder = None
        self.controller = None

        self._connecting = False
        self._auto_reconnect = True
        self._read_fail_count = 0
        self._last_synced_mode = None

        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self.update_readings)
        self.update_timer.start(100)  # Update every 100ms

        self.setup_ui()
        self._set_controls_enabled(False)

        # Auto-connect once the event loop is running
        QTimer.singleShot(500, self.connect_odrive)

    def setup_ui(self):
        """Set up the main window UI."""
        self.setWindowTitle("ODrive Qt GUI - Axis 0")
        self.setGeometry(100, 100, 700, 500)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(4)
        main_layout.setContentsMargins(6, 6, 6, 6)

        # ── Menu bar ────────────────────────────────────────────────
        menubar = self.menuBar()
        self.device_menu = menubar.addMenu("&Device")

        save_action = QAction("&Save Config", self)
        save_action.setStatusTip("Save current configuration to device NVM")
        save_action.triggered.connect(self.on_save_config)
        self.device_menu.addAction(save_action)

        export_action = QAction("&Export Config…", self)
        export_action.setStatusTip("Export configuration to a JSON file")
        export_action.triggered.connect(self.on_export_config)
        self.device_menu.addAction(export_action)

        import_action = QAction("&Import Config…", self)
        import_action.setStatusTip("Import configuration from a JSON file")
        import_action.triggered.connect(self.on_import_config)
        self.device_menu.addAction(import_action)

        self.device_menu.addSeparator()

        reboot_action = QAction("Re&boot", self)
        reboot_action.setStatusTip("Reboot the ODrive device")
        reboot_action.triggered.connect(self.on_reboot)
        self.device_menu.addAction(reboot_action)

        # ── Control (run/stop + calibration) ──────────────────────────
        control_layout = QHBoxLayout()
        control_layout.setContentsMargins(0, 0, 0, 0)

        self.run_button = QPushButton("▶ Run (Closed Loop)")
        self.run_button.setStyleSheet("font-size: 14px; padding: 8px;")
        self.run_button.clicked.connect(self.on_run_clicked)
        control_layout.addWidget(self.run_button)

        self.stop_button = QPushButton("■ Stop (Idle)")
        self.stop_button.setStyleSheet("font-size: 14px; padding: 8px;")
        self.stop_button.clicked.connect(self.on_stop_clicked)
        control_layout.addWidget(self.stop_button)

        control_layout.addStretch()

        control_layout.addWidget(QLabel("State:"))
        # Selecting an item only changes the dropdown; the state is executed
        # when the user presses "Execute State". This avoids accidentally
        # triggering calibration routines just by browsing the list.
        self.state_combo = QComboBox()
        self.state_combo.addItems(STATE_MAP.keys())
        control_layout.addWidget(self.state_combo)

        self.calib_button = QPushButton("Execute State")
        self.calib_button.clicked.connect(self.on_calib_clicked)
        control_layout.addWidget(self.calib_button)

        main_layout.addLayout(control_layout)

        # Connection status shown in the status bar (footer)
        self.status_label = QLabel("Not connected")
        self.status_label.setStyleSheet("color: gray; font-weight: bold; padding: 2px 8px;")
        self.statusBar().addPermanentWidget(self.status_label)
        self.statusBar().showMessage("Ready")

        # ── Velocity Control ────────────────────────────────────────
        self.vel_group = QGroupBox("Velocity Control")
        vel_layout = QVBoxLayout(self.vel_group)

        mode_layout = QHBoxLayout()
        mode_layout.addWidget(QLabel("Control Mode:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(MODE_NAMES.values())
        self.mode_combo.currentTextChanged.connect(self.on_mode_changed)
        mode_layout.addWidget(self.mode_combo)
        mode_layout.addStretch()
        vel_layout.addLayout(mode_layout)

        vel_set_layout = QHBoxLayout()
        vel_set_layout.addWidget(QLabel("Velocity Setpoint (rps):"))
        self.vel_spinbox = QDoubleSpinBox()
        self.vel_spinbox.setRange(-100, 100)
        self.vel_spinbox.setDecimals(3)
        self.vel_spinbox.setSingleStep(0.1)
        self.vel_spinbox.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.UpDownArrows)
        self.vel_spinbox.valueChanged.connect(self.on_velocity_changed)
        vel_set_layout.addWidget(self.vel_spinbox)
        vel_set_layout.addStretch()
        vel_layout.addLayout(vel_set_layout)

        # Torque setpoint (hidden by default)
        self.torque_group = QWidget()
        torque_layout = QHBoxLayout(self.torque_group)
        torque_layout.addWidget(QLabel("Torque Setpoint (A):"))
        self.torque_spinbox = QDoubleSpinBox()
        self.torque_spinbox.setRange(-10, 10)
        self.torque_spinbox.setDecimals(3)
        self.torque_spinbox.valueChanged.connect(self.on_torque_changed)
        torque_layout.addWidget(self.torque_spinbox)
        torque_layout.addStretch()
        vel_layout.addWidget(self.torque_group)
        self.torque_group.setVisible(False)

        # Position setpoint (hidden by default)
        self.pos_group = QWidget()
        pos_layout = QHBoxLayout(self.pos_group)
        pos_layout.addWidget(QLabel("Position Setpoint (rev):"))
        self.pos_spinbox = QDoubleSpinBox()
        self.pos_spinbox.setRange(-1e6, 1e6)
        self.pos_spinbox.setDecimals(4)
        self.pos_spinbox.valueChanged.connect(self.on_position_changed)
        pos_layout.addWidget(self.pos_spinbox)
        pos_layout.addStretch()
        vel_layout.addWidget(self.pos_group)
        self.pos_group.setVisible(False)

        main_layout.addWidget(self.vel_group)

        # ── Readings (monitoring) ───────────────────────────────────
        readings_group = QGroupBox("Readings")
        readings_layout = QVBoxLayout()
        self.vbus_label = QLabel("VBus Voltage: -- V")
        readings_layout.addWidget(self.vbus_label)
        self.current_label = QLabel("Motor Current: -- A")
        readings_layout.addWidget(self.current_label)
        self.vel_estimate_label = QLabel("Velocity Estimate: -- rps")
        self.vel_estimate_label.setFont(QFont("Arial", 11, QFont.Weight.Bold))
        readings_layout.addWidget(self.vel_estimate_label)
        self.pos_estimate_label = QLabel("Position Estimate: -- rev")
        readings_layout.addWidget(self.pos_estimate_label)
        self.error_label = QLabel("Error: None")
        self.error_label.setStyleSheet("color: red; font-weight: bold;")
        readings_layout.addWidget(self.error_label)
        readings_group.setLayout(readings_layout)
        main_layout.addWidget(readings_group)

    def closeEvent(self, event):
        """Clean up on window close."""
        self._auto_reconnect = False
        self.update_timer.stop()
        if self.odrive is not None:
            try:
                self.axis.requested_state = AXIS_STATE_IDLE
            except Exception:
                pass
        event.accept()

    # ── Connection ────────────────────────────────────────────────────

    def connect_odrive(self):
        """Start connecting to an ODrive in a background thread."""
        if self._connecting:
            return

        # Clean up stale references
        if self.odrive is not None:
            try:
                self.axis.requested_state = AXIS_STATE_IDLE
            except Exception:
                pass
            self.axis = None
            self.motor = None
            self.encoder = None
            self.controller = None
            self.odrive = None

        self._connecting = True
        self._read_fail_count = 0
        self._last_synced_mode = None
        self.status_label.setText("Connecting...")
        self.status_label.setStyleSheet("color: orange; font-weight: bold;")
        self.statusBar().showMessage("Finding ODrive...")

        threading.Thread(target=self._connect_worker, daemon=True).start()

    def _connect_worker(self):
        """Runs in a background thread. Delivers the result back on the
        main thread via queued QTimer.singleShot calls."""
        try:
            odrv = odrive.find_any()
        except Exception as e:
            QTimer.singleShot(0, lambda: self._on_connect_failed(str(e)))
        else:
            QTimer.singleShot(0, lambda: self._on_connected(odrv))

    def _on_connected(self, odrv):
        """Handle successful connection in the main thread."""
        self.odrive = odrv
        self.axis = odrv.axis0
        self.motor = self.axis.motor
        self.encoder = self.axis.encoder
        self.controller = self.axis.controller
        self._connecting = False
        self._read_fail_count = 0

        # The odrive library notifies us when this device disconnects
        # (its background discovery thread keeps running).
        try:
            if self.odrive._on_lost.done():
                # Device dropped during connection setup; reconnect instead.
                logger.warning("Device lost during connection setup; scheduling reconnect")
                QTimer.singleShot(0, self.connect_odrive)
                return
            self.odrive._on_lost.add_done_callback(self._on_device_lost)
        except Exception as e:
            logger.warning("Could not register lost-connection callback: %s", e)

        self.status_label.setText("Connected")
        self.status_label.setStyleSheet("color: green; font-weight: bold;")
        self.statusBar().showMessage("Connected!")
        self._set_controls_enabled(True)

    def _on_device_lost(self, _future):
        """Called from the odrive discovery thread when the device disconnects.
        Qt widgets must only be touched from the main thread, so we queue the
        reconnect via a timer."""
        if self._auto_reconnect:
            logger.warning("ODrive connection lost (notification)")
            QTimer.singleShot(0, self.connect_odrive)

    def _on_connect_failed(self, msg):
        """Handle connection failure in the main thread."""
        self._connecting = False
        if self._auto_reconnect:
            logger.warning("Connection failed, retrying in %d ms: %s", RECONNECT_RETRY_DELAY_MS, msg)
            self.status_label.setText("Reconnecting...")
            self.status_label.setStyleSheet("color: orange; font-weight: bold;")
            QTimer.singleShot(RECONNECT_RETRY_DELAY_MS, self.connect_odrive)
        else:
            QMessageBox.critical(self, "Connection Error", f"Failed to connect: {msg}")
            self.status_label.setText("Disconnected")
            self.status_label.setStyleSheet("color: red; font-weight: bold;")
            self._set_controls_enabled(False)

    def _set_controls_enabled(self, enabled):
        """Enable or disable all control widgets that require a connection."""
        self.vel_group.setEnabled(enabled)
        self.run_button.setEnabled(enabled)
        self.stop_button.setEnabled(enabled)
        self.state_combo.setEnabled(enabled)
        self.calib_button.setEnabled(enabled)
        self.device_menu.setEnabled(enabled)

    # ── Control handlers ──────────────────────────────────────────────

    def sync_ui_from_controller(self):
        """Sync mode combo and visible spinboxes from actual controller.control_mode.

        Only reads the device when the mode may have changed, to keep USB traffic low.
        """
        if self.controller is None:
            return
        try:
            actual_mode = self.controller.control_mode
        except Exception:
            return

        if actual_mode == self._last_synced_mode:
            return
        self._last_synced_mode = actual_mode

        mode_text = MODE_NAMES.get(actual_mode, "Unknown")
        if mode_text != self.mode_combo.currentText():
            idx = self.mode_combo.findText(mode_text)
            if idx >= 0:
                self.mode_combo.blockSignals(True)
                self.mode_combo.setCurrentIndex(idx)
                self.mode_combo.blockSignals(False)

        self.torque_group.setVisible(actual_mode == CONTROL_MODE_TORQUE_CONTROL)
        self.pos_group.setVisible(actual_mode == CONTROL_MODE_POSITION_CONTROL)

    def _current_control_mode(self):
        """Return the current control mode, or None if controller is unavailable."""
        if self.controller is None:
            return None
        try:
            return self.controller.control_mode
        except Exception:
            return None

    @Slot()
    def on_run_clicked(self):
        """Enter closed-loop control."""
        if self.axis is None:
            return
        self.axis.requested_state = AXIS_STATE_CLOSED_LOOP_CONTROL
        self.statusBar().showMessage("Running - Closed Loop Control")

    @Slot()
    def on_stop_clicked(self):
        """Go to idle."""
        if self.axis is None:
            return
        self.axis.requested_state = AXIS_STATE_IDLE
        self.vel_spinbox.setValue(0)
        self.statusBar().showMessage("Stopped - Idle")

    @Slot(str)
    def on_mode_changed(self, mode):
        """Request a switch to the selected control mode."""
        if self.controller is None:
            return
        new_mode = MODE_VALUES.get(mode)
        if new_mode is None:
            return
        try:
            if self.controller.control_mode == new_mode:
                return
        except Exception:
            pass
        self.controller.control_mode = new_mode
        if new_mode == CONTROL_MODE_VELOCITY_CONTROL:
            self.controller.input_vel = self.vel_spinbox.value()
        elif new_mode == CONTROL_MODE_POSITION_CONTROL:
            self.controller.input_pos = self.pos_spinbox.value()
        elif new_mode == CONTROL_MODE_TORQUE_CONTROL:
            self.controller.input_torque = self.torque_spinbox.value()

    @Slot(float)
    def on_velocity_changed(self, value):
        """Update velocity setpoint (only when in velocity mode)."""
        if self._current_control_mode() == CONTROL_MODE_VELOCITY_CONTROL:
            self.controller.input_vel = value

    @Slot(float)
    def on_torque_changed(self, value):
        """Update torque setpoint (only when in torque mode)."""
        if self._current_control_mode() == CONTROL_MODE_TORQUE_CONTROL:
            self.controller.input_torque = value

    @Slot(float)
    def on_position_changed(self, value):
        """Update position setpoint (only when in position mode)."""
        if self._current_control_mode() == CONTROL_MODE_POSITION_CONTROL:
            self.controller.input_pos = value

    @Slot(str)
    def on_state_changed(self, state_str):
        """Execute a state selected in the dropdown (triggered by Execute State)."""
        if self.axis is None:
            return
        state = STATE_MAP.get(state_str)
        if state is not None:
            self.axis.requested_state = state
        else:
            logger.warning("Unknown axis state requested: %s", state_str)

    @Slot()
    def on_calib_clicked(self):
        """Manually trigger the selected state."""
        self.on_state_changed(self.state_combo.currentText())

    # ── Device actions ───────────────────────────────────────────────

    @Slot()
    def on_save_config(self):
        """Save current configuration to device NVM."""
        if self.odrive is None:
            return
        try:
            self.odrive.save_configuration()
            self.statusBar().showMessage("Configuration saved to device")
        except Exception as e:
            QMessageBox.critical(self, "Save Error", f"Failed to save configuration: {e}")

    @Slot()
    def on_export_config(self):
        """Export configuration to a JSON file."""
        if self.odrive is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Configuration", "", "JSON Files (*.json);;All Files (*)"
        )
        if not path:
            return
        try:
            odrive.configuration.backup_config(self.odrive, path, logger)
            self.statusBar().showMessage(f"Configuration exported to {path}")
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Failed to export configuration: {e}")

    @Slot()
    def on_import_config(self):
        """Import configuration from a JSON file and save to device."""
        if self.odrive is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Configuration", "", "JSON Files (*.json);;All Files (*)"
        )
        if not path:
            return
        reply = QMessageBox.question(
            self, "Confirm Import",
            "Importing will overwrite the device configuration and reboot. Continue?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            odrive.configuration.restore_config(self.odrive, path, logger)
            self.statusBar().showMessage("Configuration imported — device rebooting")
        except Exception as e:
            QMessageBox.critical(self, "Import Error", f"Failed to import configuration: {e}")

    @Slot()
    def on_reboot(self):
        """Reboot the device (auto-reconnect will re-establish the connection)."""
        if self.odrive is None:
            return
        reply = QMessageBox.question(
            self, "Confirm Reboot",
            "Reboot the ODrive device?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        if not hasattr(self.odrive, "reboot"):
            QMessageBox.critical(
                self, "Reboot Error",
                "This firmware does not expose a reboot command."
            )
            return
        try:
            self.status_label.setText("Rebooting...")
            self.status_label.setStyleSheet("color: orange; font-weight: bold;")
            self._set_controls_enabled(False)
            self.odrive.reboot()
            self.statusBar().showMessage("Device rebooting")
        except Exception as e:
            QMessageBox.critical(self, "Reboot Error", f"Failed to reboot: {e}")

    # ── Readings update ───────────────────────────────────────────────

    def update_readings(self):
        """Update displayed values from the ODrive. If reads fail repeatedly
        (and no _on_lost notification arrived), trigger a reconnect."""
        if self.axis is None or self.odrive is None:
            return

        self.sync_ui_from_controller()

        any_failed = False

        try:
            self.vbus_label.setText(f"VBus Voltage: {self.odrive.vbus_voltage:.2f} V")
        except Exception as e:
            logger.debug("Failed to read vbus_voltage: %s", e)
            any_failed = True

        try:
            self.current_label.setText(f"Motor Current: {self.motor.current_measured:.2f} A")
        except Exception as e:
            logger.debug("Failed to read current_measured: %s", e)
            any_failed = True

        try:
            self.vel_estimate_label.setText(f"Velocity Estimate: {self.encoder.vel_estimate:.3f} rps")
        except Exception as e:
            logger.debug("Failed to read vel_estimate: %s", e)
            any_failed = True

        try:
            self.pos_estimate_label.setText(f"Position Estimate: {self.encoder.pos_estimate:.4f} rev")
        except Exception as e:
            logger.debug("Failed to read pos_estimate: %s", e)
            any_failed = True

        try:
            if self.axis.error:
                self.error_label.setText(f"Error: {self.axis.error}")
                self.error_label.setStyleSheet("color: red; font-weight: bold;")
            else:
                self.error_label.setText("Error: None")
                self.error_label.setStyleSheet("color: green; font-weight: bold;")
        except Exception as e:
            logger.debug("Failed to read axis error: %s", e)
            any_failed = True
        # Fallback disconnect detection (primary is _on_lost)
        if any_failed:
            self._read_fail_count += 1
            if (self._auto_reconnect
                    and not self._connecting
                    and self._read_fail_count >= RECONNECT_FAIL_THRESHOLD):
                logger.warning("Read failures, reconnecting...")
                self._set_controls_enabled(False)
                self.connect_odrive()
        else:
            self._read_fail_count = 0


def main():
    """Main entry point."""
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    window = ODriveGUI()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
