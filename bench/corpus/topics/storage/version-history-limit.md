---
type: topic
id: TOP-106
title: Ten past versions are kept per file
area: storage
project: driftwood
current: L1
code_refs:
  - src/storage/versions.py
tags: [driftwood, storage, versions]
links:
  - link: L1
    date: 2025-04-08
    status: active
    kind: adopted
    ruling:
      text: "Driftwood keeps the newest 10 versions of each file; older versions are pruned as new ones are created."
      authority: agent-inference
      source: "design note 2025-04-08"
    rationale:
      text: "A per-file version count is predictable to the user ('I can go back 10 saves') and, because chunks are shared across versions through the dedup store (TOP-101), the marginal storage cost of an old version is usually small -- most versions of a document differ by only a few chunks."
      authority: agent-inference
    revisit_if:
      - "users ask for time-based retention ('keep everything from the last 30 days') instead of a count"
    recorded_by: agent
    recorded_at: 2025-04-08
---
# Version history limit

Every save creates a new version rather than overwriting the last one in
place, up to a cap of 10 kept versions per file.

Not this topic: what happens when the FILE itself is deleted rather than
edited (TOP-105, trash retention).
