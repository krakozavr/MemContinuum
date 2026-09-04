"""Generic tree-sitter chunker backend (Anatomy M2b, design B1).

ONE backend implements the registry contract for every tree-sitter
language; per-language behavior comes entirely from LANGUAGE_TABLE data
and a per-language .scm query file under chunkers/queries/ -- never a
per-language Python function. chunkers.get_chunker(lang) for a
backend=="tree-sitter" row calls for_language(lang), which lazily imports
the grammar wheel, builds a tree_sitter.Language, compiles the query, and
returns a TreeSitterChunker exposing the same chunk_file(text, rel_path)
/ declared_symbols(text) surface a native backend module exposes --
callers in memidx.py/memlint.py never know the difference. `typescript`
and `tsx` are two separate rows (ruling 83), so `lang` alone always picks
one grammar -- there is no per-file dialect selection anywhere in this
module, and get_chunker/for_language stay one-argument calls.

This module itself must import with ZERO tree-sitter wheels installed
(binding point 1) -- `import tree_sitter` happens only inside
for_language()/TreeSitterChunker methods, never at this module's top
level, so BackendUnavailable is the only way a missing wheel is ever
observed.

Capture convention every query file follows (see the plan's Architecture
section for the full rationale): @chunk.<kind> names the span+kind,
optional @chunk.name gives the symbol, optional @chunk.qualifier gives an
explicit qualifier prefix (Lua's table/method syntax has no lexical
ancestor to walk), optional @chunk.default marks the export-default
"symbol=qualified_name='default'" case, optional @chunk.doc_anchor names a
DIFFERENT node than @chunk.<kind> to look for a preceding doc comment on
(Lua's assignment form -- `M.f = function() ... end` -- binds @chunk.method
to the anonymous function_definition nested inside expression_list, which
has no sibling of its own; the doc comment sits above the enclosing
assignment_statement instead, so that node is what doc_anchor names; every
other query's @chunk.<kind> node already sits at the right sibling
position and needs no anchor), and a match with neither @chunk.name nor
@chunk.default is dropped (the deferred, unbound `closure` kind).

Dedup (ruling 84): two DIFFERENT query patterns can independently match
the SAME conceptual callable at DIFFERENT spans (an export-wrapper's
outer node vs. the inner definition node it wraps) as well as at the
SAME span with different kinds (a get/set accessor vs. the generic
method pattern). dedup_by_priority handles the same-span case;
dedup_nested handles the different-span containment case, run in that
order in build_chunks.

No parse timeout (revision 4, binding ruling 87 -- revision 3's
signal.alarm mechanism is REMOVED, not merely refined). tree-sitter
==0.26.0's Python Parser exposes no timeout facility to begin with
(binding point 4, verified against the real pinned wheel -- no
timeout_micros anywhere), and a Python-level SIGALRM/setitimer cannot
substitute for one: a registered signal handler only actually runs when
the CPython bytecode-eval loop next checks for a pending signal, which
happens BETWEEN bytecodes, not during a single blocking C-extension call
that never returns to the interpreter. Measured directly against a real
(unmocked) `tree-sitter-javascript` parse of one large (30 MB,
deeply-nested-parentheses, ordinary linear scaling, `has_error == False`
-- nothing adversarial about the grammar's handling of it) file, this
session, in the scratch venv: the plain un-alarmed `parser.parse(data)`
took 8.65s wall time; the SAME file through the (now-removed) alarm-based
`_parse(data, timeout_s=0.5)` raised its timeout exception at 11.08s --
the OS delivers SIGALRM on schedule at 0.5s, but Python never sees it
until `parser.parse()` itself returns control to the interpreter, so the
"timeout" ships an exception eventually, not a wall-clock bound at all.
This is not specific to adversarial input -- any single parse long enough
to matter is a parse this mechanism cannot actually cut short.

Replacement: a per-file BYTE CAP, checked before `parser.parse()` is ever
called, never during it. `DEFAULT_MAX_PARSE_BYTES = 1 MiB`;
`MEMCONTINUUM_MAX_PARSE_BYTES` env var overrides the default; a
LANGUAGE_TABLE row's own `max_bytes` (optional, absent by default) wins
over both. A file over the cap raises `TreeSitterFileTooLarge` with
`reason = "file too large ({size} bytes > {cap})"` BEFORE any byte of it
reaches the parser -- it lands in memidx.py's catch-all Exception branch
(not DETERMINISTIC_FAILURES), so the file is recorded not-indexed with
`attempt_key` set, retried on the next explicit code-reindex like any
other not-indexed row (editing the file down in size and re-running picks
it back up). This bounds worst-case parse time for the OVERWHELMING
majority of real source files (parse time scales with input size for a
non-adversarial file, and 1 MiB of source is already an unusual single
file) without pretending to interrupt an in-flight C call, which nothing
in this pinned tree-sitter version can actually do.

Alternative considered and rejected: a subprocess-per-file timeout (fork
a worker per file, kill it if it overruns). Rejected on cost and fit, not
on correctness -- it WOULD genuinely bound wall-clock time, unlike the
alarm mechanism it would replace. Rejected because: (a) `code-reindex` is
an interactive, foreground command a human runs and can Ctrl-C -- a hang
is visible and killable by the person who started it, not a silent
background failure; (b) hooks never call `code-reindex`/`code-search`
(the hook-isolation invariant this module's own tests enforce, see
TestTreeSitterHookIsolation below), so a slow parse can never block a
hook's own tight latency budget; (c) forking one process per file inside
a loop that today does thousands of files in well under 5 seconds
(Swift's own perf gate) would dominate the reindex's own runtime with
process-creation overhead, for a case -- a single file large/pathological
enough to matter -- the byte cap already screens out before parsing is
even attempted. The byte cap is not a full substitute for a real
wall-clock bound on a file UNDER the cap that still parses slowly for
non-size reasons; that residual risk is accepted, named here rather than
hidden, and left to a future workstream if it proves real in practice
(no report of it exists as of this plan's writing).

No hook may ever reach chunkers.treesitter: hooks/*.sh call memidx.py
only with `for-path`, `reindex` (the DECISION-index reindexer, a
different subcommand from `code-reindex` -- `--auto` is its internal/hook
flag), and `unmapped` -- never `code-reindex`/`code-search`/
`backend-preflight`, the only subcommands that touch a tree-sitter
LANGUAGE_TABLE row (verified this revision: grepped every hooks/*.sh
invocation of memidx.py). Because `chunkers/__init__.py` never imports
this module at its own top level (binding point 1), a fresh `import
memidx` -- which every hook triggers -- never puts `chunkers.treesitter`
in sys.modules either. Task 2's own test suite asserts this directly by
RUNNING the three hook-reachable subcommands in a subprocess and checking
`sys.modules` afterward (revision 4, binding ruling 87 -- NOT a substring
grep of hooks/*.sh, which false-positived against `hooks/newfile-nudge.sh`'s
own human-facing message text that merely mentions "code-search" without
ever invoking it; see TestTreeSitterHookIsolation below).
"""

import hashlib
import importlib
import os

from . import BackendUnavailable, ChunkResult, LANGUAGE_TABLE

QUERY_DIR = os.path.join(os.path.dirname(__file__), "queries")

_KIND_PRIORITY = {"constructor": 0, "accessor": 1, "method": 2, "function": 2}

_INSTANCE_CACHE = {}   # lang -> (fingerprint, TreeSitterChunker)


DEFAULT_MAX_PARSE_BYTES = 1 * 1024 * 1024   # 1 MiB


class TreeSitterFileTooLarge(Exception):
    """Revision 4, binding ruling 87 (replaces the removed
    TreeSitterTimeout -- signal.alarm does not bound wall-clock parse
    time, measured, see the module docstring). A file's encoded byte
    length exceeded max_parse_bytes(row) -- raised BEFORE parser.parse()
    is ever called, never during or after it. Lands in memidx.py's
    catch-all Exception branch (not DETERMINISTIC_FAILURES), so the file
    is recorded not-indexed and retried like any other not-indexed row --
    editing the file down in size and re-running code-reindex picks it
    back up."""


def max_parse_bytes(row):
    """Priority: the row's own `max_bytes` (optional, per-language) >
    MEMCONTINUUM_MAX_PARSE_BYTES (env, parsed as int; ignored if unset or
    unparseable) > DEFAULT_MAX_PARSE_BYTES."""
    if row.get("max_bytes") is not None:
        return row["max_bytes"]
    env = os.environ.get("MEMCONTINUUM_MAX_PARSE_BYTES")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    return DEFAULT_MAX_PARSE_BYTES


def query_fingerprint(row):
    """sha256[:12] of the row's query file's bytes -- reads off disk only,
    NEVER imports the grammar (chunker_version must compute this even when
    the wheel is missing -- M2a binding point 1: a not-indexed row still
    stores its chunker_version). `query_file` is always a plain string
    (ruling 83 removed the only multi-file row, revision 1's dict-shaped
    `typescript`)."""
    with open(os.path.join(QUERY_DIR, row["query_file"]), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:12]


def for_language(lang):
    """Build (or return the cached) TreeSitterChunker for `lang`. Cache key
    is (lang, chunker_version) -- a query-file edit or an impl_version
    bump builds a fresh instance next call instead of serving a stale
    parser/query for the rest of this process's life. ONE argument: every
    tree-sitter LANGUAGE_TABLE row (typescript and tsx included, as two
    separate rows) picks its grammar from `lang` alone."""
    from . import chunker_version   # local import: avoid a cycle at module load
    row = LANGUAGE_TABLE[lang]
    cv = chunker_version(lang)
    cached = _INSTANCE_CACHE.get(lang)
    if cached is not None and cached[0] == cv:
        return cached[1]
    try:
        grammar = importlib.import_module(row["grammar_module"])
        from tree_sitter import Language, Query
        language = Language(getattr(grammar, row["language_fn"])())
        with open(os.path.join(QUERY_DIR, row["query_file"]), "r", encoding="utf-8") as f:
            query_src = f.read()
        query = Query(language, query_src)
    except BackendUnavailable:
        raise
    except Exception as exc:
        raise BackendUnavailable(f"{lang}: {type(exc).__name__}: {exc}") from exc
    inst = TreeSitterChunker(lang, row, language, query)
    _INSTANCE_CACHE[lang] = (cv, inst)
    return inst


def reset_cache():
    """Test helper: drop every cached parser/query instance."""
    _INSTANCE_CACHE.clear()


def dedup_by_priority(entries):
    """entries: dicts with at least 'key' (a hashable span identity) and
    'kind'. When more than one entry shares a key, keep the one whose kind
    sorts lowest in _KIND_PRIORITY (constructor beats accessor beats
    method/function) -- the concrete tie-break for the SAME-span
    collision (a get/set accessor also matching the generic method
    pattern; a constructor also matching the generic method pattern).
    Ties (same priority) keep the first-seen entry."""
    best = {}
    order = []
    for e in entries:
        key = e["key"]
        if key not in best:
            best[key] = e
            order.append(key)
            continue
        if _KIND_PRIORITY.get(e["kind"], 99) < _KIND_PRIORITY.get(best[key]["kind"], 99):
            best[key] = e
    return [best[k] for k in order]


def dedup_nested(entries):
    """Ruling 84: two DIFFERENT-span matches can still capture what a
    human reads as ONE callable -- an export-wrapper's outer node (e.g.
    `export_statement`) vs. the inner definition node it wraps (e.g.
    `function_declaration`), both naming the same symbol AND the same
    kind. Keep the INNERMOST match; drop any match whose span CONTAINS an
    already-kept match's span AND names the SAME SYMBOL AND THE SAME KIND
    (revision 3, binding addition b -- kind is now part of the trigger,
    not just symbol: a wrapper/inner pair is always the same kind in
    every query this plan ships, so requiring both is strictly safer and
    catches nothing extra by accident). A containing span with a
    DIFFERENT symbol -- most importantly a LEGITIMATELY NESTED callable,
    `function outer(){ function inner(){} }` or a method inside a class
    inside a function -- is NEVER touched: containment alone is not the
    trigger, same-symbol-and-kind containment is, so both `outer` and
    `inner` (or the class's own method) survive as their own chunks with
    their own in-file qualification. Runs AFTER dedup_by_priority, which
    already resolved every same-span collision.

    Revision 4, binding ruling 88 fix: the comparison direction MUST be
    "does the CURRENT (larger, since we process ascending-by-size) entry
    CONTAIN an already-kept (smaller-or-equal) entry" -- `s <= ks and ke
    <= en` -- and DROP THE CURRENT ONE when true. A revision-3 version of
    this function had the inequality flipped (`ks <= s and en <= ke`,
    testing whether an already-kept entry contains the current one) --
    under ascending-size processing, every already-kept entry's span is
    always <= the current entry's span by construction, so that direction
    could only ever fire on an exact-span duplicate (already excluded by
    the `!=` guard) and was a structural no-op: verified by running that
    exact revision-3 function body against this file's own first unit
    test and getting `len(deduped) == 2`, not the `1` the test asserts.
    The runnable probe below (Step 1) reproduces both the bug and the fix
    against the real grammar, not just these abstract tuples."""
    ordered = sorted(entries, key=lambda e: (e["key"][1] - e["key"][0]))
    kept = []
    for e in ordered:
        s, en = e["key"]
        contains_a_kept_entry = any(
            s <= ks and ke <= en and (ks, ke) != (s, en)
            and k["symbol"] == e["symbol"] and k["kind"] == e["kind"]
            for k in kept for ks, ke in [k["key"]]
        )
        if contains_a_kept_entry:
            continue   # e is the OUTER (larger) match of an already-kept inner one -- drop it
        kept.append(e)
    return kept


def _line_for_byte(data, byte_offset):
    return data.count(b"\n", 0, byte_offset) + 1


def _overlapping_intervals(node, intervals):
    return [iv for iv in intervals if node.start_byte < iv[1] and iv[0] <= node.end_byte]


def _merge_error_intervals(root):
    """B3: collect and merge every ERROR node's byte span and every
    is_missing token's (zero-width) position into one sorted, merged list
    of (start_byte, end_byte) intervals -- the taint set a capture is
    checked against."""
    raw = []
    def walk(node):
        if node.type == "ERROR":
            raw.append((node.start_byte, node.end_byte))
        if node.is_missing:
            raw.append((node.start_byte, node.start_byte))
        for child in node.children:
            walk(child)
    walk(root)
    if not raw:
        return []
    raw.sort()
    merged = [list(raw[0])]
    for s, e in raw[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [tuple(x) for x in merged]


def _qualify(row, node, symbol, qualifier_text):
    if qualifier_text is not None:
        return f"{qualifier_text}.{symbol}"
    parts = []
    containers = row.get("containers", {})
    p = node.parent
    while p is not None:
        field = containers.get(p.type)
        if field == "SELF_TYPE":
            t = p.child_by_field_name("type")
            if t is not None:
                parts.append(p.text[t.start_byte - p.start_byte: t.end_byte - p.start_byte].decode("utf-8", "replace"))
        elif field:
            n = p.child_by_field_name(field)
            if n is not None:
                parts.append(p.text[n.start_byte - p.start_byte: n.end_byte - p.start_byte].decode("utf-8", "replace"))
        p = p.parent
    parts.reverse()
    parts.append(symbol)
    return ".".join(parts)


def _kind_for(row, kind, node):
    if kind == "function" and row.get("method_if_ancestor_in"):
        p = node.parent
        while p is not None:
            if p.type in row["method_if_ancestor_in"]:
                return "method"
            p = p.parent
    return kind


def _render_signature(data, node):
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    text = data[node.start_byte:end].decode("utf-8", "replace")
    return " ".join(text.split())


def _doc_for(row, data, node):
    """Task 7 fix round 1, finding 2: marker-stripping is now symmetric.
    Before this fix, a line was only ever stripped on its LEFT
    (`lstrip("/*# -")`) -- correct for a multi-line block comment (`/**` on
    its own opening line strips to "", `* text` on the next line strips its
    leading `*` and returns "text" before the closing `*/` line is ever
    reached) but wrong for a SINGLE-LINE block comment, where the opening
    and closing markers share one line: `/* Adds two numbers. */` used to
    yield `"Adds two numbers. */"`, the trailing delimiter surviving
    untouched. Fixed by stripping an exact trailing `*/` (plus the
    whitespace before it) off each line BEFORE the existing left-strip
    runs, rather than a blanket `rstrip` of `*/` characters -- a blanket
    rstrip would also eat a trailing `/` from an ordinary line comment that
    happens to end in one (e.g. a URL), which this line-by-line, suffix-only
    check never touches: a `//`/`///`/`#` line comment never ends with the
    literal two-character sequence "*/", so this new step is a no-op for
    every non-block-comment language row, matching the fix's "no other
    language regresses" requirement."""
    prev = node.prev_sibling
    doc_types = row.get("doc_comment_types", ("comment",))
    if prev is None or prev.type not in doc_types:
        return ""
    text = data[prev.start_byte:prev.end_byte].decode("utf-8", "replace")
    for line in text.splitlines():
        line = line.strip()
        if line.endswith("*/"):
            line = line[:-2].rstrip()
        stripped = line.lstrip("/*# -").strip()
        if stripped:
            return stripped
    return ""


def build_chunks(lang, row, data, root, matches):
    """The shared B3 interval-taint + B2 kind/qualification engine every
    tree-sitter language uses, driven entirely by the capture convention
    (module docstring) and the row's `containers`/`method_if_ancestor_in`
    data -- no per-language Python branch."""
    error_intervals = _merge_error_intervals(root)
    entries = []
    for _pattern_idx, caps in matches:
        kind_node = None
        kind = None
        for cname, nodes in caps.items():
            if cname.startswith("chunk.") and cname not in (
                "chunk.name", "chunk.qualifier", "chunk.default", "chunk.doc_anchor",
            ):
                kind_node = nodes[0]
                kind = cname.split(".", 1)[1]
        if kind_node is None:
            continue
        name_node = caps.get("chunk.name", [None])[0]
        qual_node = caps.get("chunk.qualifier", [None])[0]
        doc_anchor_node = caps.get("chunk.doc_anchor", [None])[0]
        is_default = "chunk.default" in caps
        if name_node is None and not is_default:
            continue   # unbound callable -- deferred `closure` kind
        symbol = "default" if name_node is None else data[name_node.start_byte:name_node.end_byte].decode("utf-8", "replace")
        qualifier_text = None
        if qual_node is not None:
            qualifier_text = data[qual_node.start_byte:qual_node.end_byte].decode("utf-8", "replace")
        qualified_name = "default" if name_node is None else _qualify(row, kind_node, symbol, qualifier_text)
        resolved_kind = _kind_for(row, kind, kind_node)
        entries.append({
            "key": (kind_node.start_byte, kind_node.end_byte),
            "kind": resolved_kind,
            "symbol": symbol,
            "qualified_name": qualified_name,
            "node": kind_node,
            "doc_node": doc_anchor_node if doc_anchor_node is not None else kind_node,
        })
    entries = dedup_by_priority(entries)   # same-span kind collisions (ruling 84's sibling case)
    entries = dedup_nested(entries)        # different-span, same-symbol containment (ruling 84)

    chunks = []
    gaps = []
    seen_gap_spans = set()
    consumed_intervals = set()
    for e in entries:
        node = e["node"]
        overlaps = _overlapping_intervals(node, error_intervals)
        tainted = bool(overlaps)
        if not tainted:
            anc = node.parent
            while anc is not None:
                if anc.type == "ERROR":
                    tainted = True
                    break
                anc = anc.parent
        if tainted:
            consumed_intervals.update(overlaps)
            span = (node.start_point[0] + 1, node.end_point[0] + 1)
            if span not in seen_gap_spans:
                seen_gap_spans.add(span)
                gaps.append((span[0], span[1], "parse-error"))
            continue
        chunks.append({
            "kind": e["kind"], "symbol": e["symbol"], "qualified_name": e["qualified_name"],
            "signature": _render_signature(data, node), "doc": _doc_for(row, data, e["doc_node"]),
            "start_line": node.start_point[0] + 1, "end_line": node.end_point[0] + 1,
        })

    # B3: "Top-level ERROR spans outside any capture are gaps too" -- an
    # error interval that overlapped no capture at all (garbage between
    # two clean functions, with nothing query-shaped nearby) still needs a
    # gap; derive its line range from byte offsets since no capture node
    # exists to read start_point/end_point from. Replaces revision 1's
    # dead `pass`-loop, which computed nothing for exactly this case.
    for iv in error_intervals:
        if iv in consumed_intervals:
            continue
        start_line = _line_for_byte(data, iv[0])
        end_line = _line_for_byte(data, max(iv[0], iv[1] - 1))
        span = (start_line, end_line)
        if span not in seen_gap_spans:
            seen_gap_spans.add(span)
            gaps.append((span[0], span[1], "parse-error"))

    if root.has_error and not chunks:
        line_count = data.count(b"\n") + 1
        return [], [(1, line_count, "parse-error")], "failed"
    chunks.sort(key=lambda c: c["start_line"])
    gaps.sort(key=lambda g: g[0])
    status = "partial" if gaps else "ok"
    return chunks, gaps, status


class TreeSitterChunker:
    def __init__(self, lang, row, language, query):
        self.lang = lang
        self.row = row
        self._language = language
        self._query = query

    def _parser(self):
        from tree_sitter import Parser
        return Parser(self._language)

    def chunk_file(self, text, rel_path):
        # Revision 4, binding ruling 87: the byte cap is the ENTIRE timeout
        # story now -- checked before a single byte reaches the parser,
        # never during or after. No alarm, no thread guard, nothing to
        # arm/disarm/restore.
        from tree_sitter import QueryCursor
        data = text.encode("utf-8")
        cap = max_parse_bytes(self.row)
        if len(data) > cap:
            raise TreeSitterFileTooLarge(f"file too large ({len(data)} bytes > {cap})")
        parser = self._parser()
        tree = parser.parse(data)
        root = tree.root_node
        cursor = QueryCursor(self._query)
        matches = cursor.matches(root)
        chunks, gaps, status = build_chunks(self.lang, self.row, data, root, matches)
        for c in chunks:
            c["lang"] = self.lang
        return ChunkResult(chunks, gaps, status)

    def declared_symbols(self, text):
        try:
            result = self.chunk_file(text, "<memlint>")
        except Exception:
            return []
        seen = set()
        out = []
        for c in result.chunks:
            pair = (c["symbol"], c["qualified_name"])
            if pair not in seen:
                seen.add(pair)
                out.append(pair)
        return out
