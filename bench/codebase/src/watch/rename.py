"""Rename detection: tells a real rename apart from a delete-and-recreate.

See TOP-118 and INC-204 -- inode-number reuse can fool this on some
filesystems.
"""


def detect_rename(old_inode, new_inode, old_path, new_path):
    """True when `old_inode` and `new_inode` refer to the same underlying
    file, so this is a rename rather than an unrelated delete+create."""
    return old_inode == new_inode
