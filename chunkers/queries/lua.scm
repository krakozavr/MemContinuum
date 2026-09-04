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
(function_declaration
  name: (dot_index_expression
    table: (identifier) @chunk.qualifier
    field: (identifier) @chunk.name)) @chunk.method

; `function obj:m() ... end` (colon/method syntax) -> `method`, qn `obj.m`
; (the colon becomes a dot in qualified_name -- matches memlint's
; qualified_name.endswith("."+frag) acceptance rule).
(function_declaration
  name: (method_index_expression
    table: (identifier) @chunk.qualifier
    method: (identifier) @chunk.name)) @chunk.method

; `M.f = function() ... end` (assignment form) -- NOT a function_declaration
; at all; the RHS is an anonymous function_definition bound via
; assignment_statement. Genuinely different grammar shape, its own pattern.
(assignment_statement
  (variable_list
    (dot_index_expression
      table: (identifier) @chunk.qualifier
      field: (identifier) @chunk.name))
  (expression_list (function_definition) @chunk.method))
