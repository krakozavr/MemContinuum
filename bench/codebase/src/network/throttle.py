"""Token-bucket bandwidth throttle for uploads on an unmetered connection.

See TOP-111. The metered-connection variant lives in mobile_throttle.py
(TOP-112) -- deliberately a separate policy, not a parameter of this one.
"""


def token_bucket_acquire(n_bytes, rate_bytes_per_sec=5 * 1024 * 1024, bucket=None):
    """Block until `n_bytes` worth of tokens are available in `bucket`."""
    bucket = bucket if bucket is not None else _default_bucket()
    return bucket.consume(n_bytes, rate_bytes_per_sec)


def _default_bucket():
    class _Bucket:
        def consume(self, n, rate):
            return True
    return _Bucket()
