"""Live plot (Plan §3.1): fixed-retention sample buffer + pyqtgraph window.

Sampling happens in GuiBackend.updateReadings (100 ms poll rate) into a
SampleBuffer; the plot window only redraws slices of that buffer on its own
QTimer, so closing/opening the window never affects capture.
"""

from __future__ import annotations

import collections
import math
from collections.abc import Iterator

from odrive.enums import CONTROL_MODE_POSITION_CONTROL as _MODE_POS
from odrive.enums import CONTROL_MODE_TORQUE_CONTROL as _MODE_TQ
from odrive.enums import CONTROL_MODE_VELOCITY_CONTROL as _MODE_VEL
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

# Quantity rows: (group id, title, unit) — subplot order top to bottom.
_GROUPS = [
    ("pos", "Position", "rev"),
    ("vel", "Velocity", "turn/s"),
    ("torque", "Torque", "N·m"),
    ("current", "Current", "A"),
    ("power", "Power", "W"),
    ("voltage", "Bus V", "V"),
]

# Channel key -> (label, shown by default, group, required control mode or None).
# Setpoint/Input exist per quantity and are only shown while that control mode
# is active (their unit follows the mode).
CHANNELS = [
    ("pos", "Position", True, "pos", None),
    ("pos_sp", "Setpoint", True, "pos", _MODE_POS),
    ("pos_in", "Input", True, "pos", _MODE_POS),
    ("vel", "Velocity", True, "vel", None),
    ("vel_sp", "Setpoint", True, "vel", _MODE_VEL),
    ("vel_in", "Input", True, "vel", _MODE_VEL),
    ("torque", "Torque", True, "torque", None),
    ("tq_sp", "Setpoint", True, "torque", _MODE_TQ),
    ("tq_in", "Input", True, "torque", _MODE_TQ),
    ("iq", "Current Iq", True, "current", None),
    ("i_a", "Current phA", False, "current", None),
    ("i_b", "Current phB", False, "current", None),
    ("p_mech", "Mech. power", False, "power", None),
    ("p_elec", "Elec. power", False, "power", None),
    ("vbus", "Bus voltage", True, "voltage", None),
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
    """Native pyqtgraph plot over the shared SampleBuffer (menu-opened).

    One subplot per quantity row; setpoint/input curves are gated on the
    backend's current control mode (their unit follows the mode).
    """

    def __init__(self, backend) -> None:
        super().__init__()
        # ponytail: full redraw of ≤600 points per curve every 100 ms instead
        # of incremental append — fast enough by orders of magnitude; switch
        # only if profiling ever says otherwise.
        self._backend = backend
        self.setWindowTitle("ODrive Live Plot")
        self.resize(700, 700)
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

        # One subplot per quantity row (GraphicsLayout reflows hidden rows).
        glw = pg.GraphicsLayoutWidget()
        self._plots: dict[str, pg.PlotItem] = {}
        first: pg.PlotItem | None = None
        for gid, title, unit in _GROUPS:
            plot = glw.addPlot(row=len(self._plots), col=0)
            plot.showGrid(x=True, y=True, alpha=0.3)
            plot.setLabel("left", title)
            if unit:
                plot.setLabel("left", units=unit)
            # Fixed x: whole selected interval on every row, no wiggle;
            # y keeps autorange; x-pan/zoom disabled.
            plot.setMouseEnabled(x=False)
            plot.addLegend(offset=(5, 5))
            if first is None:
                first = plot
            else:
                plot.setXLink(first)
            self._plots[gid] = plot
        assert first is not None  # _GROUPS is non-empty
        self._plot = first

        # One checkbox per channel; unchecked hides the curve (sampling and
        # buffering are unaffected — CSV export stays complete). Setpoint/
        # Input curves additionally require their control mode to be active.
        # A row with no active curve is hidden entirely (_update_rows).
        channel_box = QHBoxLayout()
        group_curves: dict[str, list[tuple[str, pg.PlotDataItem]]] = {
            gid: [] for gid, *_ in _GROUPS}
        self._boxes: dict[str, QCheckBox] = {}
        self._curve_mode: dict[str, int | None] = {}
        for i, (key, label, default_on, group, mode) in enumerate(CHANNELS):
            curve = self._plots[group].plot(pen=pg.intColor(i, len(CHANNELS)),
                                            name=label)
            group_curves[group].append((key, curve))
            box = QCheckBox(label)
            box.setChecked(default_on)
            box.toggled.connect(lambda _checked: self._update_rows())
            channel_box.addWidget(box)
            self._boxes[key] = box
            self._curve_mode[key] = mode
        self._group_curves = group_curves

        layout = QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addWidget(glw)
        layout.addLayout(channel_box)
        backend.modeChanged.connect(self._update_rows)
        self._update_rows()

        timer = QTimer(self)
        timer.setInterval(int(SAMPLE_INTERVAL_S * 1000))
        timer.timeout.connect(self.refresh)
        timer.start()

    def refresh(self) -> None:
        if self.paused:
            return
        times, series = self._backend.samples.window(self.window_seconds)
        for gid, curves in self._group_curves.items():
            if not self._plots[gid].isVisible():
                continue
            for key, curve in curves:
                if curve.isVisible():
                    curve.setData(times, series[key], connect="finite")
        self._plot.setXRange(0.0, self.window_seconds, padding=0)

    def _update_rows(self) -> None:
        """Apply checkbox + control-mode gating per curve; hide rows without
        active curves; time ticks only on bottom-most visible row."""
        mode = self._backend.plotMode()
        last_visible = -1
        for gi, (gid, *_) in enumerate(_GROUPS):
            any_on = False
            for key, curve in self._group_curves[gid]:
                ch_mode = self._curve_mode[key]
                vis = self._boxes[key].isChecked() and ch_mode in (None, mode)
                if curve.isVisible() != vis:
                    curve.setVisible(vis)
                any_on |= vis
            self._plots[gid].setVisible(any_on)
            if any_on:
                last_visible = gi
        for gi, (gid, *_) in enumerate(_GROUPS):
            axis = self._plots[gid].getAxis("bottom")
            axis.setStyle(showValues=(gi == last_visible))
        # Hide checkboxes whose channel is bound to a non-active control mode
        # (they'd be dead weight — six setpoint/input boxes otherwise).
        for key, ch_mode in self._curve_mode.items():
            if ch_mode is not None:
                self._boxes[key].setVisible(ch_mode == mode)
        self.refresh()

    def _on_window_changed(self, idx: int) -> None:
        self.window_seconds = _WINDOW_CHOICES[idx][1]
        self.refresh()

    def _on_paused(self, checked: bool) -> None:
        self.paused = checked
        self._pause_btn.setText("Resume" if checked else "Pause")
