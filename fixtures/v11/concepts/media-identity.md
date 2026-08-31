---
type: concept
id: CON-007
title: Delete Gate
owner_boundary: "Sources/Delete — the sole path files are removed through"
implemented_by:
  - Sources/Delete/DeleteGate.swift#delete
tested_by:
  - Tests/DeleteGateTests.swift
governed_by: [TOP-0100, TOP-0042]
involved_in: []
---

Why one concept: DeleteGate is the single point of file deletion, so every removal side effect
is traceable to one call site. What is NOT this concept: it doesn't decide *what* to delete,
only performs the removal once a caller has decided.
