---
type: concept
id: CON-900
title: Bad fixture — implemented_by path does not exist
owner_boundary: "nowhere"
implemented_by:
  - Sources/Nowhere/DoesNotExist.swift#missing
tested_by:
  - Tests/DeleteGateTests.swift
governed_by: []
involved_in: []
---

Fixture: `implemented_by` names a file that is not in fixtures/v11/code, to exercise memlint's
`--code-root` existence check.
