; chunkers/queries/typescript.scm  (Task 4: full B2 query, replaces Task 2's smoke content)
; Identical to javascript.scm by design -- TS/TSX add types and JSX, neither
; changes how a function/method/class is shaped. Keep these two files in
; sync; a query change belongs in both unless it is genuinely JS-only.

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
;
; Every KIND a literal can hold needs its own bound pattern, not the plain
; method alone. dedup_by_priority resolves a same-span collision by kind
; first (constructor, then accessor, then method) and only then by which
; reading carried a qualifier, so a bound `get open(){}` matched by the
; generic accessor pattern and by a bound METHOD pattern would keep the
; accessor -- the better kind -- and lose the binding with it. Bound and
; bare readings must therefore meet at the SAME kind, where the qualifier
; decides.
(
  [
    (variable_declarator
      name: (identifier) @chunk.qualifier
      value: (object (method_definition name: (property_identifier) @chunk.name) @chunk.method))
    (assignment_expression
      left: (identifier) @chunk.qualifier
      right: (object (method_definition name: (property_identifier) @chunk.name) @chunk.method))
  ]
  (#not-eq? @chunk.name "constructor"))

(
  [
    (variable_declarator
      name: (identifier) @chunk.qualifier
      value: (object (method_definition name: (property_identifier) @chunk.name) @chunk.constructor))
    (assignment_expression
      left: (identifier) @chunk.qualifier
      right: (object (method_definition name: (property_identifier) @chunk.name) @chunk.constructor))
  ]
  (#eq? @chunk.name "constructor"))

[
  (variable_declarator
    name: (identifier) @chunk.qualifier
    value: (object
      (method_definition ["get" "set"] name: (property_identifier) @chunk.name) @chunk.accessor))
  (assignment_expression
    left: (identifier) @chunk.qualifier
    right: (object
      (method_definition ["get" "set"] name: (property_identifier) @chunk.name) @chunk.accessor))
]

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
; was captured by nothing at all. The `#` is part of the stored symbol:
; `#m` and `m` are two different members of the same class, and a symbol
; that dropped the marker would make them one name at two spans. A record
; references it as `widget.js##m` -- the path/fragment split takes the FIRST
; `#`, so the fragment keeps its own.
(method_definition
  name: (private_property_identifier) @chunk.name) @chunk.method
