---
type: topic
id: TOP-102
title: Content hash algorithm for the dedup store
area: storage
project: driftwood
current: L2
code_refs:
  - src/storage/checksum.py
tags: [driftwood, storage, dedup, hashing]
links:
  - link: L2
    date: 2025-05-11
    status: active
    kind: reversed
    reverses: L1
    reason_for_change: new-evidence
    ruling:
      text: "The chunk content hash is BLAKE3, replacing the SHA-256 hash used at launch."
      authority: agent-inference
      source: "storage profiling session 2025-05-11"
    rationale:
      text: "A profiling pass on a real user's first sync (140GB, cold cache) showed hashing was the single largest CPU cost in the initial-sync path, ahead of network I/O. BLAKE3 measured five to ten times faster than SHA-256 on the same machines with no meaningful change to collision risk at the digest length used here."
      authority: agent-inference
    evidence:
      - "storage profiling report 2025-05-11: hashing 61% of CPU time on a cold 140GB initial sync under SHA-256, 9% under BLAKE3 on the same machine"
    recorded_by: agent
    recorded_at: 2025-05-11
  - link: L1
    date: 2025-02-03
    status: superseded
    superseded_by: L2
    kind: adopted
    ruling:
      text: "The chunk content hash is SHA-256."
      authority: agent-inference
      source: "design note 2025-02-03, storage layer kickoff"
    rationale:
      text: "SHA-256 needed no extra dependency and every language binding already had a fast implementation, which mattered more than raw speed before real-world sync volume existed to profile against."
      authority: agent-inference
    recorded_by: agent
    recorded_at: 2025-02-03
---
# Content hash algorithm

The dedup store's content-addressing (TOP-101) needs one hash function
computing the storage key for every chunk. That function started as
SHA-256 for its ubiquity and was replaced once real sync traffic showed
hashing itself, not the network, was the bottleneck on a large initial
sync.

Not this topic: whether content addressing is used at all (TOP-101) or how
big a chunk is before it gets hashed (TOP-103).
