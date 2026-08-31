---
type: concept
id: CON-VOCAB-BADSTRING
title: Fixture -- #symbol only appears inside a string literal
owner_boundary: "fixtures/code -- test fixtures only"
implemented_by:
  - MemlintVocabulary.swift#stringOnlySymbol
tested_by: []
governed_by: []
involved_in: []
---

Fixture: `#stringOnlySymbol` names something that only ever appears inside a string literal in
MemlintVocabulary.swift, never as an actual declaration -- must still be an error (finding 5:
stop matching inside comments/strings). NOT this concept: a symbol that genuinely doesn't
exist anywhere in the file (see bad_symbol.md) -- this one merely LOOKS declared to a naive
text scan.
