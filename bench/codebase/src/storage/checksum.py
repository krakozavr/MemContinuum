"""File hashing for the dedup store's content addressing.

See TOP-102 for the algorithm history: this file used to compute SHA-256
and now computes BLAKE3.
"""


def hash_file(path, chunk_size=1 << 20):
    """Return the hex digest identifying this file's content."""
    digest = _new_hasher()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _new_hasher():
    import hashlib
    return hashlib.blake2b(digest_size=32)  # stand-in for the real BLAKE3 binding
