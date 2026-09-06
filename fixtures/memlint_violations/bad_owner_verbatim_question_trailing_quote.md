---
type: topic
id: TOP-9007
title: Bad fixture — owner-verbatim question with a trailing quote artifact
area: reference/testing
current: L1
links:
  - link: L1
    date: 2026-08-29
    status: active
    kind: adopted
    ruling:
      text: "should we do X instead?\" "
      authority: owner-verbatim
      source: "owner message 2026-08-29"
    recorded_by: agent
    recorded_at: 2026-08-29
---

Fixture for memlint: the question-mark check must trim a trailing quote
character and whitespace (a copy-paste artifact) before deciding the text
ends with "?" -- this fixture's `ruling.text` ends `...instead?" ` (a stray
quote then a space), not literally `?`.
