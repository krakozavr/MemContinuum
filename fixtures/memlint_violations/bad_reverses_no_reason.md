---
type: topic
id: TOP-9003
title: Bad fixture — reverses without reason_for_change
area: reference/testing
current: L2
links:
  - link: L2
    date: 2026-08-10
    status: active
    kind: reversed
    reverses: L1
    ruling:
      text: "changed my mind, but forgot to say why"
      authority: agent-inference
    recorded_by: agent
    recorded_at: 2026-08-29
  - link: L1
    date: 2026-08-01
    status: superseded
    superseded_by: L2
    kind: adopted
    ruling:
      text: "the original answer"
      authority: agent-inference
    recorded_by: agent
    recorded_at: 2026-08-29
---

Fixture for memlint: a link with `reverses:` and no `reason_for_change` must be rejected.
