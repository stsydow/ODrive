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
from PySide6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

# Channel key -> (label, unit, shown by default). Order defines curve order
# and CSV columns.
CHANNELS = [
    ("vel", "Velocity", "turn/s", True),
    ("pos", "Position", "rev", True),
    ("iq", "Current Iq", "A", True),
    ("i_a", "Current phA", "A", False),
    ("i_b", "Current phB", "A", False),
    ("torque", "Torque", "N·m", True),
    ("setpoint", "Setpoint", "", True),
    ("input", "Input", "", True),
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
            return [], {key: [] for key, *_ in CHANNELS}
        base = self.rows[-1][0] - seconds
        sel = [row for row in self.rows if row[0] >= base]
        return (
            [t - base for t, _ in sel],
            {key: [v.get(key, math.nan) for _, v in sel] for key, *_ in CHANNELS},
        )

    def csv(self) -> Iterator[str]:
        """Whole retained buffer as CSV lines (header included)."""
        yield "time," + ",".join(label for _, label, *_ in CHANNELS)
        for t, vals in self.rows:
            fields = ",".join(f"{vals.get(key, math.nan):.5g}"
                              for key, *_ in CHANNELS)
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
        combo.addItems([label for label, *_ in _WINDOW_CHOICES])
        combo.setCurrentIndex(1)
        combo.currentIndexChanged.connect(self._on_window_changed)
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
        # Fixed x: always show the whole selected interval (no wiggling as
        # the window slides); y keeps autorange. X-panning/zoom disabled.
        plot.setMouseEnabled(x=False)
        self._plot = plot

        # One checkbox per channel; unchecked just hides the curve (sampling
        # and buffering are unaffected — CSV export stays complete).
        channel_box = QHBoxLayout()
        self._curves: dict[str, pg.PlotDataItem] = {}
        for i, (key, label, unit, default_on) in enumerate(CHANNELS):
            name = f"{label} [{unit}]" if unit else label
            curve = plot.plot(pen=pg.intColor(i, len(CHANNELS)), name=name)
            # PlotDataItem has no reliable 'visible' ctor kwarg -> set it.
            curve.setVisible(default_on)
            self._curves[key] = curve
            box = QCheckBox(label)
            box.setChecked(default_on)
            box.toggled.connect(curve.setVisible)
            channel_box.addWidget(box)

        layout = QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addWidget(plot)
        layout.addLayout(channel_box)

        timer = QTimer(self)
        timer.setInterval(int(SAMPLE_INTERVAL_S * 1000))
        timer.timeout.connect(self.refresh)
        timer.start()

    def refresh(self) -> None:
        if self.paused:
            return
        times, series = self.samples.window(self.window_seconds)
        for key, curve in self._curves.items():
            if curve.isVisible():
                curve.setData(times, series[key], connect="finite")
        self._plot.setXRange(0.0, self.window_seconds, padding=0)

    def _on_window_changed(self, idx: int) -> None:
        self.window_seconds = _WINDOW_CHOICES[idx][1]
        self.refresh()

    def _on_paused(self, checked: bool) -> None:
        self.paused = checked
        self._pause_btn.setText("Resume" if checked else "Pause")
