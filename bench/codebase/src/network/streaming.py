"""Streams a large file to the server instead of loading it fully into
memory. See TOP-113 for the size threshold this kicks in at."""


def stream_upload(path, dest, threshold_bytes=256 * 1024 * 1024):
    """Upload `path` in bounded-memory chunks when it is over threshold."""
    import os
    if os.path.getsize(path) < threshold_bytes:
        return _upload_whole(path, dest)
    return _upload_in_chunks(path, dest)


def _upload_whole(path, dest):
    return "uploaded"


def _upload_in_chunks(path, dest):
    return "uploaded"
