"""Shared helpers for the QtGUI."""


def safe_getattr(obj, *attrs, default=None):
    """Walk `obj` through `attrs`, returning `default` if any step is missing.

    A guarded, nested getattr: each attribute read is wrapped so a missing
    attribute (or an exception raised during a remote/device read) yields
    `default` instead of raising. Multi-level paths are passed as separate
    arguments, e.g.::

        cur = safe_getattr(controller, "config", "input_mode")

    Replaces scattered ``try/except: pass`` read blocks (Plan.md §4.1) while
    keeping the caller responsible for handling `None`/`default`.
    """
    for attr in attrs:
        try:
            obj = getattr(obj, attr, None)
        except Exception:
            return default
        if obj is None:
            return default
    return obj
