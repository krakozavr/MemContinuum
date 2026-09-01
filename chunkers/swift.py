"""chunkers/swift.py -- lexer-aware brace-walker Swift chunker backend.

Moved from memidx.py (Task 2 of the Anatomy M1 milestone) byte-for-byte:
same regexes, same logic, proven behavior-identical against
tests/goldens/swift_chunks_pre_extraction.json (a chunks-table dump
captured from the pre-move code). memidx.py re-exports the names its own
code and memlint.py still need (`chunk_source`,
`_build_mask_and_match_dict`, `_KEYWORD_RE`, `_container_type_name`,
`_extract_decls`) for back-compat.
"""
from __future__ import annotations

import re

from chunkers import ChunkResult

# ---------------------------------------------------------------------------
# chunker: lexer-aware brace walker
# ---------------------------------------------------------------------------


def _find_prefix_hashes(text: str, i: int) -> int:
    n = 0
    while i + n < len(text) and text[i + n] == "#":
        n += 1
    return n


def _scan_region(text: str, start: int):
    """Scan text[start:] as Swift source in a FRESH top-level brace context
    (depth 0). Writes accumulate into local_writes/local_match, but are
    only TRUSTWORTHY up through `checkpoint_idx` -- the position just past
    the most recent point brace depth returned to 0 (a fully-closed
    top-level construct can never be retroactively corrupted by whatever
    comes after it, so each such point is a safe commit boundary). The
    caller (chunk_source) discards anything at/after checkpoint_idx when a
    desync is reported, keeping everything before it -- this is what lets
    "func good() {...}" ahead of a later-in-the-same-file fault still get
    indexed, rather than gapping the whole region back to its start.

    An "extra open" imbalance (e.g. one #if branch missing a closing
    brace) doesn't fail until EOF (or may, pathologically, never fail at
    all if a later stray '}' happens to numerically rebalance it --
    brace-counting alone cannot distinguish that from genuinely correct
    code; a known limitation, not fixable without semantic analysis). An
    "extra close" (a stray '}') fails immediately, so checkpoint_idx will
    usually sit right at the fault in that case.

    Handles // and /* nested */ comments, "simple" strings, triple-quoted
    strings, #"raw"# strings (interpolation not supported inside raw
    strings -- inert content, matching the advisor's "gap+warn is
    acceptable there" guidance), and \\(...\\) string interpolation
    (including one containing a closure literal) via a small mode stack so
    nested `(`/`{` inside an interpolation still balance correctly.

    Returns (desync_idx_or_None, local_writes, local_match, checkpoint_idx).
    """
    n = len(text)
    brace_stack: list[int] = []
    mode_stack: list[list] = [["CODE"]]
    local_writes: dict = {}
    local_match: dict = {}
    checkpoint_idx = start
    i = start
    while i < n:
        frame = mode_stack[-1]
        mode = frame[0]
        c = text[i]

        if mode in ("CODE", "ICODE"):
            if c == "/" and i + 1 < n and text[i + 1] == "/":
                j = i
                while j < n and text[j] != "\n":
                    j += 1
                i = j
                continue
            if c == "/" and i + 1 < n and text[i + 1] == "*":
                depth_c = 1
                j = i + 2
                while j < n and depth_c > 0:
                    if text[j : j + 2] == "/*":
                        depth_c += 1
                        j += 2
                        continue
                    if text[j : j + 2] == "*/":
                        depth_c -= 1
                        j += 2
                        continue
                    j += 1
                i = j
                continue
            if text[i : i + 3] == '"""':
                mode_stack.append(["STR_TRIPLE"])
                i += 3
                continue
            if c == "#":
                h = _find_prefix_hashes(text, i)
                if i + h < n and text[i + h] == '"':
                    mode_stack.append(["STR_RAW", h])
                    i += h + 1
                    continue
                local_writes[i] = c
                i += 1
                continue
            if c == '"':
                mode_stack.append(["STR_SIMPLE"])
                i += 1
                continue
            if c == "{":
                brace_stack.append(i)
                local_writes[i] = c
                i += 1
                continue
            if c == "}":
                if not brace_stack:
                    return i, local_writes, local_match, checkpoint_idx
                open_idx = brace_stack.pop()
                local_match[open_idx] = i
                local_writes[i] = c
                i += 1
                if not brace_stack:
                    checkpoint_idx = i
                continue
            if mode == "ICODE" and c == "(":
                frame[1] += 1
                local_writes[i] = c
                i += 1
                continue
            if mode == "ICODE" and c == ")":
                frame[1] -= 1
                local_writes[i] = c
                i += 1
                if frame[1] == 0:
                    mode_stack.pop()
                continue
            if c != "\n":
                local_writes[i] = c
            i += 1
            continue

        if mode == "STR_SIMPLE":
            if c == "\\" and i + 1 < n and text[i + 1] == "(":
                mode_stack.append(["ICODE", 1])
                i += 2
                continue
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                mode_stack.pop()
                i += 1
                continue
            i += 1
            continue

        if mode == "STR_TRIPLE":
            if c == "\\" and i + 1 < n and text[i + 1] == "(":
                mode_stack.append(["ICODE", 1])
                i += 2
                continue
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if text[i : i + 3] == '"""':
                mode_stack.pop()
                i += 3
                continue
            i += 1
            continue

        if mode == "STR_RAW":
            h = frame[1]
            if c == '"' and text[i + 1 : i + 1 + h] == "#" * h:
                mode_stack.pop()
                i += h + 1
                continue
            i += 1
            continue

        i += 1

    if brace_stack:
        return brace_stack[0], local_writes, local_match, checkpoint_idx
    return None, local_writes, local_match, n


# Shared modifier vocabulary (finding 3): used both by _RESYNC_RE's gap-resync
# heuristic below AND by _class_token_is_member_modifier's `class <modifier>*
# func/var/subscript/init` member-form detection -- one list, not two.
_DECL_MODIFIER_WORDS = (
    "public", "private", "internal", "fileprivate", "open", "final", "static",
    "class", "override", "required", "convenience", "indirect", "mutating",
    "nonisolated", "package", "consuming", "borrowing",
)

_RESYNC_RE = re.compile(
    r"^[ \t]{0,8}(?:(?:" + "|".join(_DECL_MODIFIER_WORDS) + r"|@\w+(?:\([^)]*\))?)\s+)*"
    r"(?:func|init|class|struct|enum|protocol|extension|actor|var|subscript)\b"
)


def _iter_lines(text: str, start: int):
    i = start
    n = len(text)
    while i <= n:
        j = text.find("\n", i)
        if j == -1:
            yield i, n
            return
        yield i, j
        i = j + 1


def _find_resync_point(text: str, from_idx: int):
    """The gap-fallback resync heuristic: from the line AFTER the desync
    point, the next line that (after up to 8 columns of indent and any
    modifiers) starts a func/init/class/struct/enum/protocol/extension
    declaration -- i.e. "looks top-level or type-member level". None if no
    such line exists before EOF (desync ran to end of file)."""
    nl = text.find("\n", from_idx)
    if nl == -1:
        return None
    for line_start, line_end in _iter_lines(text, nl + 1):
        if _RESYNC_RE.match(text[line_start:line_end]):
            return line_start
    return None


def _build_mask_and_match_dict(text: str):
    """The lexer-aware brace-walk shared by chunk_source and
    declared_symbol_names (finding 5, memlint's #symbol vocabulary):
    returns (mask, match_dict, gaps) -- mask is `text` with every
    comment/string's contents blanked to spaces (newlines kept, so line
    numbers still line up) and match_dict maps each '{' index to its
    matching '}' index for fully-closed top-level constructs. gaps is a
    list of (start_line, end_line) 1-based ranges skipped due to a brace
    desync (never a whole-file fallback -- indexing always resumes after
    the gap)."""
    n = len(text)
    mask_full = [("\n" if ch == "\n" else " ") for ch in text]
    match_dict: dict = {}
    gaps: list = []
    pos = 0
    while pos < n:
        desync_idx, local_writes, local_match, checkpoint_idx = _scan_region(text, pos)
        if desync_idx is None:
            # clean run to EOF: commit this region's writes/pairs.
            for idx, ch in local_writes.items():
                mask_full[idx] = ch
            match_dict.update(local_match)
            break
        # desync: only what was safely checkpointed (fully-closed
        # top-level constructs up to checkpoint_idx) is committed -- the
        # rest of this region's writes/pairs are discarded (see
        # _scan_region's docstring). The gap covers checkpoint_idx (the
        # last known-good point) through the resync point.
        for idx, ch in local_writes.items():
            if idx < checkpoint_idx:
                mask_full[idx] = ch
        for open_idx, close_idx in local_match.items():
            if open_idx < checkpoint_idx:
                match_dict[open_idx] = close_idx
        resync_idx = _find_resync_point(text, desync_idx)
        gap_start_line = text.count("\n", 0, checkpoint_idx) + 1
        if resync_idx is None:
            gap_end_line = text.count("\n", 0, n) + 1
            gaps.append((gap_start_line, gap_end_line))
            break
        gap_end_line = text.count("\n", 0, resync_idx) + 1
        gaps.append((gap_start_line, gap_end_line))
        pos = resync_idx
    mask = "".join(mask_full)
    return mask, match_dict, gaps


def chunk_source(text: str):
    """Chunk one Swift file's source text. Returns (chunks, gaps) where
    chunks is a list of dicts (kind, symbol, qualified_name, signature,
    doc, start_line, end_line -- all 1-based) in source order, and gaps is
    a list of (start_line, end_line) 1-based ranges skipped due to a brace
    desync (counted + reported by the caller; never a whole-file
    fallback -- indexing always resumes after the gap)."""
    mask, match_dict, gaps = _build_mask_and_match_dict(text)
    chunks = _extract_decls(text, mask, match_dict)
    return chunks, gaps


_IDENT_RE = re.compile(r"[ \t\n]*([A-Za-z_][A-Za-z0-9_]*)")
_OPERATOR_CHARS = set("+-*/%=<>!&|^~?.")
_KEYWORD_RE = re.compile(
    r"\b(class|struct|enum|protocol|extension|actor|func|init|subscript|var)\b"
)


def _read_backtick_identifier(mask: str, i: int):
    """A backtick-quoted name (finding 6, e.g. `` `default` ``, used to
    escape a reserved word as an identifier) -- returns the name WITHOUT
    its backticks, or None if `i` (after skipping whitespace) isn't a
    backtick-opened name."""
    j = i
    n = len(mask)
    while j < n and mask[j] in (" ", "\n", "\t"):
        j += 1
    if j >= n or mask[j] != "`":
        return None
    k = j + 1
    while k < n and mask[k] != "`":
        k += 1
    return mask[j + 1 : k] if k < n else None


def _read_identifier(mask: str, i: int):
    m = _IDENT_RE.match(mask, i)
    if m:
        return m.group(1)
    return _read_backtick_identifier(mask, i)


def _read_dotted_segment(mask: str, i: int):
    """One segment of a dotted qualifier (finding 3): a plain identifier or
    a backtick-quoted name, either one, whitespace-led. Returns (name
    WITHOUT its backticks, end_idx) or (None, i)."""
    n = len(mask)
    j = i
    while j < n and mask[j] in (" ", "\n", "\t"):
        j += 1
    if j < n and mask[j] == "`":
        k = j + 1
        while k < n and mask[k] != "`":
            k += 1
        if k >= n:
            return None, i
        return mask[j + 1 : k], k + 1
    m = _IDENT_RE.match(mask, i)
    if m:
        return m.group(1), m.end()
    return None, i


def _read_dotted_identifier(mask: str, i: int):
    """Like _read_identifier, but for `extension Outer.Inner` (finding
    6) -- keeps the full dot-joined qualifier as one chain element so the
    nested type's members qualify as Outer.Inner.member, not Outer.member.
    Each segment may be plain OR backtick-quoted (finding 3: an extension
    naming a backtick-escaped type, e.g. `` extension `Type` `` or
    `` extension `Type`.Inner ``, must still keep its container -- it used
    to be silently dropped since the segment regex never matched a
    backtick)."""
    name, end = _read_dotted_segment(mask, i)
    if name is None:
        return None
    parts = [name]
    n = len(mask)
    pos = end
    while pos < n and mask[pos] == ".":
        seg, seg_end = _read_dotted_segment(mask, pos + 1)
        if seg is None:
            break
        parts.append(seg)
        pos = seg_end
    return ".".join(parts)


def _class_token_is_member_modifier(mask: str, after: int) -> bool:
    """Finding 3: `class` is both a type-decl keyword (`class Foo {}`) and
    a member modifier (`class func`/`class var`/`class subscript`, and
    `class` stacked with further modifiers, e.g. `class final func`) --
    only `class func`/`class var` used to be excluded from the
    phantom-container check, so e.g. `class subscript` was misparsed as a
    type named "subscript" containing the actual subscript's body. This
    skips over any further _DECL_MODIFIER_WORDS right after `class` and
    checks what member keyword actually follows; func/init/subscript/var/
    let means this `class` token is a modifier, never a type declaration
    (the member itself gets its own separate _KEYWORD_RE match, so nothing
    is lost by skipping container creation here). A genuine type
    declaration never has a modifier between `class` and its name (Swift
    modifiers precede `class`, never follow it), so anything else here
    falls through to being treated as the type's own name, unchanged from
    prior behavior."""
    j = after
    n = len(mask)
    while True:
        while j < n and mask[j] in (" ", "\t", "\n"):
            j += 1
        m = _IDENT_RE.match(mask, j)
        if not m:
            return False
        word = m.group(1)
        if word in ("func", "var", "subscript", "init", "let"):
            return True
        if word in _DECL_MODIFIER_WORDS:
            j = m.end()
            continue
        return False


def _container_type_name(kw: str, mask: str, after: int):
    """Shared by _extract_decls and declared_symbol_names (findings 3/7):
    for a class/struct/enum/protocol/extension/actor _KEYWORD_RE match,
    returns the container's own name, or None when this isn't really a
    type declaration at all (a `class <modifier>* func/var/subscript/init`
    member form, or a keyword used as a plain identifier like
    `let actor = ...`)."""
    if kw == "class" and _class_token_is_member_modifier(mask, after):
        return None
    if kw == "extension":
        name = _read_dotted_identifier(mask, after)
    else:
        name = _read_identifier(mask, after)
    return name or None


def _read_identifier_or_operator(mask: str, i: int):
    j = i
    n = len(mask)
    while j < n and mask[j] in (" ", "\n", "\t"):
        j += 1
    if j < n and mask[j] == "`":
        return _read_backtick_identifier(mask, i)
    if j < n and (mask[j].isalpha() or mask[j] == "_"):
        return _read_identifier(mask, i)
    k = j
    while k < n and mask[k] in _OPERATOR_CHARS:
        k += 1
    return mask[j:k] if k > j else None


def _read_init_suffix(mask: str, i: int) -> str:
    j = i
    n = len(mask)
    while j < n and mask[j] in (" ", "\n", "\t"):
        j += 1
    if j < n and mask[j] in ("?", "!"):
        return mask[j]
    return ""


def _find_body_open(mask: str, match_dict: dict, i: int, limit: int):
    """From just after a decl's keyword+name(+header), find its own
    body-open '{' -- the first '{' at paren_depth 0, jumping wholesale
    (via match_dict) over any nested brace region first (a default
    parameter's closure literal, a where-clause, etc). Returns None if the
    enclosing scope's own '}' is hit first (a body-less protocol
    requirement -- not a chunk) or the region ends unmatched."""
    paren_depth = 0
    j = i
    while j < limit:
        c = mask[j]
        if c == "(":
            paren_depth += 1
        elif c == ")":
            paren_depth -= 1
        elif c == "{":
            if paren_depth <= 0:
                return j
            nxt = match_dict.get(j)
            if nxt is None:
                return None
            j = nxt
        elif c == "}":
            return None
        j += 1
    return None


def _classify_var(mask: str, i: int, limit: int):
    """From just after `var NAME`, decide whether it's a computed property
    (chunk) or a stored one (never a chunk -- includes `lazy var x = { ...
    }()`, excluded because '=' is found before '{'). Returns (is_computed,
    body_open_idx_or_None).

    A stored property with no initializer (`var x: Int` alone, e.g. a
    protocol requirement or a plain field) is statement-terminated by its
    newline: at each '\\n' outside any paren/bracket nesting, peek past
    following whitespace (blank lines, and comment lines, which are
    already blank in mask) for the next real character -- only '{' (brace
    on the next line) or '=' (a multi-line initializer) means the same
    declaration continues; anything else (typically the next
    declaration's own keyword) means this one had no body.

    willSet/didSet observers (finding 6) are syntactically identical to a
    computed property's getter block up to this point -- a type
    annotation directly followed by '{', no initializer -- so a stored
    property with observers and no initializer (`var x: Int { willSet
    {...} didSet {...} }`) would otherwise be misclassified as computed.
    Only the block's own first token distinguishes them: if it's
    `willSet`/`didSet`, this is NOT a computed-var chunk."""
    paren_depth = 0
    bracket_depth = 0
    j = i
    while j < limit:
        c = mask[j]
        if c == "\n" and paren_depth <= 0 and bracket_depth <= 0:
            k = j
            while k < limit and mask[k] in (" ", "\t", "\n"):
                k += 1
            if k >= limit or mask[k] not in ("{", "="):
                return False, None
            j = k
            continue
        if c == "(":
            paren_depth += 1
        elif c == ")":
            paren_depth -= 1
        elif c == "[":
            bracket_depth += 1
        elif c == "]":
            bracket_depth -= 1
        elif paren_depth <= 0 and bracket_depth <= 0:
            if c == "=":
                return False, None
            if c == "{":
                first = _read_identifier(mask, j + 1)
                if first in ("willSet", "didSet"):
                    return False, None
                return True, j
            if c == "}":
                return False, None
        j += 1
    return False, None


def _signature_text(text: str, kstart: int, body_open: int) -> str:
    raw = text[kstart:body_open]
    return re.sub(r"\s+", " ", raw).strip()


def _doc_comment_before(text: str, kstart: int) -> str:
    line_start = text.rfind("\n", 0, kstart) + 1
    lines_before = text[:line_start].splitlines()
    doc_lines: list = []
    idx = len(lines_before) - 1
    while idx >= 0:
        stripped = lines_before[idx].strip()
        if stripped.startswith("///"):
            doc_lines.insert(0, stripped[3:].strip())
            idx -= 1
            continue
        break
    return "\n".join(doc_lines)


def _extract_decls(text: str, mask: str, match_dict: dict):
    n = len(text)
    raw_types: list = []
    raw_chunks: list = []

    for m in _KEYWORD_RE.finditer(mask):
        kw = m.group(1)
        kstart = m.start()
        after = kstart + len(kw)

        if kw in ("class", "struct", "enum", "protocol", "extension", "actor"):
            # `extension Outer.Inner` (finding 6), a backtick-quoted
            # target (finding 3), and `class <modifier>* func/var/
            # subscript/init` member forms (finding 3, not just `class
            # func`/`class var`) are all resolved by the one shared
            # helper -- see _container_type_name.
            name = _container_type_name(kw, mask, after)
            if not name:
                # a `class` member-modifier form, or a keyword used as a
                # plain identifier (`let actor = ...`) -- never a
                # container.
                continue
            body_open = _find_body_open(mask, match_dict, after, n)
            if body_open is None:
                continue
            body_close = match_dict.get(body_open)
            if body_close is None:
                continue
            raw_types.append((name, kstart, body_open, body_close))
            continue

        if kw in ("func", "init", "subscript"):
            if kw == "func":
                name = _read_identifier_or_operator(mask, after)
                if not name:
                    continue
            elif kw == "init":
                name = "init" + _read_init_suffix(mask, after)
            else:
                name = "subscript"
            body_open = _find_body_open(mask, match_dict, after, n)
            if body_open is None:
                continue
            body_close = match_dict.get(body_open)
            if body_close is None:
                continue
            raw_chunks.append((kw, name, kstart, body_open, body_close))
            continue

        if kw == "var":
            name = _read_identifier(mask, after)
            if not name:
                continue
            is_computed, body_open = _classify_var(mask, after, n)
            if not is_computed:
                continue
            body_close = match_dict.get(body_open)
            if body_close is None:
                continue
            raw_chunks.append(("var", name, kstart, body_open, body_close))
            continue

    def enclosing_chain(pos: int) -> list:
        containing = [t for t in raw_types if t[1] < pos < t[3]]
        containing.sort(key=lambda t: (t[3] - t[1]), reverse=True)  # outermost first
        return [t[0] for t in containing if t[0]]

    out = []
    for kw, name, kstart, body_open, body_close in raw_chunks:
        chain = enclosing_chain(kstart)
        qualified = ".".join(chain + [name]) if chain else name
        sig = _signature_text(text, kstart, body_open)
        doc = _doc_comment_before(text, kstart)
        start_line = text.count("\n", 0, kstart) + 1
        end_line = text.count("\n", 0, body_close) + 1
        out.append(
            {
                "kind": kw,
                "symbol": name,
                "qualified_name": qualified,
                "signature": sig,
                "doc": doc,
                "start_line": start_line,
                "end_line": end_line,
            }
        )
    out.sort(key=lambda c: c["start_line"])
    return out


def chunk_file(text: str, rel_path: str) -> ChunkResult:
    """Registry-contract wrapper around `chunk_source` (chunkers.__init__'s
    ChunkResult, spec S1a): every chunk dict gains `"lang": "swift"`, gap
    tuples become `(start_line, end_line, "brace-desync")` 3-tuples, and
    status is `"ok"` when there were no gaps, else `"partial"` (chunk_source
    never fails outright -- a brace desync just skips the affected region
    and indexing resumes after it, so `"failed"` never applies here).
    `rel_path` is unused by the walker itself; it is part of the registry
    contract (chunkers.get_chunker callers pass it uniformly across
    backends)."""
    chunks, gaps = chunk_source(text)
    for chunk in chunks:
        chunk["lang"] = "swift"
    gaps3 = [(start, end, "brace-desync") for start, end in gaps]
    status = "ok" if not gaps3 else "partial"
    return ChunkResult(chunks, gaps3, status)
