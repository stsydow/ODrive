"""
Error decode for the ODrive QML GUI. Provides structured error decoding and
text formatting. The QML error dialog (qml/ErrorDialog.qml) reads the decoded
text live from the backend (Device > Errors... or the status footer error
indicator). The chronological event log lives in `eventlog.py`.
"""

import logging
import time
from dataclasses import dataclass, field

import odrive.enums

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
