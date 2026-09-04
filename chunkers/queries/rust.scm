; chunkers/queries/rust.scm  (Task 7: full B2 query, replaces Task 2's smoke content)
;
; ONE pattern is sufficient -- kind promotion to `method` (impl/trait ancestor)
; and impl/trait/mod qualification are both handled by the ancestor walk
; already in chunkers/treesitter.py, driven by the rust row's
; `containers`/`method_if_ancestor_in` data; unlike JS/Java/PHP, no distinct
; grammar node type separates "function" from "method" here.
;
; body: (block) excludes bodyless declarations from ever being captured:
; verified against the real grammar (tree_sitter_rust 0.24.2) that a trait
; method signature without a default body (`fn f(&self);`) and an extern
; block prototype (`extern "C" { fn f(x: i32); }`) both parse as the
; DISTINCT node type `function_signature_item`, never `function_item` -- so
; this pattern already excludes them structurally. The `body:` field
; constraint is added anyway to match the explicit-body convention
; java.scm/php.scm use, and costs nothing since every `function_item` that
; has a body exposes it under the field name `body`.

(function_item name: (identifier) @chunk.name body: (block)) @chunk.function
