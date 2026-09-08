"""Filesystem watcher event debouncing.

See TOP-117 and INC-203 -- a window that is too short queues a duplicate
upload per intermediate write during a large, fast-writing operation like a
git checkout.
"""


def debounce_events(events, window_ms=750):
    """Collapse a burst of events on the same path within `window_ms` into
    a single change notification."""
    collapsed = {}
    for path, ts in events:
        collapsed[path] = ts
    return list(collapsed.items())
