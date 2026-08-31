---
type: concept
id: CON-CODE-DUP-A
title: Fixture -- first of two concepts claiming the same path#symbol
owner_boundary: "fixtures/code -- test fixtures only"
implemented_by:
  - NestedTypes.swift#outerFunc
tested_by:
  - concepts/topics/top-code-1.md
governed_by: []
involved_in: []
---

Fixture: claims `NestedTypes.swift#outerFunc`, the same path#symbol dup_b.md also claims, to
exercise memlint's duplicate-implemented_by-claim check. NOT this concept: neither dup_a nor
dup_b is the "real" owner -- the whole point of this pair is that the tool can't tell, and must
say so.
