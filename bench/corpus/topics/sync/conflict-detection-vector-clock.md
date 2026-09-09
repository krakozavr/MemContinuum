---
type: topic
id: TOP-108
title: Conflicts are detected with vector clocks, not timestamps
area: sync
project: driftwood
current: L1
code_refs:
  - src/sync/conflict/vector_clock.py
tags: [driftwood, sync, conflict]
links:
  - link: L1
    date: 2025-02-10
    status: active
    kind: adopted
    ruling:
      text: "Each device keeps a per-device vector clock on every file version; a version is a true concurrent conflict only when neither version's clock is greater-than-or-equal to the other's, never decided from wall-clock timestamps."
      authority: agent-inference
      source: "design note 2025-02-10"
    rationale:
      text: "Wall-clock time is unreliable across devices (skew, timezone bugs, a clock that runs backward after sleep) and cannot tell a genuine concurrent edit apart from a normal sequential edit that merely arrived late over a slow connection. A vector clock encodes the actual causal history -- which version an edit descended from -- so it answers 'is this really concurrent' correctly regardless of what any device's clock says."
      authority: agent-inference
    recorded_by: agent
    recorded_at: 2025-02-10
---
# Conflict detection

Before Driftwood can decide what to DO about two edits to the same file
(TOP-107), it first has to correctly tell apart a real concurrent edit
from an edit that simply arrived late. That's this topic: a small causal
clock carried alongside each version, compared instead of trusting any
device's own idea of the current time.

Not this topic: what happens once a conflict is confirmed (TOP-107).
