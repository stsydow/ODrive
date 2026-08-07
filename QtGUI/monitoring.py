"""
Error display & history for the ODrive Qt GUI - Phase 2 (Plan.md §2.1-2.3).

Provides structured error decoding and an on-demand error dialog (opened via
the Device > Errors... menu or by clicking the error indicator in the status
footer). Keeps the main window compact — no persistent in-layout error panel.
"""

import logging
import time
from dataclasses import dataclass, field

import odrive.enums
from PySide6.QtCore import Slot
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
)

from util import safe_getattr

logger = logging.getLogger(__name__)

MAX_HISTORY = 1000

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
    they feed display/history, not the disconnect counter)."""
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


def format_history(history):
    """Render a bounded history deque of ErrorReports to text."""
    if not history:
        return "(no errors recorded)"
    lines = []
    for rpt in history:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rpt.timestamp))
        parts = [f"== {stamp} =="]
        for s in rpt.sources:
            detail = " | ".join(s.errors) if s.errors else f"0x{s.value:X}"
            parts.append(f"    {s.name}: {detail}")
        lines.append("\n".join(parts))
    return "\n\n".join(lines)


class ErrorDialog(QDialog):
    """On-demand error view: current decoded errors + history, with clear/export."""

    def __init__(self, report, history, clear_fn=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Errors")
        self.resize(560, 440)
        self._clear_fn = clear_fn

        outer = QVBoxLayout(self)
        self.tabs = QTabWidget()

        cur_tab = QPlainTextEdit()
        cur_tab.setReadOnly(True)
        cur_tab.setPlainText(format_current(report))
        self.tabs.addTab(cur_tab, "Current")

        self.hist_tab = QPlainTextEdit()
        self.hist_tab.setReadOnly(True)
        self.hist_tab.setPlainText(format_history(history))
        self.tabs.addTab(self.hist_tab, "History")
        outer.addWidget(self.tabs)

        row = QHBoxLayout()
        self.clear_btn = QPushButton("Clear Errors")
        self.clear_btn.clicked.connect(self._clear)
        export_btn = QPushButton("Export History…")
        export_btn.clicked.connect(self._export)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        row.addWidget(self.clear_btn)
        row.addWidget(export_btn)
        row.addStretch()
        row.addWidget(close_btn)
        outer.addLayout(row)

    @Slot()
    def _clear(self):
        if self._clear_fn:
            self._clear_fn()
            # Refresh the current tab to reflect the just-cleared state.
            self.tabs.widget(0).setPlainText("No errors.")

    @Slot()
    def _export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Error History", "error_history.txt",
            "Text Files (*.txt);;All Files (*)")
        if not path:
            return
        try:
            with open(path, "w") as f:
                f.write(self.hist_tab.toPlainText())
        except OSError as e:
            logger.warning("export failed: %s", e)
