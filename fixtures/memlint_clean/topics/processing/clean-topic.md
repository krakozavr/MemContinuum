---
type: topic
id: TOP-0001
title: Clean fixture — valid topic chain
area: processing/clean-test
current: L2
code_refs:
  - src/core/example.py#thing
links:
  - link: L2
    date: 2026-08-10
    status: active
    kind: reversed
    reverses: L1
    reason_for_change: new-evidence
    ruling:
      text: "the corrected decision"
      authority: owner-ratified
      source: "session 2026-08-10"
    rationale:
      text: "L1 turned out to be based on a stale measurement"
      authority: agent-inference
    recorded_by: agent
    recorded_at: 2026-08-29
  - link: L1
    date: 2026-08-01
    status: superseded
    superseded_by: L2
    kind: adopted
    ruling:
      text: "the original decision"
      authority: agent-inference
    recorded_by: agent
    recorded_at: 2026-08-29
---

A fully valid topic: `current` matches the newest active link, the superseded link names its
successor, the reversal names its reason, and the area has a `code_refs` entry.
