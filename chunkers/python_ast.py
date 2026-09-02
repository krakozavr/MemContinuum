"""chunkers/python_ast.py -- stdlib `ast`-based Python chunker backend.

Task 4 of the Anatomy M1 milestone. Python gets its own native backend
(never tree-sitter, per the design's backend-exclusivity ruling) because the
stdlib parser IS the reference grammar for this language -- zero deps, exact
docstrings for free, and it means the engine can index its own source
(dogfood + fail-open floor: this whole codebase is Python).

Model: one hand-written recursive visitor (`_walk_defs`, deliberately NOT
`ast.walk` -- Step 3 of the brief requires a visitor that CARRIES a
qualification stack as it descends, which `ast.walk`'s flat BFS/DFS order
can't give you) collects every `ClassDef` and `FunctionDef`/`AsyncFunctionDef`
in the tree, however deeply nested (inside `if`/`try`/`for`/`with` bodies,
not just directly in a class or function body), tagging each with its
dotted qualification path and whether its immediate parent was a class.
`chunk_file` and `declared_symbols` both build on that one walk rather than
each re-deriving it (one traversal, two views).

Mapping rules (spec S2, verbatim from the Task 4 brief):
- module-level (or nested-under-a-function) `def`/`async def` -> "function"
- `def`/`async def` whose immediate parent is a `class` body -> "method",
  except `__init__` -> "constructor" (checked first: an `__init__` is a
  constructor even in the vanishingly unlikely case it also carries a
  property-shaped decorator -- the name is definitive) and
  `@property`/`@x.setter` decorated -> "accessor"
- a nested `def` (immediate parent is a function, not a class) is ALWAYS
  "function", even if that enclosing function is itself a method -- kind is
  decided purely by the immediate parent, not by any ancestor further up.
- `ClassDef` is NEVER itself a chunk -- it only contributes its name to the
  qualification path of whatever it contains (`Outer.Inner.method`),
  matching the Swift container doctrine (chunkers/swift.py's `types` stack).

Signature rendering is a manual, `ast.unparse`-free walk of the `arguments`
node (posonlyargs/args/vararg/kwonlyargs/kwarg/defaults) plus a small
recursive expression renderer for annotations and default values --
deliberately not `ast.unparse` anywhere (not even on sub-expressions), so
there is exactly one literal reading of "ast.unparse-free" to worry about.
The renderer covers the node shapes real signatures use (Name, Attribute,
Constant, Subscript, Tuple/List/Dict/Set, `X | Y` unions, unary minus, and
simple calls); anything else falls back to an inert placeholder rather than
raising -- a signature is a search/display aid, never a parse target, so an
imprecise rendering for an exotic default is an acceptable, visible
degradation (never a crash).
"""
from __future__ import annotations

import ast

from chunkers import ChunkResult

# ---------------------------------------------------------------------------
# expression rendering (annotations, defaults) -- bounded, ast.unparse-free
# ---------------------------------------------------------------------------

_UNARY_OP_TEXT = {
    ast.USub: "-",
    ast.UAdd: "+",
    ast.Not: "not ",
    ast.Invert: "~",
}


def _render_expr(node) -> str:
    """A small recursive renderer for the expression shapes that show up in
    real annotations and default values. Never raises: an unrecognized node
    shape renders as `<...>` rather than crashing signature rendering (and,
    transitively, chunk_file) over a decorative field."""
    if node is None:
        return ""
    if isinstance(node, ast.Constant):
        if node.value is Ellipsis:
            return "..."
        return repr(node.value)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_render_expr(node.value)}.{node.attr}"
    if isinstance(node, ast.Subscript):
        return f"{_render_expr(node.value)}[{_render_expr(node.slice)}]"
    if isinstance(node, ast.Tuple):
        inner = ", ".join(_render_expr(e) for e in node.elts)
        if len(node.elts) == 1:
            inner += ","
        return inner
    if isinstance(node, ast.List):
        return "[" + ", ".join(_render_expr(e) for e in node.elts) + "]"
    if isinstance(node, ast.Set):
        return "{" + ", ".join(_render_expr(e) for e in node.elts) + "}"
    if isinstance(node, ast.Dict):
        pairs = []
        for k, v in zip(node.keys, node.values):
            if k is None:  # **spread inside a dict literal
                pairs.append(f"**{_render_expr(v)}")
            else:
                pairs.append(f"{_render_expr(k)}: {_render_expr(v)}")
        return "{" + ", ".join(pairs) + "}"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return f"{_render_expr(node.left)} | {_render_expr(node.right)}"
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OP_TEXT:
        return f"{_UNARY_OP_TEXT[type(node.op)]}{_render_expr(node.operand)}"
    if isinstance(node, ast.Starred):
        return f"*{_render_expr(node.value)}"
    if isinstance(node, ast.Call):
        args = [_render_expr(a) for a in node.args]
        args += [
            f"{kw.arg}={_render_expr(kw.value)}" if kw.arg else f"**{_render_expr(kw.value)}"
            for kw in node.keywords
        ]
        return f"{_render_expr(node.func)}({', '.join(args)})"
    return "<...>"


def _render_arg(arg: ast.arg, default) -> str:
    text = arg.arg
    if arg.annotation is not None:
        text += f": {_render_expr(arg.annotation)}"
        if default is not None:
            text += f" = {_render_expr(default)}"
    elif default is not None:
        text += f"={_render_expr(default)}"
    return text


def _render_arguments(node: ast.arguments) -> str:
    """Manual render of the `arguments` node -- posonlyargs/args share one
    right-aligned defaults list (per Python's own grammar: `defaults`
    applies to the tail of posonlyargs+args combined), kwonlyargs each pair
    with their own `kw_defaults` slot (`None` there means no default,
    distinct from a `None` default -- ast.arguments' own convention)."""
    parts: list[str] = []
    posonly = list(node.posonlyargs)
    plain = list(node.args)
    combined = posonly + plain
    n_defaults = len(node.defaults)
    # defaults right-align against `combined`; the head has no default.
    padded_defaults = [None] * (len(combined) - n_defaults) + list(node.defaults)
    for arg, default in zip(combined, padded_defaults):
        parts.append(_render_arg(arg, default))
    if posonly:
        parts.insert(len(posonly), "/")
    if node.vararg is not None:
        parts.append(f"*{node.vararg.arg}" + (
            f": {_render_expr(node.vararg.annotation)}" if node.vararg.annotation else ""
        ))
    elif node.kwonlyargs:
        parts.append("*")
    for arg, default in zip(node.kwonlyargs, node.kw_defaults):
        parts.append(_render_arg(arg, default))
    if node.kwarg is not None:
        parts.append(f"**{node.kwarg.arg}" + (
            f": {_render_expr(node.kwarg.annotation)}" if node.kwarg.annotation else ""
        ))
    return ", ".join(parts)


def _render_signature(node) -> str:
    keyword = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    args_text = _render_arguments(node.args)
    sig = f"{keyword} {node.name}({args_text})"
    if node.returns is not None:
        sig += f" -> {_render_expr(node.returns)}"
    return sig


# ---------------------------------------------------------------------------
# the recursive visitor (ast.walk-free, carries the qualification stack)
# ---------------------------------------------------------------------------

_DEF_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _walk_defs(tree: ast.Module) -> list[dict]:
    """One recursive descent over the whole tree (not `ast.walk`), carrying
    a dotted-name stack of every class/function currently being descended
    into. Returns raw records in source order: {node, stack, parent_is_class}
    for every ClassDef and FunctionDef/AsyncFunctionDef found at any depth
    -- `chunk_file` and `declared_symbols` each project this shared list
    differently rather than re-walking the tree twice."""
    out: list[dict] = []

    def visit(node, stack: list, parent_is_class: bool):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                out.append({"node": child, "stack": list(stack), "is_class": True,
                             "parent_is_class": parent_is_class})
                stack.append(child.name)
                visit(child, stack, True)
                stack.pop()
            elif isinstance(child, _DEF_TYPES):
                out.append({"node": child, "stack": list(stack), "is_class": False,
                             "parent_is_class": parent_is_class})
                stack.append(child.name)
                visit(child, stack, False)
                stack.pop()
            else:
                visit(child, stack, parent_is_class)

    visit(tree, [], False)
    return out


def _is_accessor(node) -> bool:
    """@property (a bare `Name` decorator) or @x.setter (an `Attribute`
    decorator whose `.attr` is "setter") -- verbatim per the brief; NOT
    @x.deleter/@x.getter, which stay plain methods (nothing in the brief
    calls for them)."""
    for dec in node.decorator_list:
        if isinstance(dec, ast.Name) and dec.id == "property":
            return True
        if isinstance(dec, ast.Attribute) and dec.attr == "setter":
            return True
    return False


def _kind_for(node, parent_is_class: bool) -> str:
    if not parent_is_class:
        return "function"
    if node.name == "__init__":  # checked first: definitive regardless of decorators
        return "constructor"
    if _is_accessor(node):
        return "accessor"
    return "method"


def _qualified(stack: list, name: str) -> str:
    return ".".join(stack + [name]) if stack else name


def _start_line(node) -> int:
    if node.decorator_list:
        return node.decorator_list[0].lineno
    return node.lineno


def _doc_first_line(node) -> str:
    doc = ast.get_docstring(node)
    if not doc:
        return ""
    return doc.splitlines()[0]


def chunk_source(text: str):
    """Chunk one Python file's source text. Returns (chunks, gaps, status) --
    chunks is a list of dicts (kind, symbol, qualified_name, signature, doc,
    start_line, end_line, all 1-based) in source order; gaps is `[]` unless
    parsing fails outright, in which case chunks is `[]` and gaps names the
    whole file as one `"syntax-error"` region (`ast.parse` is whole-file --
    unlike Swift's brace walker there is no partial/resync recovery to
    offer; a file that doesn't parse contributes nothing, not a guess)."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        # B6 (Anatomy M1 fix wave): the comment that used to sit here said
        # ast.parse raises ValueError for source containing NUL bytes.
        # Verified false on this project's interpreter (CPython 3.12.3):
        # `ast.parse("a = 1\x00b = 2")` raises SyntaxError ("source code
        # string cannot contain null bytes"). ValueError is kept in the
        # tuple anyway -- it is what older CPython raised for exactly this
        # input, and the brief's "fail-open, no exception escapes" contract
        # is worth more than a tight except clause -- but the comment must
        # not claim behavior the runtime does not have.
        line_count = text.count("\n") + 1
        return [], [(1, line_count, "syntax-error")], "failed"

    records = _walk_defs(tree)
    chunks = []
    for rec in records:
        if rec["is_class"]:
            continue
        node = rec["node"]
        chunks.append({
            "kind": _kind_for(node, rec["parent_is_class"]),
            "symbol": node.name,
            "qualified_name": _qualified(rec["stack"], node.name),
            "signature": _render_signature(node),
            "doc": _doc_first_line(node),
            "start_line": _start_line(node),
            "end_line": node.end_lineno,
        })
    chunks.sort(key=lambda c: c["start_line"])
    return chunks, [], "ok"


def chunk_file(text: str, rel_path: str) -> ChunkResult:
    """Registry-contract wrapper (chunkers.__init__'s ChunkResult, spec S1a):
    every chunk dict gains `"lang": "python"`. `rel_path` is unused by the
    parser itself; it is part of the registry contract (chunkers.get_chunker
    callers pass it uniformly across backends)."""
    chunks, gaps, status = chunk_source(text)
    for chunk in chunks:
        chunk["lang"] = "python"
    return ChunkResult(chunks, gaps, status)


def declared_symbols(text: str) -> list:
    """All symbol names memlint's #symbol vocabulary check should recognize
    as declared in `text` -- every chunkable def's bare name PLUS each
    class's own name (mirroring chunkers.swift.declared_symbols, which
    includes container type names alongside member names: a #symbol
    fragment may name the class itself, not just a member). Part of the
    registry contract alongside `chunk_file`: memidx.fragment_declared_in_text
    calls it generically, by language, with no per-language branch.
    Returns `(symbol, qualified_name)` pairs in source order,
    de-duplicated (a `@property`/`@x.setter` pair share one qualified_name,
    e.g. `Gadget.value` from two separate defs -- listing it once is enough
    for a vocabulary check). On a syntax error, returns `[]` -- fail-open,
    matching chunk_file's `status="failed"` case (no exception escapes)."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []

    records = _walk_defs(tree)
    seen: set = set()
    out: list = []
    for rec in records:
        node = rec["node"]
        pair = (node.name, _qualified(rec["stack"], node.name))
        if pair in seen:
            continue
        seen.add(pair)
        out.append(pair)
    return out
