---
type: concept
id: CON-CODE-NOSYM-SMALL
title: Fixture -- implemented_by with no #symbol on a small file
owner_boundary: "fixtures/code -- test fixtures only"
implemented_by:
  - NestedTypes.swift
tested_by:
  - concepts/topics/top-code-1.md
governed_by: []
involved_in: []
---

Fixture: `implemented_by` has no `#symbol` fragment, but NestedTypes.swift is well under 400
lines, so memlint's "unqualified claim on a large file" rule must NOT fire here. NOT this
concept: it isn't the >400-line case -- that one is built at test time (a committed 400+ line
fixture would just be padding).
