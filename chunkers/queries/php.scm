; chunkers/queries/php.scm  (Task 6: full B2 query, replaces Task 2's smoke content;
; fix round 1 finding 2: body: (compound_statement) added to both method patterns,
; matching java.scm's own body: (block) constraint -- a bodyless method_declaration
; (an interface method signature, an abstract method) is never chunked)

(function_definition name: (name) @chunk.name) @chunk.function

(method_declaration
  name: (name) @chunk.name
  body: (compound_statement)
  (#eq? @chunk.name "__construct")) @chunk.constructor

(method_declaration
  name: (name) @chunk.name
  body: (compound_statement)
  (#not-eq? @chunk.name "__construct")) @chunk.method
