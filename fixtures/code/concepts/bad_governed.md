---
type: concept
id: CON-CODE-BADGOV
title: Fixture -- governed_by references an unknown topic id
owner_boundary: "fixtures/code -- test fixtures only"
implemented_by:
  - NestedTypes.swift#outerFunc
tested_by:
  - concepts/topics/top-code-1.md
governed_by: [TOP-DOES-NOT-EXIST]
involved_in: []
---

Fixture: `governed_by` names a topic id that doesn't exist anywhere in the linted corpus, to
exercise memlint's unknown-governed_by-id check. NOT this concept: this check only fires when
the corpus actually contains at least one topic record (a registry to check against) -- see the
no-topic-registry test, which proves the same bad id is silently skipped without one.
