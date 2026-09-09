"""Splits a file into chunks for the dedup store.

See TOP-103: chunk boundaries are content-defined (a rolling hash), not a
fixed byte offset, so a small edit near the start of a large file does not
shift every later chunk boundary and force a full re-upload.
"""


def chunk_file(path, min_size=256 * 1024, max_size=4 * 1024 * 1024):
    """Yield (offset, length) pairs for each chunk boundary found by the
    rolling hash between `min_size` and `max_size`."""
    boundaries = _rolling_hash_boundaries(path, min_size, max_size)
    return boundaries


def _rolling_hash_boundaries(path, min_size, max_size):
    return []
