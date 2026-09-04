; chunkers/queries/lua.scm  (Task 8: full query, qualifier-based naming)
;
; Lua has no lexical container tree-sitter can walk to for `M.g`/`obj:m` --
; `M`/`obj` are just table-valued variables, not class-like scopes -- so
; this is the one language whose query uses @chunk.qualifier (an explicit
; capture on the table/object identifier) instead of the `containers`
; ancestor walk every other language row relies on (see treesitter.py's
; _qualify).

; `function f() ... end` and `local function f() ... end` are the SAME
; grammar node type, function_declaration -- the `local` keyword does not
; change the shape (verified against the real grammar). Both -> `function`,
; symbol/qn `f`.
(function_declaration name: (identifier) @chunk.name) @chunk.function

; `function M.g() ... end` (dot syntax) -> `method`, qn `M.g` via
; @chunk.qualifier = `M`.
;
; The table is captured as a wildcard, not as an identifier: a multi-segment
; name (`function App.Services.load()`, the shape of every module of any
; size) nests one dot_index_expression inside another, so `App.Services` is
; not an identifier and an identifier-only capture matched nothing at all --
; the function was dropped with status=ok. Lua's own grammar for a function
; name is `Name {'.' Name} [':' Name]`, so the wildcard can only ever be a
; dotted path, and its source text IS the qualifier: `App.Services.load`.
(function_declaration
  name: (dot_index_expression
    table: (_) @chunk.qualifier
    field: (identifier) @chunk.name)) @chunk.method

; `function obj:m() ... end` (colon/method syntax) -> `method`, qn `obj.m`
; (the colon becomes a dot in qualified_name -- matches memlint's
; qualified_name.endswith("."+frag) acceptance rule). Same wildcard table as
; the dot form: `function App.Services:load()` is the common receiver shape.
(function_declaration
  name: (method_index_expression
    table: (_) @chunk.qualifier
    method: (identifier) @chunk.name)) @chunk.method

; `M.f = function() ... end` (assignment form) -- NOT a function_declaration
; at all; the RHS is an anonymous function_definition bound via
; assignment_statement. Genuinely different grammar shape, its own pattern.
; @chunk.doc_anchor marks the assignment_statement itself, not the inner
; function_definition @chunk.method binds: a `--` doc comment sits directly
; above the ASSIGNMENT (the whole statement, one block-level sibling), never
; above the anonymous function_definition nested inside expression_list,
; which has no earlier sibling of its own to find it on (Task 8 review
; finding 2 -- verified against the real grammar this task).
(assignment_statement
  (variable_list
    (dot_index_expression
      table: (_) @chunk.qualifier
      field: (identifier) @chunk.name))
  (expression_list (function_definition) @chunk.method)) @chunk.doc_anchor
