---
type: concept
id: CON-CODE-GOOD
title: Fixture -- clean concept
owner_boundary: "fixtures/code -- test fixtures only"
implemented_by:
  - NestedTypes.swift#outerFunc
tested_by:
  - concepts/topics/top-code-1.md
governed_by: [TOP-CODE-1]
involved_in: []
---

Why one concept: a clean fixture exercising every passing path of memlint's concept rules at
once. NOT this concept: it isn't testing any single rule's failure mode -- see the sibling
bad_* fixtures for that.
