---
type: concept
id: CON-CODE-BADSYM
title: Fixture -- #symbol fragment does not exist in the file
owner_boundary: "fixtures/code -- test fixtures only"
implemented_by:
  - NestedTypes.swift#doesNotExist
tested_by:
  - concepts/topics/top-code-1.md
governed_by: []
involved_in: []
---

Fixture: `#doesNotExist` names no func/struct/enum/class/subscript actually declared in
NestedTypes.swift, to exercise memlint's #symbol-existence check. NOT this concept: it isn't
about a missing FILE (see the plain missing-path fixture in fixtures/v11/concepts/) -- the file
exists, only the symbol doesn't.
