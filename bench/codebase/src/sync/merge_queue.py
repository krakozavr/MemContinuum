"""The merge queue retries a conflicting edit against the latest server
state before giving up and surfacing a conflict copy to the user.

See TOP-109 for the backoff schedule -- deliberately separate from the
upload pipeline's own retry policy (TOP-110), because a merge retry re-runs
conflict detection against a moving target and a tight loop can amplify
load during an outage (INC-206).
"""


def retry_merge(entry, attempt, max_attempts=5):
    """Re-attempt merging `entry` into the current server state.

    Backs off 2**attempt seconds between tries, capped at max_attempts.
    """
    if attempt >= max_attempts:
        return "conflict_copy"
    delay = min(2 ** attempt, 60)
    _sleep(delay)
    return _attempt_merge(entry)


def _sleep(seconds):
    pass


def _attempt_merge(entry):
    return "merged"
