---
type: topic
id: TOP-9010
title: Bad fixture — a reversed link's target is still active
area: reference/testing
current: L2
links:
  - link: L2
    date: 2026-08-30
    status: active
    kind: reversed
    reverses: L1
    reason_for_change: new-evidence
    ruling:
      text: "changed my mind again"
      authority: agent-inference
    recorded_by: agent
    recorded_at: 2026-08-30
  - link: L1
    date: 2026-08-01
    status: active
    kind: adopted
    ruling:
      text: "the original answer"
      authority: agent-inference
    recorded_by: agent
    recorded_at: 2026-08-29
---

Fixture for memlint: a `kind: reversed` link naming a `reverses:` target
whose status is still `active` must be rejected -- the target must first be
marked superseded (or otherwise moved off active/provisional).
