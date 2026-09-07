---
type: topic
id: TOP-9011
title: Clean fixture — a properly superseded reversal
area: processing/testing
code_refs:
  - src/clean_reversal.py
current: L2
links:
  - link: L2
    date: 2026-08-30
    status: active
    kind: reversed
    reverses: L1
    reason_for_change: new-evidence
    ruling:
      text: "changed my mind, this time correctly"
      authority: agent-inference
    recorded_by: agent
    recorded_at: 2026-08-30
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

Clean fixture: `L2` (`kind: reversed`) correctly reverses `L1`, which is
`status: superseded` -- no longer active or provisional.
