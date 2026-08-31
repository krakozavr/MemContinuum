---
type: topic
id: TOP-9002
title: Bad fixture — superseded without superseded_by
area: reference/testing
current: L2
links:
  - link: L2
    date: 2026-08-10
    status: active
    kind: adopted
    ruling:
      text: "the current answer"
      authority: agent-inference
    recorded_by: agent
    recorded_at: 2026-08-29
  - link: L1
    date: 2026-08-01
    status: superseded
    kind: adopted
    ruling:
      text: "the old answer, replaced but the pointer was forgotten"
      authority: agent-inference
    recorded_by: agent
    recorded_at: 2026-08-29
---

Fixture for memlint: a link with `status: superseded` and no `superseded_by` must be rejected.
