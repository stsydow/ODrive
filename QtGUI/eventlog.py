"""
In-memory UI/device event log + viewer for the ODrive Qt GUI.

Per the project pattern, the UI sends *commands* and the device *generates
events* about what happened; ``log_event`` records that device-side history
(connect / state / mode / setpoint / error / clear ...). The viewer is opened
via Debug > Event Log… and works even while the device is disconnected, so the
run-up to a disconnect stays visible.
"""

import logging
import time
from dataclasses import dataclass

from PySide6.QtCore import Slot
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

logger = logging.getLogger(__name__)


@dataclass
class LogEntry:
    """One time-stamped event in the in-memory log."""

    timestamp: float
    category: str   # CONNECT / STATE / MODE / SETPOINT / CFG / ERROR / CLEAR
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


class LogDialog(QDialog):
    """Chronological UI/device event log (Debug > Event Log…).

    Non-modal and live-updating: it stays open beside the main window and
    refreshes itself by observing newly appended entries (via the `updated`
    Qt signal — no polling)."""

    def __init__(self, entries, updated=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Event Log")
        self.resize(640, 480)
        self._entries = entries          # live reference to the shared deque

        outer = QVBoxLayout(self)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        outer.addWidget(self.text)

        row = QHBoxLayout()
        export_btn = QPushButton("Export Log…")
        export_btn.clicked.connect(self._export)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        row.addWidget(export_btn)
        row.addStretch()
        row.addWidget(close_btn)
        outer.addLayout(row)

        self._refresh()
        if updated is not None:
            updated.connect(self._refresh)

    @Slot()
    def _refresh(self):
        text = format_log(self._entries)
        if text == self.text.toPlainText():
            return
        self.text.setPlainText(text)
        # Keep the live view pinned to the newest entry.
        sb = self.text.verticalScrollBar()
        sb.setValue(sb.maximum())

    @Slot()
    def _export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Log", "odrive_log.txt",
            "Text Files (*.txt);;All Files (*)")
        if not path:
            return
        try:
            with open(path, "w") as f:
                f.write(self.text.toPlainText())
        except OSError as e:
            logger.warning("export failed: %s", e)
