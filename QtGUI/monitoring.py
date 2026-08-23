"""Live plot (Plan §3.1): fixed-retention sample buffer + pyqtgraph window.

Sampling happens in GuiBackend.updateReadings (100 ms poll rate) into a
SampleBuffer; the plot window only redraws slices of that buffer on its own
QTimer, so closing/opening the window never affects capture.
"""

from __future__ import annotations

import collections
import math
from collections.abc import Iterator

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

# Channel key -> (label, unit). Order defines curve order and CSV columns.
CHANNELS = [
    ("vel", "Velocity", "turn/s"),
    ("pos", "Position", "rev"),
    ("iq", "Current Iq", "A"),
    ("i_a", "Current phA", "A"),
    ("i_b", "Current phB", "A"),
    ("torque", "Torque", "N·m"),
    ("setpoint", "Setpoint", ""),
    ("input", "Input", ""),
]

SAMPLE_INTERVAL_S = 0.1   # == device poll rate
BUFFER_SECONDS = 60.0     # retention == widest selectable plot window

_WINDOW_CHOICES = [("5 s", 5.0), ("30 s", 30.0), ("60 s", 60.0)]


class SampleBuffer:
    """Ring of (wall-time, {channel: value}) rows; missing channels are NaN.

    Retention equals the widest plot window; CSV export (§3.2) dumps exactly
    these rows (header + one row per sample), so it slots straight in.
    """

    def __init__(self, seconds: float = BUFFER_SECONDS) -> None:
        self.rows: collections.deque[tuple[float, dict[str, float]]] = \
            collections.deque(maxlen=int(seconds / SAMPLE_INTERVAL_S))

    def append(self, t: float, values: dict[str, float]) -> None:
        self.rows.append((t, values))

    def window(self, seconds: float) -> tuple[list[float], dict[str, list[float]]]:
        """Trailing `seconds` as (x times relative to window start, per-channel y)."""
        if not self.rows:
            return [], {key: [] for key, _, _ in CHANNELS}
        base = self.rows[-1][0] - seconds
        sel = [row for row in self.rows if row[0] >= base]
        return (
            [t - base for t, _ in sel],
            {key: [v.get(key, math.nan) for _, v in sel] for key, _, _ in CHANNELS},
        )

    def csv(self) -> Iterator[str]:
        """Whole retained buffer as CSV lines (header included)."""
        yield "time," + ",".join(label for _, label, _ in CHANNELS)
        for t, vals in self.rows:
            fields = ",".join(f"{vals.get(key, math.nan):.5g}"
                              for key, _, _ in CHANNELS)
            yield f"{t:.3f},{fields}"


class PlotWindow(QWidget):
    """Native pyqtgraph plot over the shared SampleBuffer (menu-opened)."""

    def __init__(self, samples: SampleBuffer) -> None:
        super().__init__()
        # ponytail: full redraw of ≤600 points per curve every 100 ms instead
        # of incremental append — fast enough by orders of magnitude; switch
        # only if profiling ever says otherwise.
        self.setWindowTitle("ODrive Live Plot")
        self.resize(700, 400)
        self.samples = samples
        self.window_seconds = 30.0
        self.paused = False

        bar = QHBoxLayout()
        combo = QComboBox()
        combo.addItems([label for label, _ in _WINDOW_CHOICES])
        combo.setCurrentIndex(1)
        combo.currentIndexChanged.connect(
            lambda idx: setattr(self, "window_seconds", _WINDOW_CHOICES[idx][1]))
        self._pause_btn = QPushButton("Pause")
        self._pause_btn.setCheckable(True)
        self._pause_btn.toggled.connect(self._on_paused)
        bar.addWidget(combo)
        bar.addWidget(self._pause_btn)
        bar.addStretch()

        import pyqtgraph as pg  # deferred: heavy import only on first open

        plot = pg.PlotWidget()
        plot.showGrid(x=True, y=True, alpha=0.3)
        plot.addLegend(offset=(10, 10))
        self._curves = []
        for i, (_, label, unit) in enumerate(CHANNELS):
            name = f"{label} [{unit}]" if unit else label
            self._curves.append(plot.plot(
                pen=pg.intColor(i, len(CHANNELS)), name=name))

        layout = QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addWidget(plot)

        timer = QTimer(self)
        timer.setInterval(int(SAMPLE_INTERVAL_S * 1000))
        timer.timeout.connect(self.refresh)
        timer.start()

    def refresh(self) -> None:
        if self.paused:
            return
        times, series = self.samples.window(self.window_seconds)
        for curve, (key, _, _) in zip(self._curves, CHANNELS, strict=False):
            curve.setData(times, series[key], connect="finite")

    def _on_paused(self, checked: bool) -> None:
        self.paused = checked
        self._pause_btn.setText("Resume" if checked else "Pause")
