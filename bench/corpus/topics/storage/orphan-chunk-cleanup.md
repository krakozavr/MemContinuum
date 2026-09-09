---
type: topic
id: TOP-104
title: Garbage collection for chunks nothing references any more
area: storage
project: driftwood
current: L1
code_refs:
  - src/storage/dedup/store.py
tags: [driftwood, storage, dedup, gc]
links:
  - link: L1
    date: 2025-06-02
    status: active
    kind: adopted
    ruling:
      text: "A periodic sweep deletes any chunk whose reference count has reached zero, holding the store's write lock for the duration of the sweep."
      authority: agent-inference
      source: "incident follow-up 2025-06-02"
    rationale:
      text: "Deleting or editing a file drops references to the chunks it used, but content addressing means those chunks might still be referenced by another file, so they can only be deleted once nothing points at them at all. The write lock exists because an unlocked sweep can delete a chunk between another request checking it exists and that request writing to it -- see the incident below."
      authority: agent-inference
    evidence:
      - "INC-201: dedup store corruption from an unlocked GC sweep racing a concurrent chunk write"
    revisit_if:
      - "the write lock becomes a throughput bottleneck on a store with heavy concurrent writers"
    recorded_by: agent
    recorded_at: 2025-06-02
---
# Chunk garbage collection

The dedup store (TOP-101) never deletes a chunk just because one file
stopped using it -- another file might still need it. This topic is the
sweep that finds chunks nothing references at all and reclaims the space,
and the locking that keeps it from colliding with a write in flight.

Not this topic: how a chunk gets a reference in the first place (TOP-101)
or how long a deleted FILE stays recoverable before its own record is
purged (TOP-105, a different layer -- trash retention, not chunk GC).
