---
type: topic
id: TOP-9004
title: Bad fixture — current does not equal newest active link
area: reference/testing
current: L1
links:
  - link: L2
    date: 2026-08-10
    status: active
    kind: adopted
    ruling:
      text: "the actually-current answer"
      authority: agent-inference
    recorded_by: agent
    recorded_at: 2026-08-29
  - link: L1
    date: 2026-08-01
    status: historical
    kind: adopted
    ruling:
      text: "the stale answer the frontmatter still points at"
      authority: agent-inference
    recorded_by: agent
    recorded_at: 2026-08-29
---

Fixture for memlint: frontmatter `current: L1` but the newest link with `status: active` is `L2`
— must be rejected, and the error must name the correct value (L2).
