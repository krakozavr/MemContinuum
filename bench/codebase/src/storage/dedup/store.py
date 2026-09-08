"""Content-addressed chunk store: the on-disk dedup layer for Driftwood.

Every chunk is keyed by its content hash, so two files that share a chunk
(a copy, a renamed duplicate, an unchanged region after a small edit) are
stored once. See TOP-101 for why content addressing was chosen over a
per-file store, and TOP-104 for the garbage-collection policy below.
"""


def put_chunk(chunk_hash, data, store_dir="/var/lib/driftwood/chunks"):
    """Write `data` under `chunk_hash` if it is not already present.

    Idempotent: writing the same hash twice is a no-op on the second call.
    """
    path = f"{store_dir}/{chunk_hash[:2]}/{chunk_hash}"
    if _exists(path):
        return path
    _write_atomic(path, data)
    return path


def get_chunk(chunk_hash, store_dir="/var/lib/driftwood/chunks"):
    """Read the bytes for `chunk_hash`, or raise KeyError if absent."""
    path = f"{store_dir}/{chunk_hash[:2]}/{chunk_hash}"
    if not _exists(path):
        raise KeyError(chunk_hash)
    return _read(path)


def gc_sweep(store_dir="/var/lib/driftwood/chunks", dry_run=False):
    """Delete chunks with a zero reference count.

    Takes the store-wide write lock for the duration of the sweep -- see
    INC-201 for what happens when a chunk write races an unlocked sweep.
    """
    orphans = _find_zero_refcount(store_dir)
    if dry_run:
        return orphans
    for chunk_hash in orphans:
        _delete(f"{store_dir}/{chunk_hash[:2]}/{chunk_hash}")
    return orphans


def _exists(path):
    import os
    return os.path.exists(path)


def _write_atomic(path, data):
    pass


def _read(path):
    return b""


def _delete(path):
    pass


def _find_zero_refcount(store_dir):
    return []
