"""Per-file version history for Driftwood.

See TOP-106 for how many past versions of a file are kept.
"""


def prune_versions(file_id, keep=10):
    """Drop all but the newest `keep` versions of `file_id`."""
    versions = _list_versions(file_id)
    for stale in versions[keep:]:
        _delete_version(stale)
    return versions[:keep]


def _list_versions(file_id):
    return []


def _delete_version(version):
    pass
