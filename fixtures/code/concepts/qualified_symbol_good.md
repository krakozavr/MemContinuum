---
type: concept
id: CON-CODE-QUALGOOD
title: Fixture -- qualified #symbol fragment accepted
owner_boundary: "fixtures/code -- test fixtures only"
implemented_by:
  - NestedTypes.swift#Outer.outerFunc
tested_by: []
governed_by: []
involved_in: []
---

Why one concept: proves memlint accepts a QUALIFIED #symbol fragment (Outer.outerFunc) exactly
as code-search's runtime concept attachment does (finding 4) -- the fragment names a member via
its full dotted qualifier, not just the bare member name. NOT this concept: it isn't testing the
bare-name form (see good.md) or the path-containment checks (see the inline escape tests in
tests/test_code_index.py).
