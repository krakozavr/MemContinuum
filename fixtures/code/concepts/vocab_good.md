---
type: concept
id: CON-VOCAB-GOOD
title: Fixture -- memlint #symbol vocabulary, passing cases
owner_boundary: "fixtures/code -- test fixtures only"
implemented_by:
  - InitSubscriptOperator.swift#init
  - SyntheticSettingsController.swift#rowCount
  - MemlintVocabulary.swift#realSymbol
  - MemlintVocabulary.swift#staticHelper
  - MemlintVocabulary.swift#escaped
tested_by: []
governed_by: []
involved_in: []
---

Why one concept: finding 5 -- memlint's #symbol vocabulary check must recognize everything
the chunker emits (init, computed var, static func, backtick-quoted names), not just a plain
`func NAME`/`class NAME`. NOT this concept: comment/string-only mentions of a name that was
never actually declared -- see vocab_bad_comment.md / vocab_bad_string.md.
