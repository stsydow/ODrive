"""
Error decode + event log for the ODrive Qt GUI - Phase 2 (Plan.md §2.1-2.3).

Provides structured error decoding and an on-demand log viewer (opened via the
Device > Errors... menu or by clicking the error indicator in the status footer).
The viewer shows a time-stamped ring buffer of events (connect / state / mode /
setpoint / config / error / clear ...) so the entries around an error give
context of what happened before it. Keeps the main window compact — no persistent
in-layout error panel.
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

MAX_LOG = 1000

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


@dataclass
class LogEntry:
    """One time-stamped event in the in-memory log."""

    timestamp: float
    category: str   # CONNECT / STATE / MODE / SETPOINT / CFG / ERROR / CLEAR
    message: str


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


def format_log(entries):
    """Render a bounded deque of LogEntries to text, oldest first."""
    if not entries:
        return "(no log entries yet)"
    lines = []
    for e in entries:
        stamp = time.strftime("%H:%M:%S", time.localtime(e.timestamp))
        lines.append(f"[{stamp}] {e.category:<9} {e.message}")
    return "\n".join(lines)


class LogDialog(QDialog):
    """On-demand viewer: chronological event log (with error entries) plus the
    current decoded errors, with clear/export."""

    def __init__(self, report, entries, clear_fn=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Log / Errors")
        self.resize(640, 480)
        self._clear_fn = clear_fn

        outer = QVBoxLayout(self)
        self.tabs = QTabWidget()

        # Event log: context + error entries, oldest first.
        self.log_tab = QPlainTextEdit()
        self.log_tab.setReadOnly(True)
        self.log_tab.setPlainText(format_log(entries))
        self.tabs.addTab(self.log_tab, "Event Log")

        # Current decoded errors (live snapshot).
        self.cur_tab = QPlainTextEdit()
        self.cur_tab.setReadOnly(True)
        self.cur_tab.setPlainText(format_current(report))
        self.tabs.addTab(self.cur_tab, "Current Errors")
        outer.addWidget(self.tabs)

        row = QHBoxLayout()
        self.clear_btn = QPushButton("Clear Errors")
        self.clear_btn.clicked.connect(self._clear)
        export_btn = QPushButton("Export Log…")
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
            # Reflect the just-cleared state in the current-errors tab.
            self.cur_tab.setPlainText("No errors.")

    @Slot()
    def _export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Log", "odrive_log.txt",
            "Text Files (*.txt);;All Files (*)")
        if not path:
            return
        try:
            with open(path, "w") as f:
                f.write(self.log_tab.toPlainText())
        except OSError as e:
            logger.warning("export failed: %s", e)
