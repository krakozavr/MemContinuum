; chunkers/queries/javascript.scm  (Task 3: full B2 query, replaces the Task 2 smoke content)

; A generator is its own node type in this grammar --
; generator_function_declaration for `function* g(){}` (and for `async
; function* g(){}`, which differs only by an `async` token), never
; function_declaration. It reads as an ordinary named function to a person,
; so it is one here too: kind `function`, symbol its own name.
[
  (function_declaration name: (identifier) @chunk.name)
  (generator_function_declaration name: (identifier) @chunk.name)
] @chunk.function

(variable_declarator
  name: (identifier) @chunk.name
  value: [(arrow_function) (function_expression) (generator_function)] @chunk.function)

; `memo(...)` / `forwardRef(...)` and the `React.memo(...)` /
; `React.forwardRef(...)` spelling of the same two wrappers: a bare call is
; an identifier callee, a namespaced one a member_expression whose PROPERTY
; carries the name, and both bind the component to the declarator's name.
(variable_declarator
  name: (identifier) @chunk.name
  value: (call_expression
    function: [
      (identifier) @_wrapper
      (member_expression property: (property_identifier) @_wrapper)
    ]
    arguments: (arguments [(arrow_function) (function_expression)] @chunk.function))
  (#any-of? @_wrapper "memo" "forwardRef"))

(export_statement
  "default"
  value: (call_expression
    function: [
      (identifier) @_wrapper
      (member_expression property: (property_identifier) @_wrapper)
    ]
    arguments: (arguments [(arrow_function) (function_expression)] @chunk.function))
  (#any-of? @_wrapper "memo" "forwardRef")) @chunk.default

(export_statement
  "default"
  declaration: [
    (function_declaration name: (identifier) @chunk.name)
    (generator_function_declaration name: (identifier) @chunk.name)
  ] @chunk.function)

(export_statement
  "default"
  value: [(arrow_function) (function_expression) (generator_function)] @chunk.function) @chunk.default

; A method of an object LITERAL is the same method_definition node a class
; body holds, but an object literal is no qualification container -- nothing
; in the ancestor walk names it -- so two distinct objects in one file each
; declared an `open` under the bare name `open`. When the literal is the
; value of a binding, that binding IS the name a reader uses (`api.get`), and
; @chunk.qualifier says so explicitly. A literal with no binding to name it
; (an argument, a nested value) keeps the unqualified method: less precise,
; never dropped.
(variable_declarator
  name: (identifier) @chunk.qualifier
  value: (object
    (method_definition name: (property_identifier) @chunk.name) @chunk.method))

(assignment_expression
  left: (identifier) @chunk.qualifier
  right: (object
    (method_definition name: (property_identifier) @chunk.name) @chunk.method))

(method_definition
  name: (property_identifier) @chunk.name
  (#eq? @chunk.name "constructor")) @chunk.constructor

(method_definition
  "get"
  name: [(property_identifier) (private_property_identifier)] @chunk.name) @chunk.accessor

(method_definition
  "set"
  name: [(property_identifier) (private_property_identifier)] @chunk.name) @chunk.accessor

(method_definition
  name: (property_identifier) @chunk.name
  (#not-eq? @chunk.name "constructor")) @chunk.method

; A private member's name is a private_property_identifier, a different node
; type from the property_identifier every pattern above matches, so `#m(){}`
; was captured by nothing at all. The stored symbol drops the leading `#`
; (chunkers/treesitter.py's _symbol_text) -- a record references a symbol as
; `path#symbol`, and `widget.js##m` is not a reference anyone writes.
(method_definition
  name: (private_property_identifier) @chunk.name) @chunk.method
