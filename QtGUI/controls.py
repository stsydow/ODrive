"""
Control Settings panel for the ODrive Qt GUI.

Layout (one level up for control params, which are config-editor values):

    Control Settings (main.py)
      ├─ Input Mode selector
      ├─ Control Parameters            (gains / integrators / feed-forward)
      └─ Limits (QTabWidget)
            ├─ Electrical Limits
            └─ Mechanical Limits

Values are read from the device on connect and written back on change.
Feature-gating uses `hasattr` checks: any parameter the
attached firmware does not expose is disabled. Portable by design:
values are read from the connected device, never assumed.
"""

import logging
from typing import ClassVar

from odrive.enums import (
    CONTROL_MODE_POSITION_CONTROL,
    CONTROL_MODE_TORQUE_CONTROL,
    CONTROL_MODE_VELOCITY_CONTROL,
    INPUT_MODE_PASSTHROUGH,
    INPUT_MODE_POS_FILTER,
    INPUT_MODE_TORQUE_RAMP,
    INPUT_MODE_TRAP_TRAJ,
    INPUT_MODE_VEL_RAMP,
)
from PySide6.QtCore import Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QLabel,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from util import DEVICE_EXCEPTIONS, safe_getattr

logger = logging.getLogger(__name__)

# Input modes exposed in the selector. Value -> display label.
INPUT_MODES = {
    INPUT_MODE_PASSTHROUGH: "Passthrough",
    INPUT_MODE_VEL_RAMP: "Velocity Ramp",
    INPUT_MODE_POS_FILTER: "Position Filter",
    INPUT_MODE_TRAP_TRAJ: "Trapezoidal Trajectory",
    INPUT_MODE_TORQUE_RAMP: "Torque Ramp",
}

BASE_CONTROLLER = "controller"
BASE_MOTOR = "motor"
BASE_ODRIVE = "odrive"


class InputModeSelector(QComboBox):
    """Selector for the input mode, restricted to the current control mode.

    `input_mode` selects how the user setpoint is turned into the controller
    setpoint. Only modes that apply to the active control mode are shown:
      velocity -> PASSTHROUGH, VEL_RAMP
      position -> PASSTHROUGH, POS_FILTER, TRAP_TRAJ
      torque   -> PASSTHROUGH, TORQUE_RAMP
    """

    # Valid input modes per control mode. Passthrough is an option but is
    # listed LAST (never the default) when a real shaping mode exists.
    MODES_BY_CONTROL: ClassVar[dict] = {
        CONTROL_MODE_VELOCITY_CONTROL: [INPUT_MODE_VEL_RAMP, INPUT_MODE_PASSTHROUGH],
        CONTROL_MODE_POSITION_CONTROL: [INPUT_MODE_POS_FILTER, INPUT_MODE_TRAP_TRAJ, INPUT_MODE_PASSTHROUGH],
        CONTROL_MODE_TORQUE_CONTROL: [INPUT_MODE_TORQUE_RAMP, INPUT_MODE_PASSTHROUGH],
    }
    # Fallback when the device input_mode is inapplicable to the control mode:
    # use the recommended mode, never Passthrough.
    DEFAULT_BY_CONTROL: ClassVar[dict] = {
        CONTROL_MODE_VELOCITY_CONTROL: INPUT_MODE_VEL_RAMP,
        CONTROL_MODE_POSITION_CONTROL: INPUT_MODE_TRAP_TRAJ,
        CONTROL_MODE_TORQUE_CONTROL: INPUT_MODE_TORQUE_RAMP,
    }

    def __init__(self, status=None, parent=None):
        super().__init__(parent)
        self._status = status
        self._controller = None
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.setToolTip(
            "How the axis setpoint is derived from the input, for the current "
            "control mode. VEL_RAMP adds an acceleration limit via "
            "'vel_ramp_rate'. TORQUE_RAMP only applies in TORQUE_CONTROL mode."
        )
        self.currentTextChanged.connect(self._on_changed)
        self.setEnabled(False)

    @property
    def input_mode(self):
        return self.currentData()

    def bind(self, controller):
        """Attach a controller, enable the box and populate for its mode."""
        self._controller = controller
        if controller is None or not hasattr(controller.config, "input_mode"):
            self.clear()
            self.setEnabled(False)
            return
        self.setEnabled(True)
        self._apply_for_mode(self._read_mode())

    def set_control_mode(self, control_mode):
        """Repopulate for a (possibly new) control mode."""
        if self._controller is None:
            return
        self._apply_for_mode(control_mode)

    def _read_mode(self):
        return safe_getattr(self._controller, "config", "control_mode")

    def _apply_for_mode(self, control_mode):
        allowed = self.MODES_BY_CONTROL.get(control_mode, [INPUT_MODE_PASSTHROUGH])
        cur = safe_getattr(self._controller, "config", "input_mode")
        if cur is not None and cur in allowed and cur != INPUT_MODE_PASSTHROUGH:
            select = cur
        else:
            # Passthrough (or an inapplicable mode) is never auto-selected:
            # fall back to the recommended mode instead.
            select = self.DEFAULT_BY_CONTROL.get(control_mode, allowed[0])
        self._populate(select, allowed)
        # Device had an input mode inapplicable to this control mode: correct it.
        if cur is not None and select != cur:
            try:
                self._controller.config.input_mode = select
            except DEVICE_EXCEPTIONS as e:
                logger.warning("failed to set input_mode: %s", e)

    def _populate(self, select_value, allowed):
        self.blockSignals(True)
        self.clear()
        idx = 0
        for i, value in enumerate(allowed):
            self.addItem(INPUT_MODES[value], value)
            if value == select_value:
                idx = i
        self.setCurrentIndex(idx)
        # Size the box to its longest item so text isn't clipped.
        fm = self.fontMetrics()
        max_w = max(fm.horizontalAdvance(self.itemText(i))
                    for i in range(self.count())) + 40
        self.setMinimumWidth(max_w)
        self.blockSignals(False)

    @Slot()
    def _on_changed(self):
        if self._controller is None:
            return
        value = self.currentData()
        if value is None:
            return
        try:
            self._controller.config.input_mode = value
            if self._status:
                self._status(f"Input mode set to {self.currentText()}")
        except DEVICE_EXCEPTIONS as e:
            logger.warning("Failed to set input_mode: %s", e)
            if self._status:
                self._status(f"Failed to set input mode: {e}")


class _RowConfigPanel(QGroupBox):
    """Shared logic for the two config panels: build rows of spinboxes and
    checkboxes into provided grid layouts, then read-on-bind / write-on-change
    with `hasattr` feature gating."""

    _SPINS: ClassVar[list] = []   # subclass: (attr, base, label, unit, min, max, decimals, step)
    _CHECKS: ClassVar[list] = []  # subclass: (attr, base, label)

    def __init__(self, title, status=None, parent=None):
        super().__init__(title, parent)
        self._status = status
        self._controller = None
        self._motor = None
        self._odrive = None
        self._syncing = False
        self._spin_boxes = {}
        self._check_boxes = {}
        self._bases = {}

    # -- row building --------------------------------------------------

    def _new_spin(self, spec):
        """Build a spinbox. `spec` may carry trailing group fields; only the
        range/decimals live at fixed indices."""
        _min, _max, dec, step = (float(spec[4]), float(spec[5]), spec[6], spec[7])
        sb = QDoubleSpinBox()
        sb.setRange(_min, _max)
        sb.setDecimals(dec)
        sb.setSingleStep(step)
        sb.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.UpDownArrows)
        sb.valueChanged.connect(self._on_spin_changed)
        return sb

    def _place_spin(self, layout, row, col, spec):
        attr, base, label, unit, *_ = spec
        text = f"{label} ({unit})" if unit else label
        lbl = QLabel(text)
        tip = f"{base}.config.{attr}"
        if attr == "requested_current_range":
            tip += (
                "\nMax 60 A on this controller. Should be > "
                "current_lim + current_lim_margin, but as low as "
                "possible for best resolution."
            )
        lbl.setToolTip(tip)
        sb = self._new_spin(spec)
        layout.addWidget(lbl, row, col)
        layout.addWidget(sb, row, col + 1)
        self._spin_boxes[attr] = sb
        self._bases[attr] = base

    def _place_check(self, layout, row, col, check):
        attr, base, label, *_ = check
        cb = QCheckBox(label)
        cb.setToolTip(f"{base}.config.{attr}")
        cb.toggled.connect(self._on_check_toggled)
        layout.addWidget(cb, row, col, 1, 2)
        self._check_boxes[attr] = cb
        self._bases[attr] = base

    def _write_scalar_rows(self, layout, start_row=0):
        """Fill `layout` with spinbox/checkbox rows in a two-column grid.

        Only parameters with the *same* group tag share a line; an unmatched
        item leaves its second slot empty rather than being paired arbitrarily.

            | Current limit (A)[   ]  Current limit margin (A)[   ] |   (group "cur")
            | Requested range (A)[  ]                                |   (group "rng")
        """
        row = start_row
        spins = list(self._SPINS)
        i = 0
        while i < len(spins):
            group = spins[i][-1]
            self._place_spin(layout, row, 0, spins[i])
            i += 1
            if i < len(spins) and spins[i][-1] == group:
                self._place_spin(layout, row, 2, spins[i])
                i += 1
            row += 1
        checks = list(self._CHECKS)
        j = 0
        while j < len(checks):
            group = checks[j][-1]
            self._place_check(layout, row, 0, checks[j])
            j += 1
            if j < len(checks) and checks[j][-1] == group:
                self._place_check(layout, row, 2, checks[j])
                j += 1
            row += 1
        return row

    # -- device sync / writes -----------------------------------------

    def bind(self, controller, motor, odrive):
        """Attach device refs and (re)load values + feature availability."""
        self._controller = controller
        self._motor = motor
        self._odrive = odrive
        if controller is None:
            self.setEnabled(False)
            return
        self.setEnabled(True)
        self._sync_from_device()

    def _obj(self, attr):
        base = self._bases[attr]
        if base == BASE_MOTOR:
            return self._motor
        if base == BASE_ODRIVE:
            return self._odrive
        return self._controller

    def _sync_from_device(self):
        self._syncing = True
        try:
            for attr, box in self._spin_boxes.items():
                obj = self._obj(attr)
                enabled = obj is not None and hasattr(obj.config, attr)
                box.blockSignals(True)
                box.setEnabled(enabled)
                if enabled:
                    val = safe_getattr(obj.config, attr)
                    if val is not None:
                        box.setValue(float(val))
                    else:
                        box.setEnabled(False)
                        logger.debug("read %s failed", attr)
                box.blockSignals(False)
            for attr, cb in self._check_boxes.items():
                obj = self._obj(attr)
                enabled = obj is not None and hasattr(obj.config, attr)
                cb.blockSignals(True)
                cb.setEnabled(enabled)
                if enabled:
                    val = safe_getattr(obj.config, attr)
                    if val is not None:
                        cb.setChecked(bool(val))
                cb.blockSignals(False)
        finally:
            self._syncing = False

    def _on_spin_changed(self, value):
        if self._syncing:
            return
        attr = next((a for a, b in self._spin_boxes.items()
                     if b.sender() == self.sender()), None)
        if attr is None:
            return
        obj = self._obj(attr)
        try:
            setattr(obj.config, attr, value)
        except DEVICE_EXCEPTIONS as e:
            logger.warning("Failed to set %s: %s", attr, e)
            if self._status:
                self._status(f"Failed to set {attr}: {e}")

    def _on_check_toggled(self, checked):
        if self._syncing:
            return
        cb = self.sender()
        attr = next((a for a, b in self._check_boxes.items() if b == cb), None)
        if attr is None:
            return
        obj = self._obj(attr)
        try:
            setattr(obj.config, attr, bool(checked))
        except DEVICE_EXCEPTIONS as e:
            logger.warning("Failed to set %s: %s", attr, e)
            if self._status:
                self._status(f"Failed to set {attr}: {e}")


class SettingsTabs(_RowConfigPanel):
    """Consolidated settings as three tabs:

      Electrical Limits | Mechanical Limits | Control Parameters
    """

    TITLE = "Settings"

    _TABS = ("Electrical Limits", "Mechanical Limits", "Control Parameters")

    # (attr, base, label, unit, min, max, decimals, step, tab, group)
    _SPINS: ClassVar[list] = [
        # tab 0: Electrical Limits
        ("current_lim", BASE_MOTOR, "Current limit", "A", 0.0, 60.0, 2, 0.1, 0, "cur"),
        ("current_lim_margin", BASE_MOTOR, "Current limit margin", "A", 0.0, 60.0, 2, 0.1, 0, "cur"),
        ("requested_current_range", BASE_MOTOR, "Requested current range", "A", 0.0, 60.0, 1, 0.5, 0, "rng"),
        ("dc_max_positive_current", BASE_ODRIVE, "DC +ve current limit (PSU)", "A", 0.0, 60.0, 1, 0.5, 0, "dc"),
        ("dc_max_negative_current", BASE_ODRIVE, "DC -ve current limit (regen)", "A", -60.0, 0.0, 2, 0.1, 0, "dc"),
        ("dc_bus_overvoltage_trip_level", BASE_ODRIVE, "DC overvoltage trip", "V", 0.0, 60.0, 1, 0.5, 0, "ov"),
        # tab 1: Mechanical Limits
        ("vel_limit", BASE_CONTROLLER, "Velocity limit", "turn/s", 0.0, 200.0, 1, 0.5, 1, "vel"),
        ("torque_lim", BASE_MOTOR, "Torque limit", "N·m", 0.0, 50.0, 3, 0.1, 1, "tor"),
        # tab 2: Control Parameters
        ("vel_gain", BASE_CONTROLLER, "Velocity gain", "N·m/(turn/s)", 0.0, 10.0, 4, 0.001, 2, "vel"),
        ("vel_integrator_gain", BASE_CONTROLLER, "Vel. integrator gain", "N·m/turn", 0.0, 10.0, 4, 0.001, 2, "int"),
        ("vel_integrator_limit", BASE_CONTROLLER, "Vel. integrator limit", "N·m", 0.0, 50.0, 3, 0.1, 2, "int"),
        ("pos_gain", BASE_CONTROLLER, "Position gain", "(turn/s)/turn", 0.0, 100.0, 3, 0.1, 2, "pos"),
        ("inertia", BASE_CONTROLLER, "Inertia (feed-forward)", "N·m/(turn/s²)", -50.0, 50.0, 4, 0.001, 2, "inertia"),
    ]
    # (attr, base, label, tab, group)
    _CHECKS: ClassVar[list] = [
        ("enable_vel_limit", BASE_CONTROLLER, "Enable velocity limit", 1, "vl"),
        ("enable_torque_mode_vel_limit", BASE_CONTROLLER, "Torque-mode velocity limit", 1, "vl"),
        ("enable_overspeed_error", BASE_CONTROLLER, "Overspeed error", 1, "ov"),
        ("enable_gain_scheduling", BASE_CONTROLLER, "Gain scheduling", 2, "gs"),
    ]

    def __init__(self, status=None, parent=None):
        super().__init__(self.TITLE, status, parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._tabs = QTabWidget()
        outer.addWidget(self._tabs)

        pages = []
        for name in self._TABS:
            page = QWidget()
            lay = QGridLayout(page)
            lay.setVerticalSpacing(3)
            lay.setHorizontalSpacing(12)
            self._tabs.addTab(page, name)
            pages.append((page, lay))

        # De-annotate free-form _SPINS/_CHECKS in __init__: each row carries a
        # trailing tab index and a line-group tag, which we strip before
        # delegating to the base.
        self._spin_specs = {}
        self._check_specs = {}
        for spec in self._SPINS:
            *scalar, tab, group = spec
            self._spin_specs[tab] = [*self._spin_specs.get(tab, []), (*scalar, group)]
        for spec in self._CHECKS:
            attr, base, label, tab, group = spec
            self._check_specs[tab] = [*self._check_specs.get(tab, []), (attr, base, label, group)]

        # Temporarily swap in per-tab specs so the base writer fills each page.
        for tab_idx, (_page, lay) in enumerate(pages):
            self._SPINS = tuple(self._spin_specs.get(tab_idx, []))
            self._CHECKS = tuple(self._check_specs.get(tab_idx, []))
            self._write_scalar_rows(lay, start_row=0)

        self.setEnabled(False)
