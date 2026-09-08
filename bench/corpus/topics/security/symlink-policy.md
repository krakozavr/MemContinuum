---
type: topic
id: TOP-122
title: A symlink syncs as a symlink, never as the target's content
area: security
project: driftwood
current: L1
code_refs:
  - src/security/symlinks.py
tags: [driftwood, security, symlinks]
links:
  - link: L1
    date: 2025-07-01
    status: active
    kind: adopted
    ruling:
      text: "A symbolic link is synced as a symlink record holding its target path string; it is never followed and uploaded as though it were the target file's own content."
      authority: agent-inference
      source: "design note 2025-07-01"
    rationale:
      text: "Following a symlink during sync can walk outside the folder the user actually intended to share -- a symlink inside a synced folder can point anywhere on the filesystem, including at a sensitive file the user never meant to upload. Treating the symlink as its own small record avoids ever reading a file the user didn't explicitly put in the synced folder."
      authority: agent-inference
    alternatives:
      - {option: "follow the symlink and sync the target file's content", rejected_because: "a symlink can point outside the synced folder entirely, turning an ordinary sync into an unintended read of an arbitrary file on the filesystem", authority: agent-inference}
    recorded_by: agent
    recorded_at: 2025-07-01
---
# Symlink handling

A symlink is data about a link, not the bytes it points at, and Driftwood
treats it that way end to end.

Not this topic: encryption of ordinary file content (TOP-121).
