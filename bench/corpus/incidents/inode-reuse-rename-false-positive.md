---
type: incident
id: INC-204
title: Inode reuse after a filesystem rebuild produced a false-positive rename
area: watch
date: '2025-04-25'
status: active
authority: agent-inference
source: "user report and reproduction 2025-04-25"
evidence:
  - "a user rebuilt their filesystem (backup/restore onto a fresh volume) and the restore tool reused a low inode number for an unrelated file"
  - "the watcher matched the reused inode number against a file deleted earlier in the same session and treated an unrelated new file as a rename of it, merging the two files' sync history"
code_refs:
  - src/watch/rename.py
---
# False-positive rename from inode reuse

A user restored a large folder from backup onto a freshly formatted
volume. The restore process allocated inode numbers in whatever order the
filesystem handed them out, and one of those numbers happened to match the
inode of a file the watcher had seen deleted minutes earlier in the same
session. The watcher's rename detector (TOP-118) treated the new,
unrelated file as a rename of the deleted one, merging their sync history.

This is a known, documented limitation of inode-based rename detection
(TOP-118 records it under `revisit_if` rather than reversing the
decision): inode reuse is rare enough on a live, non-rebuilt filesystem
that the decision stands, but a fresh restore onto a reformatted volume is
exactly the case where it can occur.
