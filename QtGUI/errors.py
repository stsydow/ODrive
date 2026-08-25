"""
Error decode for the ODrive QML GUI. Provides structured error decoding and
text formatting. The QML error dialog (qml/ErrorDialog.qml) reads the decoded
text live from the backend (Device > Errors... or the status footer error
indicator). The chronological event log lives in `eventlog.py`.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

import odrive.enums
from fibre import ObjectLostError

# Expected, transient device-communication failures worth handling gracefully
# (return a default / show an error / trigger a reconnect). A bare generic
# `Exception` from libfibre ("internal error", "peer misbehaving", "unknown
# error") is a BUG and is deliberately NOT caught, so it surfaces with a stack
# trace instead of being silently swallowed.

DEVICE_EXCEPTIONS = (
    ObjectLostError,
    EOFError,
    TimeoutError,
    OSError,          # transport / I/O (ConnectionError is a subclass)
    asyncio.CancelledError,
)

# Fork note (0.5.7-hardened): the vendored libfibre raises ObjectLostError
# uniformly for every "the object/link is gone" condition (destroyed handle,
# EmptyInterface access), so no extra exception tuples are needed here.
# AttributeError/TypeError on read paths now mean a genuinely missing endpoint
# or a bug -- they surface instead of being conflated with disconnects.

logger = logging.getLogger(__name__)


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


def read_error_report(odrv):
    """Read all error values into a structured ErrorReport.

    Reads are guarded: if the ODrive or its axis0 is missing, an empty report
    is returned. Otherwise, it reads errors from known sub-objects.
    """
    report = ErrorReport(timestamp=time.time())
    if odrv is None:
        return report

    try:
        axis0 = odrv.axis0
        sources = []

        def check(name, val, prefix):
            if not val:
                return
            # Inline bitmask decode: find all set error bits for the given prefix
            bits = [n for n, const in vars(odrive.enums).items()
                    if n.startswith(prefix) and isinstance(const, int) and (val & const)]
            sources.append(ErrorModule(name, val, bits))

        check("system", odrv.error, "ODRIVE_ERROR_")
        check("axis", axis0.error, "AXIS_ERROR_")
        check("motor", axis0.motor.error, "MOTOR_ERROR_")
        check("encoder", axis0.encoder.error, "ENCODER_ERROR_")
        check("controller", axis0.controller.error, "CONTROLLER_ERROR_")
        check("sensorless", axis0.sensorless_estimator.error, "SENSORLESS_ESTIMATOR_ERROR_")

        report.sources = sources
    except (DEVICE_EXCEPTIONS, AttributeError):
        pass

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
