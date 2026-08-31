---
type: topic
id: TOP-9006
title: Warn fixture — processing topic with no code_refs
area: processing/warn-test
current: L1
links:
  - link: L1
    date: 2026-08-01
    status: active
    kind: adopted
    ruling:
      text: "a processing-area decision that never got a code_refs entry"
      authority: agent-inference
    recorded_by: agent
    recorded_at: 2026-08-29
---

Fixture for memlint: a topic in area `processing/*` with no `code_refs` must warn, not error
(exit 0 on its own).
