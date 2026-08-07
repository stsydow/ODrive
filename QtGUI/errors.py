"""
Error decode + current-error display for the ODrive Qt GUI - Phase 2 (Plan.md
§2.1-2.3). Provides structured error decoding and the current-errors dialog
(opened via Device > Errors... or the status footer error indicator). The
chronological event log lives in `eventlog.py` (Debug > Event Log…).
"""

import logging
import time
from dataclasses import dataclass, field

import odrive.enums
from PySide6.QtCore import Slot
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from util import safe_getattr

logger = logging.getLogger(__name__)

# (display name, object path, enum prefix) read per poll
_ERROR_SOURCES = (
    ("system", "error", "ODRIVE_ERROR_"),
    ("axis", "error", "AXIS_ERROR_"),
    ("motor", "motor.error", "MOTOR_ERROR_"),
    ("encoder", "encoder.error", "ENCODER_ERROR_"),
    ("controller", "controller.error", "CONTROLLER_ERROR_"),
    ("sensorless", "sensorless_estimator.error", "SENSORLESS_ESTIMATOR_ERROR_"),
)


def _decode(value, prefix):
    """Return the names of all error bits set in `value` for `*_ERROR_*` enums."""
    out = []
    for name, const in vars(odrive.enums).items():
        if not name.startswith(prefix) or not isinstance(const, int):
            continue
        if value & const:
            out.append(name)
    return out


@dataclass
class ErrorModule:
    name: str
    value: int
    errors: list = field(default_factory=list)


@dataclass
class ErrorReport:
    timestamp: float
    sources: list = field(default_factory=list)  # [ErrorModule, ...]

    @property
    def any(self):
        return any(s.value for s in self.sources)


def read_error_report(odrv, axis):
    """Read all error values into a structured ErrorReport.

    Reads are guarded: a missing sub-object is skipped (optional-only reads —
    they feed display, not the disconnect counter)."""
    report = ErrorReport(timestamp=time.time())
    sources = []
    for name, path, prefix in _ERROR_SOURCES:
        obj = odrv if name == "system" else axis
        value = safe_getattr(obj, *path.split("."))
        if value is None:
            continue  # module not present on this firmware
        if value:
            sources.append(ErrorModule(name, value, _decode(value, prefix)))
    report.sources = sources
    return report


def format_current(report):
    """Render the current decoded errors of a report to text."""
    if not report.sources:
        return "No errors."
    lines = []
    for s in report.sources:
        detail = " | ".join(s.errors) if s.errors else f"0x{s.value:X}"
        lines.append(f"{s.name}: {detail}")
    return "\n".join(lines)


class ErrorDialog(QDialog):
    """Current decoded errors (Device > Errors… or the footer Err indicator)."""

    def __init__(self, report, clear_fn=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Errors")
        self.resize(480, 360)
        self._clear_fn = clear_fn

        outer = QVBoxLayout(self)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setPlainText(
            format_current(report) if report is not None
            else "(no current-error snapshot — device not connected)")
        outer.addWidget(self.text)

        row = QHBoxLayout()
        self.clear_btn = QPushButton("Clear Errors")
        self.clear_btn.setEnabled(clear_fn is not None)
        self.clear_btn.clicked.connect(self._clear)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        row.addWidget(self.clear_btn)
        row.addStretch()
        row.addWidget(close_btn)
        outer.addLayout(row)

    @Slot()
    def _clear(self):
        if self._clear_fn:
            self._clear_fn()
            self.text.setPlainText("No errors.")
