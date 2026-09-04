; chunkers/queries/php.scm  (Task 6: full B2 query, replaces Task 2's smoke content)

(function_definition name: (name) @chunk.name) @chunk.function

(method_declaration
  name: (name) @chunk.name
  (#eq? @chunk.name "__construct")) @chunk.constructor

(method_declaration
  name: (name) @chunk.name
  (#not-eq? @chunk.name "__construct")) @chunk.method
