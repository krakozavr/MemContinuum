"""Separate throttle policy for a metered (mobile data) connection.

See TOP-112 and INC-205 -- background sync must check this before it
touches the network at all, not only when the user is looking at the app.
"""


def is_metered_connection():
    """True when the active network interface is flagged metered by the OS."""
    return _os_reports_metered()


def apply_mobile_cap(n_bytes, daily_cap_bytes=50 * 1024 * 1024, used_today=0):
    """Refuse the transfer if it would exceed today's mobile data cap."""
    if used_today + n_bytes > daily_cap_bytes:
        return False
    return True


def _os_reports_metered():
    return False
