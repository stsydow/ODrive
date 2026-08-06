#!/usr/bin/env python3
"""
ODrive Qt GUI - Velocity control focused interface for ODrive Axis 0.
"""

import logging
import os
import signal
import sys
import threading
from collections import deque

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
from PySide6.QtCore import QTimer, Slot, Signal, Qt
from PySide6.QtGui import QAction

# Make the ODrive tools package (tools/odrive) importable when running
# directly from the QtGUI directory without prior installation.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "tools"))

import odrive
import odrive.configuration
import odrive.enums

# Phase 1: Control Settings (Plan.md §1)
from controls import ControlParamsGroup, InputModeSelector, LimitsTabs

# Phase 2: Error display & history (Plan.md §2)
from monitoring import ErrorDialog, read_error_report

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
RECONNECT_FAIL_THRESHOLD = 5  # ~0.5 seconds
RECONNECT_RETRY_DELAY_MS = 1000

# Control mode <-> display name
MODE_NAMES = {
    CONTROL_MODE_VELOCITY_CONTROL: "Velocity Control",
    CONTROL_MODE_POSITION_CONTROL: "Position Control",
    CONTROL_MODE_TORQUE_CONTROL: "Torque Control",
}
MODE_VALUES = {name: value for value, name in MODE_NAMES.items()}

# Axis states selectable from the dropdown (friendly label -> value).
STATE_MAP = {
    "Full Calibration Sequence": AXIS_STATE_FULL_CALIBRATION_SEQUENCE,
    "Motor Calibration": AXIS_STATE_MOTOR_CALIBRATION,
    "Encoder Index Search": AXIS_STATE_ENCODER_INDEX_SEARCH,
    "Encoder Offset Calibration": AXIS_STATE_ENCODER_OFFSET_CALIBRATION,
    "Encoder Direction Find": AXIS_STATE_ENCODER_DIR_FIND,
    "Homing": AXIS_STATE_HOMING,
    "Lock-In Spin": AXIS_STATE_LOCKIN_SPIN,
}

# Reverse map: axis-state value -> short display name (status bar).
AXIS_STATE_NAMES = {
    v: n.replace("AXIS_STATE_", "")
    for n, v in vars(odrive.enums).items()
    if n.startswith("AXIS_STATE_")
}


def maybe_read(fn, default=None):
    """Return `fn()` if it succeeds, else `default` (None).

    Centralises "get a value if available" so truly-optional device reads
    (monitor values, live state) are handled consistently and logged once,
    instead of scattered ``try/except: pass`` blocks that tend to rot into
    hard-to-find bugs. The caller must handle `None`.

    Only use for genuinely optional reads. Reads that must distinguish error
    classes (e.g. ObjectLostError -> reconnect) keep explicit, targeted
    try/except (see `_read_failed`). Never use a bare ``except: pass``.
    """
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - optional read, default on any failure
        logger.debug("read failed (defaulting to %r): %s", default, e)
        return default



class _ClickableLabel(QLabel):
    """QLabel that emits `clicked` on mouse press (e.g. footer error field)."""

    clicked = Signal()

    def mousePressEvent(self, event):
        self.clicked.emit()
        super().mousePressEvent(event)


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
        self._connected = False
        self._read_fail_count = 0
        self._last_synced_mode = None
        self._last_read_error = None
        self._last_report = None
        self._last_error_key = None
        self.error_history = deque(maxlen=1000)
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

        errors_action = QAction("Errors…", self)
        errors_action.setStatusTip("Show decoded errors and error history")
        errors_action.triggered.connect(self._on_show_error_history)
        self.device_menu.addAction(errors_action)

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

        control_layout.addWidget(QLabel("Programm:"))
        # Selecting an item only changes the dropdown; the program is started
        # when the user presses "Start". This avoids accidentally triggering
        # calibration routines just by browsing the list.
        self.state_combo = QComboBox()
        self.state_combo.addItems(STATE_MAP.keys())
        control_layout.addWidget(self.state_combo)

        self.calib_button = QPushButton("Start")
        self.calib_button.clicked.connect(self.on_calib_clicked)
        control_layout.addWidget(self.calib_button)

        main_layout.addLayout(control_layout)

        # Status footer: connection / error / bus voltage / power draw.
        # A composed permanent widget (no duplicate connection text overlap).
        status_widget = QWidget()
        status_layout = QHBoxLayout(status_widget)
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.setSpacing(14)
        self.conn_label = QLabel("● Offline")
        self.conn_label.setStyleSheet("color: gray; font-weight: bold; padding: 2px 6px;")
        self.state_status_label = QLabel("State: --")
        self.error_status_label = _ClickableLabel("Err: OK")
        self.error_status_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.error_status_label.setToolTip("Click to open the decoded errors / history")
        self.error_status_label.setStyleSheet("color: gray;")
        self.error_status_label.clicked.connect(self._on_show_error_history)
        self.vbus_status_label = QLabel("-- V")
        self.power_status_label = QLabel("-- W")
        status_layout.addWidget(self.conn_label)
        status_layout.addWidget(self.state_status_label)
        status_layout.addWidget(self.error_status_label)
        status_layout.addWidget(self.vbus_status_label)
        status_layout.addWidget(self.power_status_label)
        self.statusBar().addPermanentWidget(status_widget)
        self.statusBar().showMessage("Ready", 0)

        # ── Control Command (setpoints, disabled unless closed-loop) ──
        self.cmd_group = QGroupBox("Control Command")
        vel_layout = QVBoxLayout(self.cmd_group)

        mode_layout = QHBoxLayout()
        mode_layout.addWidget(QLabel("Control Mode:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(MODE_NAMES.values())
        self.mode_combo.currentTextChanged.connect(self.on_mode_changed)
        mode_layout.addWidget(self.mode_combo)
        mode_layout.addSpacing(16)
        # Input mode: restricted to modes valid for the current control mode.
        mode_layout.addWidget(QLabel("Input Mode:"))
        self.input_selector = InputModeSelector(self.statusBar().showMessage)
        mode_layout.addWidget(self.input_selector)
        mode_layout.addStretch()
        vel_layout.addLayout(mode_layout)

        # Setpoint rows share one consistent layout via the helpers below:
        #   [label] [spinbox] [estimate?] [stretch]
        # Only the row matching the current control mode is visible (see
        # sync_ui_from_controller); setpoints are written only on explicit
        # confirmation (Apply button / Enter key — see _apply_setpoint).
        self.vel_spinbox = self._make_setpoint_spin(-100.0, 100.0, 3)
        self.vel_spinbox.setSingleStep(0.1)
        self.vel_estimate_label = QLabel("est: -- rps")
        self.vel_estimate_label.setStyleSheet("color: gray;")
        self.vel_group = self._make_setpoint_row(
            "Velocity Setpoint (rps):", self.vel_spinbox, self.vel_estimate_label)
        vel_layout.addWidget(self.vel_group)

        # Torque setpoint (hidden by default)
        self.torque_spinbox = self._make_setpoint_spin(-10.0, 10.0, 3)
        self.torque_group = self._make_setpoint_row(
            "Torque Setpoint (A):", self.torque_spinbox)
        vel_layout.addWidget(self.torque_group)
        self.torque_group.setVisible(False)

        # Position setpoint (hidden by default)
        self.pos_spinbox = self._make_setpoint_spin(-1e6, 1e6, 4)
        self.pos_estimate_label = QLabel("est: -- rev")
        self.pos_estimate_label.setStyleSheet("color: gray;")
        self.pos_group = self._make_setpoint_row(
            "Position Setpoint (rev):", self.pos_spinbox, self.pos_estimate_label)
        vel_layout.addWidget(self.pos_group)
        self.pos_group.setVisible(False)

        # Confirmed setpoint apply (monitor-only: adjusting a spinbox never
        # moves the motor; the user confirms via Apply or Enter).
        apply_layout = QHBoxLayout()
        self.apply_button = QPushButton("Apply Setpoint")
        self.apply_button.setToolTip(
            "Send the current setpoint to the device.\n"
            "Press Enter in the setpoint box as a shortcut."
        )
        self.apply_button.clicked.connect(self._apply_setpoint)
        apply_layout.addWidget(self.apply_button)
        apply_layout.addStretch()
        vel_layout.addLayout(apply_layout)

        main_layout.addWidget(self.cmd_group)

        # ── Control Settings (Phase 1, collapsible) ─────────────────
        self.controls_group = QGroupBox("Control Settings")
        self.controls_group.setCheckable(True)
        self.controls_group.setChecked(True)
        self.controls_group.setToolTip("Untick to collapse")
        cg_layout = QVBoxLayout(self.controls_group)
        self.control_params = ControlParamsGroup(self.statusBar().showMessage)
        self.limits_tabs = LimitsTabs(self.statusBar().showMessage)
        cg_layout.addWidget(self.control_params)
        cg_layout.addWidget(self.limits_tabs)
        self.controls_group.toggled.connect(self._on_controls_collapsed)
        main_layout.addWidget(self.controls_group)

        # Note: the separate "Readings" group was removed; live estimates now
        # live beside their setpoints in the Control Command section and the
        # footer shows bus voltage / power.

    def closeEvent(self, event):
        """Clean up on window close.

        UI-only: never touches the motor. The motor keeps running after the
        GUI is closed (see Plan.md §4.6). Only the explicit Stop button or a
        user-selected axis state commands the device.
        """
        self.update_timer.stop()
        event.accept()

    # ── Connection ────────────────────────────────────────────────────

    def connect_odrive(self):
        """Start connecting to an ODrive in a background thread."""
        if self._connecting:
            logger.debug("connect_odrive: already connecting, skipping")
            return

        logger.debug("connect_odrive: called (odrive=%s, axis=%s)",
                     self.odrive is not None, self.axis is not None)

        # Clean up stale references (UI-only: never stops the motor)
        if self.odrive is not None:
            self.axis = None
            self.motor = None
            self.encoder = None
            self.controller = None
            self.odrive = None
            logger.debug("connect_odrive: stale references cleared")

        self._connecting = True
        self._read_fail_count = 0
        self._last_synced_mode = None
        self._set_conn("● Connecting…", "orange")

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

        # Phase 1 control settings: load current device values + feature gate
        self.input_selector.bind(self.controller)
        self.control_params.bind(self.controller, self.motor, self.odrive)
        self.limits_tabs.bind(self.controller, self.motor, self.odrive)
        # Show the device's actual setpoint on connect.
        self._sync_setpoint_from_device()

        self._set_conn("● Online", "green")
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
        self._set_conn("● Offline (retrying)", "red")
        QTimer.singleShot(RECONNECT_RETRY_DELAY_MS, self.connect_odrive)

    def _set_conn(self, text, color):
        """Set the permanent connection-indicator label in the status footer."""
        self.conn_label.setText(text)
        self.conn_label.setStyleSheet(
            f"color: {color}; font-weight: bold; padding: 2px 6px;")

    def _set_controls_enabled(self, enabled):
        """Enable or disable all control widgets that require a connection."""
        self._connected = enabled
        self.run_button.setEnabled(enabled)
        self.stop_button.setEnabled(enabled)
        self.state_combo.setEnabled(enabled)
        self.calib_button.setEnabled(enabled)
        self.controls_group.setEnabled(enabled)
        self._update_control_enabled()

    def _update_control_enabled(self):
        """Gate the Control Command setpoint inputs on closed-loop control.

        The command inputs (setpoint spinboxes + Apply) are only usable while
        the axis is actually running closed-loop. The control-mode combo stays
        available whenever connected so the user can select the mode before
        running (a disabled parent would otherwise force-disable the combo).
        """
        running = False
        if self._connected and self.axis is not None:
            running = maybe_read(
                lambda: self.axis.current_state == AXIS_STATE_CLOSED_LOOP_CONTROL,
                default=False)
        cmd_ok = self._connected and running
        self.cmd_group.setEnabled(self._connected)
        self.vel_spinbox.setEnabled(cmd_ok)
        self.torque_spinbox.setEnabled(cmd_ok)
        self.pos_spinbox.setEnabled(cmd_ok)
        self.apply_button.setEnabled(cmd_ok)

    @Slot(bool)
    def _on_controls_collapsed(self, checked):
        """Collapse/expand the Control Settings panel contents."""
        self.control_params.setVisible(checked)
        self.limits_tabs.setVisible(checked)

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

        self.vel_group.setVisible(actual_mode == CONTROL_MODE_VELOCITY_CONTROL)
        self.torque_group.setVisible(actual_mode == CONTROL_MODE_TORQUE_CONTROL)
        self.pos_group.setVisible(actual_mode == CONTROL_MODE_POSITION_CONTROL)
        # Restrict the input-mode box to modes valid for this control mode.
        self.input_selector.set_control_mode(actual_mode)

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
        # Show the device's actual setpoint, not a stale/reset local value.
        self._sync_setpoint_from_device()
        self.statusBar().showMessage("Running - Closed Loop Control")

    @Slot()
    def on_stop_clicked(self):
        """Go to idle."""
        if self.axis is None:
            return
        self.axis.requested_state = AXIS_STATE_IDLE
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
            # Update the visible setpoint row immediately (not only at the
            # next 100ms poll).
            self.sync_ui_from_controller()
            self.statusBar().showMessage(f"Control mode set to {mode}", 3000)
        except Exception as e:
            logger.warning("Failed to set control mode %s: %s", mode, e)
            self.statusBar().showMessage(f"Failed to set control mode: {e}", 3000)

    def _sync_setpoint_from_device(self):
        """Populate the active setpoint spinbox from the device's current
        input setpoint (device truth). Called on connect and when entering
        closed-loop so the display isn't a stale/reset local value."""
        if self.controller is None:
            return
        mode = self._current_control_mode()
        try:
            if mode == CONTROL_MODE_VELOCITY_CONTROL:
                v = self.controller.input_vel
                if v is not None:
                    self.vel_spinbox.blockSignals(True)
                    self.vel_spinbox.setValue(float(v))
                    self.vel_spinbox.blockSignals(False)
            elif mode == CONTROL_MODE_TORQUE_CONTROL:
                t = self.controller.input_torque
                if t is not None:
                    self.torque_spinbox.blockSignals(True)
                    self.torque_spinbox.setValue(float(t))
                    self.torque_spinbox.blockSignals(False)
            elif mode == CONTROL_MODE_POSITION_CONTROL:
                p = self.controller.input_pos
                if p is not None:
                    self.pos_spinbox.blockSignals(True)
                    self.pos_spinbox.setValue(float(p))
                    self.pos_spinbox.blockSignals(False)
        except Exception as e:
            logger.debug("sync setpoint from device failed: %s", e)

    def _position_circular_range(self):
        """Return the circular setpoint range when circular position mode is
        active, else None. Used to wrap the displayed estimate into
        [0, range) so it matches the device's circular setpoint behaviour."""
        if self.controller is None:
            return None
        try:
            if self.controller.config.circular_setpoints:
                rng = self.controller.config.circular_setpoint_range
                if rng and float(rng) > 0:
                    return float(rng)
        except Exception:
            pass
        return None

    def _make_setpoint_spin(self, minimum, maximum, decimals):
        """Build a setpoint spinbox. No valueChanged write: the value reaches
        the device only via explicit confirmation (Apply / Enter)."""
        sb = QDoubleSpinBox()
        sb.setRange(minimum, maximum)
        sb.setDecimals(decimals)
        sb.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.UpDownArrows)
        sb.lineEdit().returnPressed.connect(self._apply_setpoint)
        return sb

    def _make_setpoint_row(self, text, spin, est_label=None):
        """Build one Control Command row: [label] [spinbox] [estimate?] [stretch].
        Shared by velocity / torque / position so all rows are identical."""
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel(text))
        lay.addWidget(spin)
        if est_label is not None:
            lay.addWidget(est_label)
        lay.addStretch()
        return row

    @Slot()
    def _apply_setpoint(self):
        """Write the currently-active setpoint to the device.

        The setpoint spinboxes do not write on change, so adjusting them never
        moves the motor (monitor-only principle). The value is sent only when
        the user confirms via the Apply button or by pressing Enter.
        """
        if self.controller is None:
            return
        mode = self._current_control_mode()
        try:
            if mode == CONTROL_MODE_VELOCITY_CONTROL:
                self.controller.input_vel = self.vel_spinbox.value()
                label = "Velocity"
            elif mode == CONTROL_MODE_TORQUE_CONTROL:
                self.controller.input_torque = self.torque_spinbox.value()
                label = "Torque"
            elif mode == CONTROL_MODE_POSITION_CONTROL:
                self.controller.input_pos = self.pos_spinbox.value()
                label = "Position"
            else:
                return
            self.statusBar().showMessage(f"{label} setpoint applied", 2000)
        except Exception as e:
            self.statusBar().showMessage(f"Failed to apply setpoint: {e}", 3000)

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
            self._set_conn("● Rebooting…", "orange")
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
    def _on_show_error_history(self):
        """Open the decoded errors / history dialog (Device > Errors… or a
        click on the footer error indicator)."""
        if self.odrive is None:
            QMessageBox.information(self, "Errors", "Not connected")
            return
        if self._last_report is None:
            self._last_report = read_error_report(self.odrive, self.axis)
        dlg = ErrorDialog(self._last_report, self.error_history,
                          clear_fn=self._clear_errors, parent=self)
        dlg.exec()

    def _clear_errors(self):
        """Clear errors on the device (used by the error dialog)."""
        if self.odrive is None:
            return
        try:
            self.odrive.clear_errors()
            self.statusBar().showMessage("Cleared all errors", 3000)
        except Exception as e:
            self.statusBar().showMessage(f"Failed to clear errors: {e}", 3000)

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

    def _read_value(self, name, fn, setter=None):
        """Read a device value and apply it.

        Centralises the try/except read pattern (Plan.md §4.1): `fn()` does
        the device read, `setter(value)` updates the UI. Returns
        `(value, fatal)` where `fatal` is True only when the read failed with
        a disconnect (`ObjectLostError`) — the caller ORs it into the
        reconnect counter. Non-fatal failures return `(None, False)` and are
        logged once by `_read_failed`.
        """
        try:
            value = fn()
        except Exception as e:
            return None, self._read_failed(name, e)
        if setter is not None:
            setter(value)
        return value, False

    def update_readings(self):
        """Update displayed values from the ODrive. If reads fail repeatedly
        (and no _on_lost notification arrived), trigger a reconnect."""
        if self.axis is None or self.odrive is None:
            return

        self.sync_ui_from_controller()

        # Keep the Control Command gating + footer state in sync each poll.
        self._update_control_enabled()
        st = maybe_read(lambda: self.axis.current_state)
        if st is not None:
            self.state_status_label.setText(
                "State: " + AXIS_STATE_NAMES.get(st, str(st)))

        any_failed = False

        vbus, failed = self._read_value(
            "vbus_voltage",
            lambda: self.odrive.vbus_voltage,
            lambda v: self.vbus_status_label.setText(f"{v:.1f} V"))
        any_failed |= failed

        ibus, failed = self._read_value("ibus", lambda: self.odrive.ibus)
        any_failed |= failed

        if vbus is not None and ibus is not None:
            self.power_status_label.setText(f"{vbus * ibus:.1f} W")
        else:
            self.power_status_label.setText("-- W")

        _, failed = self._read_value(
            "vel_estimate",
            lambda: self.encoder.vel_estimate,
            lambda v: self.vel_estimate_label.setText(f"est: {v:.3f} rps"))
        any_failed |= failed

        pos, failed = self._read_value("pos_estimate",
                                       lambda: self.encoder.pos_estimate)
        any_failed |= failed
        if pos is not None:
            rng = self._position_circular_range()
            if rng is not None:
                pos = pos % rng  # wrap into [0, range) to match circular mode
            self.pos_estimate_label.setText(f"est: {pos:.4f} rev")

        # Phase 2: decode errors; keep footer + bounded history (guarded
        # optional reads — do not feed the disconnect counter).
        report = read_error_report(self.odrive, self.axis)
        self._last_report = report
        key = tuple(sorted((s.name, tuple(s.errors)) for s in report.sources))
        if key and key != self._last_error_key:
            self.error_history.append(report)
            self._last_error_key = key
        elif not key:
            self._last_error_key = None
        if report.any:
            union = 0
            for s in report.sources:
                union |= s.value
            self.error_status_label.setText(f"Err: 0x{union:X}")
            self.error_status_label.setStyleSheet("color: red; font-weight: bold;")
        else:
            self.error_status_label.setText("Err: OK")
            self.error_status_label.setStyleSheet("color: green; font-weight: bold;")
        # Fallback disconnect detection (primary is _on_lost)
        if any_failed:
            self._read_fail_count += 1
            if self._read_fail_count == RECONNECT_FAIL_THRESHOLD:
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

    # Ctrl+C should terminate the UI reliably from any state (including while
    # "finding device"). The Qt event loop is a blocking C++ call, so a Python
    # KeyboardInterrupt is *not* serviced during exec() — a timer "nudge" is
    # unreliable. Restoring the default SIGINT action kills the process at the
    # OS level, always. This is safe because the UI is monitor/settings only
    # and never drives realtime control (Plan.md §4.6): the motor keeps
    # running independently.
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
