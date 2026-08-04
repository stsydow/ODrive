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
from odrive.utils import dump_errors

# Import fibre for ObjectLostError disconnect detection
import fibre

from odrive.enums import (
    AXIS_ERROR_NONE,
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
    AxisError,
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

    def __init__(self, verbose=False):
        super().__init__()
        self.odrive = None
        self.axis = None
        self.motor = None
        self.encoder = None
        self.controller = None

        self._connecting = False
        self._read_fail_count = 0
        self._last_synced_mode = None
        self._last_read_error = None
        self._verbose = verbose

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

        self.device_menu.addSeparator()

        clear_errors_action = QAction("Clear Errors", self)
        clear_errors_action.setStatusTip("Clear all axis and system errors on the device")
        clear_errors_action.triggered.connect(self._on_clear_errors)
        self.device_menu.addAction(clear_errors_action)

        dump_errors_action = QAction("Dump Errors…", self)
        dump_errors_action.setStatusTip("Print detailed error info to the console")
        dump_errors_action.triggered.connect(self._on_dump_errors)
        self.device_menu.addAction(dump_errors_action)

        self.device_menu.addSeparator()

        info_action = QAction("Device Info…", self)
        info_action.setStatusTip("Show serial number, firmware version and status")
        info_action.triggered.connect(self._on_show_device_info)
        self.device_menu.addAction(info_action)

        dump_action = QAction("Dump Read Failures…", self)
        dump_action.setStatusTip("Show the current read-failure counter")
        dump_action.triggered.connect(self._on_dump_state)
        self.device_menu.addAction(dump_action)

        # ── Debug menu ────────────────────────────────────────────────
        debug_menu = menubar.addMenu("&Debug")

        self.verbose_action = QAction("Verbose Logging", self, checkable=True)
        self.verbose_action.setChecked(self._verbose)
        self.verbose_action.setStatusTip("Enable DEBUG-level logging to console")
        self.verbose_action.toggled.connect(self._on_verbose_toggled)
        debug_menu.addAction(self.verbose_action)

        reconnect_action = QAction("Force Reconnect", self)
        reconnect_action.setStatusTip("Drop the current connection and reconnect")
        reconnect_action.triggered.connect(self.connect_odrive)
        debug_menu.addAction(reconnect_action)

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
        self.statusBar().showMessage("Ready", 0)

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
            logger.debug("connect_odrive: already connecting, skipping")
            return

        logger.debug("connect_odrive: called (odrive=%s, axis=%s)",
                     self.odrive is not None, self.axis is not None)

        # Clean up stale references
        if self.odrive is not None:
            try:
                self.axis.requested_state = AXIS_STATE_IDLE
                logger.debug("connect_odrive: set axis to IDLE")
            except Exception as ex:
                logger.debug("connect_odrive: set IDLE failed: %s", ex)
            self.axis = None
            self.motor = None
            self.encoder = None
            self.controller = None
            self.odrive = None
            logger.debug("connect_odrive: stale references cleared")

        self._connecting = True
        self._read_fail_count = 0
        self._last_synced_mode = None
        self.status_label.setText("Connecting...")
        self.status_label.setStyleSheet("color: orange; font-weight: bold;")
        self.statusBar().showMessage("Finding ODrive...", 0)

        logger.debug("connect_odrive: spawning _connect_worker thread")
        threading.Thread(target=self._connect_worker, daemon=True).start()

    def _connect_worker(self):
        """Runs in a background thread (daemon). Calls odrive.find_any()
        which blocks until a device is discovered, then delivers the result
        to the main thread via QTimer.singleShot.

        The odrive library starts a background discovery thread on the
        first call, so subsequent calls return immediately if a device is
        already known.
        """
        logger.debug("connect_worker: thread started, calling odrive.find_any()...")
        try:
            odrv = odrive.find_any()
            logger.debug("connect_worker: find_any() returned device %s", odrv)
        except Exception as e:
            # Capture the message string before the exception variable is
            # cleared (Python 3.14+ deletes it at the end of the except block).
            msg = str(e)
            logger.debug("connect_worker: find_any() raised: %s", msg)
            QTimer.singleShot(0, self, lambda: self._on_connect_failed(msg))
            return
        logger.debug("connect_worker: scheduling _on_connected on main thread")
        QTimer.singleShot(0, self, lambda: self._on_connected(odrv))

    def _on_connected(self, odrv):
        """Handle successful connection in the main thread."""
        logger.debug("on_connected: wiring up device")
        self.odrive = odrv
        self.axis = odrv.axis0
        self.motor = self.axis.motor
        self.encoder = self.axis.encoder
        self.controller = self.axis.controller
        self._connecting = False
        self._read_fail_count = 0
        logger.debug("on_connected: axis0 wired (motor=%s, encoder=%s, controller=%s)",
                     self.motor is not None, self.encoder is not None, self.controller is not None)

        # The odrive library notifies us when this device disconnects
        # (its background discovery thread keeps running).
        try:
            logger.debug("on_connected: checking _on_lost state")
            if self.odrive._on_lost.done():
                logger.warning("on_connected: device already lost during setup, reconnecting")
                QTimer.singleShot(0, self.connect_odrive)
                return
            self.odrive._on_lost.add_done_callback(self._on_device_lost)
            logger.debug("on_connected: _on_device_lost callback registered")
        except Exception as e:
            logger.warning("on_connected: _on_lost registration failed: %s", e)

        self.status_label.setText("Connected")
        self.status_label.setStyleSheet("color: green; font-weight: bold;")
        self.statusBar().showMessage("Connected!", 0)
        self._set_controls_enabled(True)
        logger.info("Connected to ODrive")

    def _on_device_lost(self, _future):
        """Called from the odrive discovery thread when the device disconnects.
        Qt widgets must only be touched from the main thread, so we queue the
        reconnect via a timer."""
        logger.warning("on_device_lost: connection lost (thread=%s, future.done=%s)",
                       threading.current_thread().name, _future.done())
        QTimer.singleShot(0, self, self.connect_odrive)
    def _on_connect_failed(self, msg):
        """Handle connection failure in the main thread."""
        self._connecting = False
        logger.warning("on_connect_failed: %s (retry in %d ms)", msg, RECONNECT_RETRY_DELAY_MS)
        self.status_label.setText("Reconnecting...")
        self.status_label.setStyleSheet("color: orange; font-weight: bold;")
        self.statusBar().showMessage("Finding ODrive...", 0)
        QTimer.singleShot(RECONNECT_RETRY_DELAY_MS, self.connect_odrive)

    def _set_controls_enabled(self, enabled):
        """Enable or disable all control widgets that require a connection."""
        self.vel_group.setEnabled(enabled)
        self.run_button.setEnabled(enabled)
        self.stop_button.setEnabled(enabled)
        self.state_combo.setEnabled(enabled)
        self.calib_button.setEnabled(enabled)

    # ── Control handlers ──────────────────────────────────────────────

    def sync_ui_from_controller(self):
        """Sync mode combo and visible spinboxes from actual controller.config.control_mode.

        Only reads the device when the mode may have changed, to keep USB traffic low.
        """
        if self.controller is None:
            return
        try:
            actual_mode = self.controller.config.control_mode
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
            return self.controller.config.control_mode
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
            if self.controller.config.control_mode == new_mode:
                return
        except Exception:
            pass
        try:
            self.controller.config.control_mode = new_mode
            if new_mode == CONTROL_MODE_VELOCITY_CONTROL:
                self.controller.input_vel = self.vel_spinbox.value()
            elif new_mode == CONTROL_MODE_POSITION_CONTROL:
                self.controller.input_pos = self.pos_spinbox.value()
            elif new_mode == CONTROL_MODE_TORQUE_CONTROL:
                self.controller.input_torque = self.torque_spinbox.value()
            self.statusBar().showMessage(f"Control mode set to {mode}", 3000)
        except Exception as e:
            logger.warning("Failed to set control mode %s: %s", mode, e)
            self.statusBar().showMessage(f"Failed to set control mode: {e}", 3000)

    @Slot(float)
    def on_velocity_changed(self, value):
        """Update velocity setpoint (only when in velocity mode)."""
        if self._current_control_mode() == CONTROL_MODE_VELOCITY_CONTROL:
            try:
                self.controller.input_vel = value
            except Exception as e:
                logger.debug("Failed to set input_vel: %s", e)

    @Slot(float)
    def on_torque_changed(self, value):
        """Update torque setpoint (only when in torque mode)."""
        if self._current_control_mode() == CONTROL_MODE_TORQUE_CONTROL:
            try:
                self.controller.input_torque = value
            except Exception as e:
                logger.debug("Failed to set input_torque: %s", e)

    @Slot(float)
    def on_position_changed(self, value):
        """Update position setpoint (only when in position mode)."""
        if self._current_control_mode() == CONTROL_MODE_POSITION_CONTROL:
            try:
                self.controller.input_pos = value
            except Exception as e:
                logger.debug("Failed to set input_pos: %s", e)

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

    # ── Debug helpers ─────────────────────────────────────────────────

    @Slot(bool)
    def _on_verbose_toggled(self, checked):
        """Toggle DEBUG-level logging on the root logger."""
        logging.getLogger().setLevel(logging.DEBUG if checked else logging.INFO)
        logger.info("Verbose logging %s", "enabled" if checked else "disabled")
        self.statusBar().showMessage("Verbose logging enabled" if checked
                                     else "Verbose logging disabled", 3000)

    @Slot()
    def _on_show_device_info(self):
        """Show serial number, firmware version and live status."""
        if self.odrive is None:
            QMessageBox.information(self, "Device Info", "Not connected")
            return
        try:
            serial = odrive.get_serial_number_str_sync(self.odrive)
        except Exception:
            serial = "unknown"
        try:
            fw = ".".join(str(x) for x in (
                self.odrive.fw_version_major,
                self.odrive.fw_version_minor,
                self.odrive.fw_version_revision,
            ))
        except Exception:
            fw = "unknown"
        try:
            vbus = self.odrive.vbus_voltage
        except Exception:
            vbus = None
        lines = [
            f"Serial number: {serial}",
            f"Firmware: {fw}",
            f"VBus: {vbus:.2f} V" if vbus is not None else "VBus: unknown",
            f"Axis0 error: {self.axis.error if self.axis is not None else 'n/a'}",
            f"Read failures: {self._read_fail_count}",
        ]
        logger.info("Device info:\n%s", "\n".join(lines))
        QMessageBox.information(self, "Device Info", "\n".join(lines))

    @Slot()
    def _on_dump_state(self):
        """Show internal connection state for debugging."""
        lines = [
            f"Connecting: {self._connecting}",
            f"Connected: {self.odrive is not None}",
            f"Read failures: {self._read_fail_count} (threshold {RECONNECT_FAIL_THRESHOLD})",
            f"Last synced mode: {self._last_synced_mode}",
            f"Retry delay: {RECONNECT_RETRY_DELAY_MS} ms",
        ]
        logger.info("Internal state:\n%s", "\n".join(lines))
        QMessageBox.information(self, "Internal State", "\n".join(lines))

    @Slot()
    def _on_clear_errors(self):
        """Clear all errors on the device."""
        if self.odrive is None:
            return
        try:
            self.odrive.clear_errors()
            logger.info("Cleared all errors")
            self.statusBar().showMessage("Cleared all errors", 3000)
        except Exception as e:
            logger.warning("Failed to clear errors: %s", e)
            QMessageBox.critical(self, "Clear Errors", f"Failed to clear errors: {e}")

    @Slot()
    def _on_dump_errors(self):
        """Capture dump_errors() output and show it in a dialog."""
        if self.odrive is None:
            QMessageBox.information(self, "Dump Errors", "Not connected")
            return
        try:
            import io, re
            buf = io.StringIO()
            dump_errors(self.odrive, printfunc=lambda x: print(x, file=buf))
            raw = buf.getvalue()
            # Strip ANSI colour codes
            text = re.sub(r"\x1b\[[0-9;]*m", "", raw)
            logger.info("Error dump:\n%s", text)
            QMessageBox.information(self, "Error Dump", text.strip())
        except Exception as e:
            logger.warning("Failed to dump errors: %s", e)
            QMessageBox.critical(self, "Dump Errors", f"Failed to dump errors: {e}")

    # ── Readings update ───────────────────────────────────────────────

    def _read_failed(self, name, exc):
        """Handle a read failure. Returns True if it's a device disconnect
        (ObjectLostError), or False if it's a non-fatal error (e.g. attribute
        not supported on this firmware).

        Non-fatal errors are logged at DEBUG only on the first occurrence
        to avoid spamming the log at 10 Hz.
        """
        if isinstance(exc, fibre.libfibre.ObjectLostError):
            logger.debug("Failed to read %s: device lost", name)
            return True
        msg = f"{name}: {exc}"
        if msg != self._last_read_error:
            logger.debug("Failed to read %s", msg)
            self._last_read_error = msg
        return False

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
            any_failed |= self._read_failed("vbus_voltage", e)

        try:
            self.current_label.setText(f"Motor Current: {self.odrive.ibus:.2f} A")
        except Exception as e:
            any_failed |= self._read_failed("ibus", e)

        try:
            self.vel_estimate_label.setText(f"Velocity Estimate: {self.encoder.vel_estimate:.3f} rps")
        except Exception as e:
            any_failed |= self._read_failed("vel_estimate", e)

        try:
            self.pos_estimate_label.setText(f"Position Estimate: {self.encoder.pos_estimate:.4f} rev")
        except Exception as e:
            any_failed |= self._read_failed("pos_estimate", e)

        try:
            if self.axis.error:
                self.error_label.setText(f"Error: {self.axis.error}")
                self.error_label.setStyleSheet("color: red; font-weight: bold;")
            else:
                self.error_label.setText("Error: None")
                self.error_label.setStyleSheet("color: green; font-weight: bold;")
        except Exception as e:
            any_failed |= self._read_failed("axis.error", e)
        # Fallback disconnect detection (primary is _on_lost)
        if any_failed:
            self._read_fail_count += 1
            if self._read_fail_count % 10 == 0:
                logger.debug("update_readings: %d consecutive read failures", self._read_fail_count)
            if (not self._connecting
                    and self._read_fail_count >= RECONNECT_FAIL_THRESHOLD):
                logger.warning("update_readings: fallback reconnect triggered after %d failures",
                               self._read_fail_count)
                self._set_controls_enabled(False)
                self.connect_odrive()
        else:
            if self._read_fail_count > 0:
                logger.debug("update_readings: reads recovered after %d failures", self._read_fail_count)
            self._read_fail_count = 0


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="ODrive Qt GUI - Axis 0")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable DEBUG-level logging at startup")
    args, _ = parser.parse_known_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    window = ODriveGUI(verbose=args.verbose)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
