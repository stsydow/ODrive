#!/usr/bin/env python3
"""Poll-rate benchmark for the oscilloscope recorder (Plan.md §3.4 Step 0).

Reads axis0.encoder.pos_estimate N times with timestamps and prints sustained
sample rate and inter-sample gap jitter.

Decision bar (Plan §3.4): >=100 Hz x 3 channels useful, ~200 Hz x 4 ch good.
Single-channel rate is the ceiling; per-channel cost scales roughly linearly.

Usage: python bench_poll_rate.py [n_reads]
"""

import statistics
import sys
import time
from itertools import pairwise

import odrive


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 4000
    print("connecting...")
    odrv = odrive.find_any()
    print(f"reading axis0.encoder.pos_estimate {n} times...")

    stamps = []
    for _ in range(n):
        _v = odrv.axis0.encoder.pos_estimate  # full path each time: one USB read per access
        stamps.append(time.perf_counter())

    gaps = [b - a for a, b in pairwise(stamps)]
    span = stamps[-1] - stamps[0]
    rate = (len(stamps) - 1) / span
    med = statistics.median(gaps)
    print(f"\nsamples      : {len(stamps)}")
    print(f"span         : {span:.3f} s")
    print(f"rate         : {rate:.1f} Hz")
    print(f"gap median   : {med * 1000:.2f} ms")
    print(f"gap min/max  : {min(gaps) * 1000:.2f} / {max(gaps) * 1000:.2f} ms")
    print(f"gap p95/p99  : {statistics.quantiles(gaps, n=20)[18] * 1000:.2f} / "
          f"{statistics.quantiles(gaps, n=100)[98] * 1000:.2f} ms")
    verdict = ("OK" if rate >= 200 else "MARGINAL" if rate >= 100 else "TOO SLOW")
    print(f"bar (>=200 Hz nice, >=100 Hz useful): {verdict}")


if __name__ == "__main__":
    main()
