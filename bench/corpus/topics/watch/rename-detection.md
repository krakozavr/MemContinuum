---
type: topic
id: TOP-118
title: A rename is detected by comparing inode numbers
area: watch
project: driftwood
current: L1
code_refs:
  - src/watch/rename.py
tags: [driftwood, watch, rename]
links:
  - link: L1
    date: 2025-04-22
    status: active
    kind: adopted
    ruling:
      text: "The watcher tells a rename apart from an unrelated delete-plus-create by comparing the filesystem inode number of the removed path against every newly created path, not by comparing file content."
      authority: agent-inference
      source: "design note 2025-04-22"
    rationale:
      text: "Comparing content would misfire on two unrelated files that happen to be byte-identical (an empty file, a common template), and it costs a full read of both files just to check. The inode is the filesystem's own identity for 'this is the same underlying file', so it is both cheaper and more correct for this specific question -- known to be defeated only when the filesystem itself reuses an inode number, which is rare and is its own limitation, not a reason to switch away from inode comparison as the primary signal."
      authority: agent-inference
    evidence:
      - "INC-204: inode reuse after a filesystem rebuild caused a false-positive rename detection on ext4"
    revisit_if:
      - "inode reuse after a filesystem rebuild turns out to be common enough that a content-based fallback check becomes worth its cost"
    recorded_by: agent
    recorded_at: 2025-04-22
---
# Rename detection

Recognizing a rename matters because a true rename should sync as a cheap
metadata change, not as a delete of the old path plus a full re-upload
under the new one. This topic is the signal the watcher trusts to make
that call.

Not this topic: how a burst of raw events is collapsed before the watcher
even gets to reason about rename-vs-delete (TOP-117).
