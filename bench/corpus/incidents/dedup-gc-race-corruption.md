---
type: incident
id: INC-201
title: Chunk garbage collection raced a concurrent write and corrupted the dedup store
area: storage
date: '2025-05-30'
status: active
authority: agent-inference
source: "on-call postmortem 2025-05-30"
evidence:
  - "a chunk written by an in-flight upload was deleted by a concurrent GC sweep milliseconds later, because the sweep held no lock against writers"
  - "the affected file's re-download after resync showed a truncated chunk where the deleted one had been"
code_refs:
  - src/storage/dedup/store.py
---
# Dedup store corruption from an unlocked GC sweep

The garbage-collection sweep computed its list of zero-reference chunks,
then deleted them one at a time, with no lock held against a concurrent
chunk write landing in between. A chunk that gained a new reference in the
gap between "counted as orphaned" and "deleted" was removed anyway,
leaving the file that had just started referencing it silently missing a
chunk on disk.

The fix (TOP-104) makes the sweep hold the store's write lock for its
whole duration, not just its final delete step.
