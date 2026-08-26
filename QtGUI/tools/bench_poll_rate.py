#!/usr/bin/env python3
"""
USB poll-rate / jitter benchmark against the control-loop tick counter.

Reads odrv.n_evt_control_loop N times; the counter delta gives the true loop
frequency and ticks-per-transfer, so wall-clock jitter can be judged against
loop time directly.

Usage: bench_poll_rate.py [n]
"""

import sys
import time

import numpy as np
import odrive


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 4000

    print("connecting...")
    odrv = odrive.find_any()

    # warmup
    _dummy = odrv.n_evt_control_loop

    samples = np.empty([2, n], dtype=np.int64)
    for i in range(n):
        tick = odrv.n_evt_control_loop
        samples[0, i] = time.perf_counter_ns()
        samples[1, i] = tick

    time_span = 1e-9 * (samples[0, -1] - samples[0, 0])
    tick_span = samples[1, -1] - samples[1, 0]

    if n < 2 or tick_span < 2 or time_span <= 1e-6:
        sys.exit(f"interval too small: {tick_span} ticks / {time_span:.4f} s")

    transfer_rate = (n - 1) / time_span
    clock_rate = tick_span / time_span

    dsample = np.diff(samples)  # rows: [0] = Δt in ns, [1] = Δticks

    corr = np.corrcoef(dsample)[0, 1]  # rows are the variables → corr(Δt, Δtick)
    perc = np.percentile(dsample, [0, 5, 50, 95, 100], axis=1) * [1e-6, 1]
    # perc: [5 percentiles, 2 vars], col 0 converted ns→ms;
    # order: min, p5, median, p95, max
    p_dt_ms, p_dtick = perc[:, 0], perc[:, 1]

    print(f"\nspan:  {time_span:.3f} s / {tick_span} ticks / {n} samples")
    print(f"rate:  transfer {transfer_rate:.1f} Hz  / clock {clock_rate:.1f} Hz")
    print(
        f"\u0394t:       median {p_dt_ms[2]:.3f} ms "
        f"(min: {p_dt_ms[0]:.3f} / p5: {p_dt_ms[1]:.3f} / "
        f"p95: {p_dt_ms[3]:.3f} / max: {p_dt_ms[4]:.3f})"
    )
    print(
        f"\u0394clock:  median {p_dtick[2]:.0f} ticks "
        f"(min: {p_dtick[0]:.0f} / max: {p_dtick[4]:.0f})"
    )
    print(f"corr(\u0394t,\u0394clock): {corr:.3f}")


if __name__ == "__main__":
    main()
