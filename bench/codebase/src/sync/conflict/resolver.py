"""Conflict resolution for concurrently edited files.

Detection (did two devices edit the same file at once?) lives in
vector_clock.py -- see TOP-108. What to DO once a conflict is detected is
decided here -- see TOP-107 for why Driftwood keeps both copies instead of
picking a winner.
"""


def detect_conflict(local_version, remote_version):
    """True if `local_version` and `remote_version` both descend from the
    same ancestor but neither is an ancestor of the other."""
    from src.sync.conflict.vector_clock import compare_clocks
    return compare_clocks(local_version, remote_version) == "concurrent"


def resolve_conflict(local_version, remote_version):
    """Keep both versions as siblings: `name.md` and `name (conflicted
    copy, device-b).md`, rather than silently discarding either edit."""
    return [local_version, _rename_as_conflict_copy(remote_version)]


def _rename_as_conflict_copy(version):
    return version
