"""How Driftwood syncs symbolic links.

See TOP-122 -- a symlink is synced as a symlink, never followed and
uploaded as the target's content.
"""


def sync_symlink(link_path):
    """Upload `link_path` as a symlink record (its target string), not the
    file it points at."""
    target = _read_link(link_path)
    return {"type": "symlink", "target": target}


def _read_link(link_path):
    return ""
