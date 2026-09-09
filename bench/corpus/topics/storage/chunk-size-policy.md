---
type: topic
id: TOP-103
title: Chunk boundaries are content-defined, not fixed-size
area: storage
project: driftwood
current: L2
code_refs:
  - src/storage/chunker.py
tags: [driftwood, storage, dedup, chunking]
links:
  - link: L2
    date: 2025-03-20
    status: active
    kind: reversed
    reverses: L1
    reason_for_change: new-evidence
    ruling:
      text: "A file is split into chunks between 256KB and 4MB using a rolling hash to find boundaries (content-defined chunking), not fixed 4MB offsets."
      authority: agent-inference
      source: "bug triage session 2025-03-20"
    rationale:
      text: "With fixed offsets, inserting even one byte near the start of a large file shifts every chunk boundary after it, so the whole file re-hashes as new content and re-uploads in full. A rolling-hash boundary re-synchronizes a few chunks after the insertion point and leaves the rest of the file's chunks unchanged, so an edit near the start of a large video file costs one small upload instead of a full re-upload."
      authority: agent-inference
    evidence:
      - "bug report: a one-line metadata edit at the top of a 2GB video file forced a full 2GB re-upload under fixed chunking"
    recorded_by: agent
    recorded_at: 2025-03-20
  - link: L1
    date: 2025-02-03
    status: superseded
    superseded_by: L2
    kind: adopted
    ruling:
      text: "A file is split into fixed 4MB chunks."
      authority: agent-inference
      source: "design note 2025-02-03, storage layer kickoff"
    rationale:
      text: "Fixed-size chunking is the simplest possible splitter and was enough to ship the first version of the dedup store."
      authority: agent-inference
    recorded_by: agent
    recorded_at: 2025-02-03
---
# Chunk boundaries

Every file has to be cut into pieces before those pieces can be
content-addressed (TOP-101) and hashed (TOP-102). Where the cuts fall
turns out to matter a lot for how much re-uploads an edit near the start
of a large file: a fixed offset shifts every later boundary; a
content-defined one mostly doesn't.

Not this topic: what the pieces are keyed by once cut (TOP-101/TOP-102).
