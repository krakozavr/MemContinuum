; chunkers/queries/typescript.scm  (Task 4: full B2 query, replaces Task 2's smoke content)
; Identical to javascript.scm by design -- TS/TSX add types and JSX, neither
; changes how a function/method/class is shaped. Keep these two files in
; sync; a query change belongs in both unless it is genuinely JS-only.

(function_declaration name: (identifier) @chunk.name) @chunk.function

(variable_declarator
  name: (identifier) @chunk.name
  value: [(arrow_function) (function_expression)] @chunk.function)

(variable_declarator
  name: (identifier) @chunk.name
  value: (call_expression
    function: (identifier) @_wrapper
    arguments: (arguments [(arrow_function) (function_expression)] @chunk.function))
  (#any-of? @_wrapper "memo" "forwardRef"))

(export_statement
  "default"
  value: (call_expression
    function: (identifier) @_wrapper
    arguments: (arguments [(arrow_function) (function_expression)] @chunk.function))
  (#any-of? @_wrapper "memo" "forwardRef")) @chunk.default

(export_statement
  "default"
  declaration: (function_declaration name: (identifier) @chunk.name) @chunk.function)

(export_statement
  "default"
  value: [(arrow_function) (function_expression)] @chunk.function) @chunk.default

(method_definition
  name: (property_identifier) @chunk.name
  (#eq? @chunk.name "constructor")) @chunk.constructor

(method_definition
  "get"
  name: (property_identifier) @chunk.name) @chunk.accessor

(method_definition
  "set"
  name: (property_identifier) @chunk.name) @chunk.accessor

(method_definition
  name: (property_identifier) @chunk.name
  (#not-eq? @chunk.name "constructor")) @chunk.method
