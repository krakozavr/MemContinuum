; chunkers/queries/javascript.scm  (Task 3: full B2 query, replaces the Task 2 smoke content)

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
