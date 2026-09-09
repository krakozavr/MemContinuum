---
type: topic
id: TOP-105
title: Deleted files stay recoverable for 30 days
area: storage
project: driftwood
current: L1
code_refs:
  - src/storage/trash.py
tags: [driftwood, storage, trash]
links:
  - link: L1
    date: 2025-04-08
    status: active
    kind: adopted
    ruling:
      text: "Deleting a file moves its record to trash rather than removing it outright; trash entries are purged for good after 30 days."
      authority: agent-inference
      source: "design note 2025-04-08"
    rationale:
      text: "An accidental delete synced across every device before the user noticed is the single most-feared failure mode in a sync tool; a recovery window turns that from data loss into an inconvenience. 30 days was chosen as long enough to cover 'I didn't notice until next month's report' without keeping trash growing forever."
      authority: agent-inference
    alternatives:
      - {option: "delete immediately, rely on the version history instead", rejected_because: "version history (TOP-106) is keyed per surviving file and offers nothing once the file record itself is gone", authority: agent-inference}
    recorded_by: agent
    recorded_at: 2025-04-08
---
# Trash retention

A delete is not instant. It moves the file into a recoverable trash state
first, and only after the retention window closes is the record actually
gone.

Not this topic: how many past VERSIONS of a still-existing file are kept
(TOP-106) -- trash is about a whole file being removed, versions are about
one file's own edit history.
