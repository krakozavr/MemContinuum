---
type: concept
id: CON-302
title: Conflict Resolver
owner_boundary: "src/sync/conflict -- deciding whether two edits to the same file are a genuine concurrent conflict, and what happens to the file once one is confirmed."
implemented_by:
  - src/sync/conflict/resolver.py#detect_conflict
  - src/sync/conflict/resolver.py#resolve_conflict
  - src/sync/conflict/vector_clock.py#compare_clocks
tested_by:
  - tests/test_conflict.py
governed_by: [TOP-107, TOP-108]
involved_in: []
tags: [driftwood, sync]
---
# Conflict Resolver

Everything about recognizing and handling a concurrent edit to the same
file lives here: the vector-clock comparison that tells a real conflict
apart from an ordinary sequential edit, and the policy for what happens
to the file once a conflict is confirmed.

This is NOT this concept: retrying a merge that failed against the current server
state (`sync/merge_queue.py`, governed by TOP-109) -- that runs AFTER this
concept has already decided a conflict exists, and answers a scheduling
question, not a conflict-resolution one.
