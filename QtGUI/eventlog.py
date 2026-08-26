"""
In-memory UI/device event log for the ODrive QML GUI.

Per the project pattern, the UI sends *commands* and the device *generates
events* about what happened; ``logEvent`` records that device-side history
(connect / state / mode / setpoint / error / clear ...). The QML event-log
dialog (qml/EventLogDialog.qml) renders ``backend.logText`` live via the
`logUpdated` signal, and works even while the device is disconnected.
"""

import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class LogEntry:
    """One time-stamped event in the in-memory log."""

    timestamp: float
    category: str  # CONNECT / STATE / MODE / SETPOINT / CFG / ERROR / CLEAR
    message: str


def format_log(entries):
    """Render a bounded deque of LogEntries to text, oldest first."""
    if not entries:
        return "(no log entries yet)"
    lines = []
    for e in entries:
        stamp = time.strftime("%H:%M:%S", time.localtime(e.timestamp))
        lines.append(f"[{stamp}] {e.category:<9} {e.message}")
    return "\n".join(lines)
