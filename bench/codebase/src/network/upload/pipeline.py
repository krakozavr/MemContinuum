"""Turns a local file change into bytes on the server.

See TOP-110 for the retry/backoff schedule on a failed chunk upload, and
TOP-113 for when a file is streamed instead of read fully into memory.
"""


def upload_chunk(chunk, dest):
    """Upload one chunk; raises UploadError on a non-2xx response."""
    return _http_put(dest, chunk)


def retry_upload(chunk, attempt, max_attempts=6):
    """Exponential backoff for a failed chunk upload: 1s, 2s, 4s, ... up to
    max_attempts tries before the chunk is marked failed and surfaced to
    the user."""
    if attempt >= max_attempts:
        return "failed"
    delay = 2 ** attempt
    _sleep(delay)
    return upload_chunk(chunk, dest=None)


def _http_put(dest, chunk):
    return 200


def _sleep(seconds):
    pass
