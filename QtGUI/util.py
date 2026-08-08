"""Shared helpers for the QtGUI."""

import asyncio

from fibre.libfibre import ObjectLostError

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


def safe_getattr(obj, *attrs, default=None):
    """Walk `obj` through `attrs`, returning `default` if any step is missing.

    A guarded, nested getattr: each attribute read is wrapped so a missing
    attribute (or a transient device read error) yields `default` instead of
    raising. Multi-level paths are passed as separate arguments, e.g.::

        cur = safe_getattr(controller, "config", "input_mode")

    Replaces scattered ``try/except: pass`` read blocks while
    keeping the caller responsible for handling `None`/`default`. Only
    `DEVICE_EXCEPTIONS` are swallowed; genuine libfibre errors propagate.
    """
    for attr in attrs:
        try:
            obj = getattr(obj, attr, None)
        except DEVICE_EXCEPTIONS:
            return default
        if obj is None:
            return default
    return obj
