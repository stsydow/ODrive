# Fork notes: 0.5.7-hardened tooling

This vendored copy of the ODrive 0.5.6 Python tooling (`fibre/libfibre.py`)
carries deliberate deviations from upstream. Every change is marked with a
`Fork note (0.5.7-hardened)` comment in place.

## Deviations from upstream 0.5.6

1. **`_get_exception(kFibreInvalidArgument)` raises `ObjectLostError`**
   (was: bare `ctypes.ArgumentError`). That status surfaces when touching an
   object whose C handle was destroyed — i.e. the link is gone — so it belongs
   in the typed "object lost" family, not masquerading as a marshalling error
   under a borrowed ctypes class name.

2. **`EmptyInterface` raises `ObjectLostError` on attribute access/writes**
   (was: generic `AttributeError`/`TypeError` from whatever code touched the
   lost object). A lost object's class is swapped to `EmptyInterface`, so all
   post-loss access now uniformly signals "link gone".

## Why

Consumers (the QtGUI) can catch disconnects with one exception family instead
of four unrelated types leaking from timing-dependent code paths.

3. **`utils.rate_test(device, count=10000, mode='sequential')` re-shaped** —
   upstream 0.5.6 took `(device)` at a fixed 10000 reads and printed frames/s;
   the fork adopts the 0.6 tooling's signature/output (values/s) so consumers
   keep the same call across stacks. Only mode 'sequential' exists here
   ('pipelined'/'batch' need 0.6's async transport). The Plan §3.4 Step-0
   latency/jitter measurement lives GUI-side
   (`QtGUI/tools/bench_poll_rate.py`), not in the fork.
## Known limitation (not fixed)

Blocking reads have no timeout: `run_coroutine_threadsafe()` waits on
`future.result()` indefinitely. Discovery (`find_any`) legitimately blocks
forever, so a blanket timeout would break it; per-call-site timeouts were
judged not worth the surgery for this use case.
