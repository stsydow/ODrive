"""SampleBuffer + plot sampling logic (Plan §3.1) — pure logic, no hardware."""

import math
import time

from conftest import MockDevice

from backend import _PLOT_READERS
from monitoring import BUFFER_SECONDS, CHANNELS, SAMPLE_INTERVAL_S, SampleBuffer


def test_window_slices_trailing_seconds():
    buf = SampleBuffer(seconds=10.0)
    t0 = 1000.0
    for i in range(101):  # 0..10 s at 100 ms
        buf.append(t0 + i * 0.1, {"vel": float(i)})
    times, series = buf.window(5.0)
    # newest sample (i=100) is window end; keep i>=50 -> ~51 points
    assert len(times) == len(series["vel"]) == 51
    assert math.isclose(times[-1], 5.0, abs_tol=1e-9)
    assert times[0] <= 0.1
    assert series["vel"][-1] == 100.0


def test_missing_channels_are_nan():
    buf = SampleBuffer()
    buf.append(time.time(), {"vel": 1.0})  # iq/torque/setpoint not provided
    _, series = buf.window(30.0)
    for key, *_ in CHANNELS:
        assert key in series
    assert series["vel"] == [1.0]
    assert math.isnan(series["torque"][0])


def test_ring_retention():
    buf = SampleBuffer()  # default 60 s / 0.1 s
    now = time.time()
    for i in range(700):  # more than maxlen (600)
        buf.append(now + i * SAMPLE_INTERVAL_S, {"vel": float(i)})
    assert len(buf.rows) == int(BUFFER_SECONDS / SAMPLE_INTERVAL_S)


def test_csv_export_header_and_rows():
    buf = SampleBuffer()
    buf.append(123.456, {"vel": 1.5})
    lines = list(buf.csv())
    assert lines[0] == "time," + ",".join(label for _, label, *_ in CHANNELS)
    assert lines[1].startswith("123.456,")
    assert ",1.5," in lines[1]  # vel value present in its column


def test_readers_against_mock_device():
    dev = MockDevice()
    axis = dev.axis0
    vals = {key: read(axis, dev) for key, read in _PLOT_READERS.items()}
    assert vals["vel"] == 1.5          # MockEncoder.vel_estimate
    assert vals["pos"] == 2.5          # no pos_circular on mock -> pos_estimate
    assert vals["vel_in"] == 1.0       # controller.input_vel
    assert vals["pos_in"] == 0.0       # MockController.input_pos default
    assert vals["tq_in"] == 0.0        # MockController.input_torque default
    assert vals["vbus"] == 24.0        # MockDevice.vbus_voltage
    # mock fw exposes none of these endpoints -> NaN (feature-gated curves)
    for key in ("iq", "i_a", "i_b", "torque", "p_mech", "p_elec",
                "pos_sp", "vel_sp", "tq_sp"):
        assert math.isnan(vals[key]), key
