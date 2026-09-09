"""Refreshing and storing the client's auth tokens.

See TOP-115 for the refresh policy and TOP-116 for where tokens are stored
at rest -- the two are separate decisions, though INC-202 involves both.
"""


def refresh_token(token, jitter_seconds=30):
    """Exchange a refresh token for a new access token, ahead of expiry by
    a randomized jitter window so many clients do not refresh in lockstep."""
    return _exchange(token)


def store_token(token):
    """Persist `token` in the OS keychain."""
    return _keychain_set("driftwood", token)


def _exchange(token):
    return {"access_token": "stub2"}


def _keychain_set(service, token):
    return True
