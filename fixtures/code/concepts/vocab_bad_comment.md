---
type: concept
id: CON-VOCAB-BADCOMMENT
title: Fixture -- #symbol only appears inside a comment
owner_boundary: "fixtures/code -- test fixtures only"
implemented_by:
  - MemlintVocabulary.swift#commentedOutSymbol
tested_by: []
governed_by: []
involved_in: []
---

Fixture: `#commentedOutSymbol` names something that only ever appears inside a `//` comment in
MemlintVocabulary.swift, never as an actual declaration -- must still be an error (finding 5:
stop matching inside comments/strings). NOT this concept: a symbol that genuinely doesn't
exist anywhere in the file (see bad_symbol.md) -- this one merely LOOKS declared to a naive
text scan.
