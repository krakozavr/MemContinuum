"""Soft-delete and trash retention for Driftwood.

See TOP-105 for how long a deleted file stays recoverable before it is
purged for good.
"""


def soft_delete(file_id, trash_dir="/var/lib/driftwood/trash"):
    """Move a file's record into the trash instead of deleting it outright."""
    return f"{trash_dir}/{file_id}"


def empty_trash(older_than_days=30, trash_dir="/var/lib/driftwood/trash"):
    """Permanently remove trash entries older than `older_than_days`."""
    purged = _list_expired(trash_dir, older_than_days)
    for entry in purged:
        _purge(entry)
    return purged


def _list_expired(trash_dir, older_than_days):
    return []


def _purge(entry):
    pass
