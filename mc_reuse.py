"""mc_reuse.py -- the reuse inventory checker (docs/REUSE-INVENTORY.json).

The inventory names jobs the engine implements ONCE (a joining helper, a
sentence builder, a parser core) and the deterministic guards that reject a
recognisable SECOND implementation. This module is the whole checker: it loads
and validates the inventory, scans the production tree for definitions, runs
the guard methods the rows name, and renders rows as text (the context pack
uses that rendering).

Stdlib only, importable by tests and by memidx.py; it never imports a
production module -- Python is read with `ast`, shell with a bounded scanner.

What a guard is NOT: a test can reject a recognisable second implementation; it
cannot recognise every rewritten implementation of the same job. Each row says
what its guards miss (`coverage_limit`). Discovery of new jobs is a separate,
human-judged process; this module only keeps confirmed jobs from regressing.

The shell scanner is a documented, bounded scanner, not a Bash parser:

  * function declarations come from the one parse (`_sh_parse`), at command
    position (it descends into `$( )` and backticks): `name() {`, `function name {`
    and `function name () {`, the brace on the same line or a later one; the body
    ends at the `}` the parse pairs with the `{`, so a declaration-looking line in a
    quote or an array literal is text and a real one inside a command substitution
    is a definition. `name() (` and other forms are reported, never guessed at.
  * one lexical authority: `_sh_parse` is the only code that decides where a
    quote, a comment, a heredoc body, a continuation, a skipped group or a function
    body begins and ends; command words, command rendering, interpreter starts, the
    heredoc and comment masking, the declarations and the identifier compare all
    read that parse. Every skipped region (parenthesis group, `${ }`, `[[ ]]`,
    backticks) is walked by its one `skip_region` step. A backslash-newline is
    dropped only outside single quotes, by the reader.
  * a boundary the parse cannot decide exactly is a scan problem, not a skip: a
    heredoc delimiter that is not a plain or simply quoted word, a `case` inside a
    skipped group, a `#` comment inside backticks, a function with no closing `}`.
  * comments: the `#` comments that parse reports (a `#` that starts a word
    outside quotes, whole-line or trailing) are dropped from every text the
    guards read; a `#` line inside a multi-line quoted string is text.
  * heredoc bodies are dropped from the shell text; a terminator line must
    equal the delimiter exactly (leading tabs are stripped for `<<-` only). A
    heredoc fed to a python interpreter (`python - <<'EOF'`, `"$MC_PY" - <<'EOF'`,
    the interpreter and the `<<` possibly on different physical lines of one
    command) and a single-quoted `<interpreter> -c '...'` payload are
    extracted and scanned as Python. A double-quoted `-c "..."` payload and
    Python held in a shell variable are NOT scanned.
"""
from __future__ import annotations

import ast
import bisect
import copy
import fnmatch
import functools
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1
POLICIES = ("singleton", "allowed_variants", "known_debt")
METHODS = ("name_owner", "exact_copy", "token_owner", "delegation", "vocab_owner")
ROW_FIELDS = ("id", "job", "policy", "implementation", "contract", "why_single", "related_paths",
              "variants", "guards", "coverage_limit", "evidence", "test")
TOP_FIELDS = ("schema_version", "scan_roots", "exclusions", "rows")
OPTIONAL_TOP_FIELDS = ("tests_dir",)    # null: this inventory has no tests directory to resolve test ids in
INVENTORY_RELPATH = "docs/REUSE-INVENTORY.json"

_INTERP = r"(?:python[\w.]*|\$\{?\w*PY\w*\}?)"
_PY_INLINE_RE = re.compile(r"(?i)\"?" + _INTERP + r"\"?\s+(?:-\w+\s+)*-c\s+'")


class InventoryError(Exception):
    """The inventory could not be loaded; `errors` lists every problem."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


# ---------------------------------------------------------------------------
# Loading and schema validation
# ---------------------------------------------------------------------------

def load_inventory(path) -> dict:
    """The parsed inventory at `path`; InventoryError on unreadable JSON."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise InventoryError([f"cannot read inventory {path}: {exc}"])
    except ValueError as exc:
        raise InventoryError([f"inventory {path} is not valid JSON: {exc}"])
    if not isinstance(data, dict):
        raise InventoryError([f"inventory {path}: the top level must be an object"])
    return data


def _is_str_list(v):
    return isinstance(v, list) and all(isinstance(x, str) and x for x in v)


def validate_schema(inv: dict) -> list:
    """Every structural problem with `inv`, as clear messages (no scanning)."""
    errs = []
    for k in TOP_FIELDS:
        if k not in inv:
            errs.append(f"inventory: missing top-level field {k!r}")
    for k in inv:
        if k not in TOP_FIELDS and k not in OPTIONAL_TOP_FIELDS:
            errs.append(f"inventory: unknown top-level field {k!r}")
    if "tests_dir" in inv and inv["tests_dir"] is not None:
        errs.append("inventory: tests_dir, when present, must be null (the inventory declares it has no tests "
                    "directory); omit it to have every row's test id resolved under tests/")
    if inv.get("schema_version") != SCHEMA_VERSION:
        errs.append(f"inventory: schema_version must be {SCHEMA_VERSION}, got {inv.get('schema_version')!r}")
    if not _is_str_list(inv.get("scan_roots")) or not inv.get("scan_roots"):
        errs.append("inventory: scan_roots must be a non-empty list of repo-relative paths")
    if not _is_str_list(inv.get("exclusions")):
        errs.append("inventory: exclusions must be a list of directory names or repo-relative prefixes")
    rows = inv.get("rows")
    if not isinstance(rows, list) or not rows:
        errs.append("inventory: rows must be a non-empty list")
        return errs
    seen = set()
    for idx, row in enumerate(rows):
        label = f"row {idx}"
        if not isinstance(row, dict):
            errs.append(f"{label}: must be an object")
            continue
        rid = row.get("id")
        if isinstance(rid, str) and rid:
            label = f"row {rid!r}"
            if rid in seen:
                errs.append(f"{label}: duplicate id")
            seen.add(rid)
        errs.extend(_validate_row(label, row))
    return errs


def _symbol_ref_ok(s):
    return isinstance(s, str) and s.count("#") == 1 and all(s.split("#"))


def _validate_row(label, row) -> list:
    errs = []
    for k in ROW_FIELDS:
        if k not in row:
            errs.append(f"{label}: missing field {k!r}")
    for k in row:
        if k not in ROW_FIELDS:
            errs.append(f"{label}: unknown field {k!r}")
    for k in ("id", "job", "contract", "why_single", "coverage_limit", "evidence", "test"):
        if k in row and (not isinstance(row[k], str) or not row[k].strip()):
            errs.append(f"{label}: {k} must be a non-empty string")
    policy = row.get("policy")
    if "policy" in row and policy not in POLICIES:
        errs.append(f"{label}: policy must be one of {', '.join(POLICIES)}, got {policy!r}")
    if "implementation" in row and not _symbol_ref_ok(row["implementation"]):
        errs.append(f"{label}: implementation must be path#qualified_symbol, got {row['implementation']!r}")
    if "related_paths" in row and not _is_str_list(row["related_paths"]):
        errs.append(f"{label}: related_paths must be a list of strings")
    variants = row.get("variants")
    if "variants" in row:
        if not isinstance(variants, list):
            errs.append(f"{label}: variants must be a list")
            variants = []
        for vi, v in enumerate(variants):
            if (not isinstance(v, dict) or set(v) != {"symbol", "contract", "reason"}
                    or not _symbol_ref_ok(v.get("symbol"))
                    or not all(isinstance(v.get(k), str) and v[k].strip() for k in ("contract", "reason"))):
                errs.append(f"{label}: variants[{vi}] must be exactly {{symbol: path#symbol, contract, reason}}")
    guards = row.get("guards")
    if "guards" in row:
        if not isinstance(guards, list):
            errs.append(f"{label}: guards must be a list")
            guards = []
        for gi, g in enumerate(guards):
            if not isinstance(g, dict) or set(g) != {"method", "params"} or not isinstance(g.get("params"), dict):
                errs.append(f"{label}: guards[{gi}] must be exactly {{method, params}} (params an object)")
                continue
            if g["method"] not in METHODS:
                errs.append(f"{label}: guards[{gi}]: unknown method {g['method']!r} "
                            f"(known: {', '.join(METHODS)})")
                continue
            errs.extend(_validate_guard_params(f"{label}: guards[{gi}] {g['method']}", g["method"], g["params"]))
    if policy == "singleton" and isinstance(guards, list) and not guards:
        errs.append(f"{label}: a singleton row must name at least one guard method")
    if policy == "allowed_variants" and isinstance(variants, list) and not variants:
        errs.append(f"{label}: an allowed_variants row must list its variants")
    if policy == "allowed_variants" and isinstance(guards, list) and guards:
        errs.append(f"{label}: an allowed_variants row carries no guard (it records a deliberate variant, not a singleton)")
    if policy == "known_debt":
        if isinstance(guards, list) and guards:
            errs.append(f"{label}: a known_debt row is never guarded; remove its guards")
        if isinstance(variants, list) and not variants:
            errs.append(f"{label}: a known_debt row must name every current implementation in variants")
    return errs


def _forbid_entry(f):
    """`(scope, regex)` of one `forbid` entry, or None if it is malformed. A bare
    string, or `{"text": R}`, is searched in the unit's executable text; a
    `{"command": R}` is matched, from its first word, against each parsed shell
    simple command (see _sh_command_strings)."""
    if isinstance(f, str):
        return "text", f
    if isinstance(f, dict) and len(f) == 1:
        (scope, rx), = f.items()
        if scope in ("text", "command") and isinstance(rx, str):
            return scope, rx
    return None


def _validate_guard_params(label, method, params) -> list:
    errs = []
    allowed = {
        "name_owner": {"pattern", "allow"},
        "exact_copy": set(),
        "token_owner": {"pattern", "paths"},
        "delegation": {"consumers", "refs", "forbid", "aliases"},
        "vocab_owner": set(),
    }[method]
    for k in params:
        if k not in allowed:
            errs.append(f"{label}: unknown parameter {k!r}")
    for k in ("pattern",):
        if k in params:
            try:
                re.compile(params[k])
            except (re.error, TypeError):
                errs.append(f"{label}: pattern is not a valid regular expression")
    if method == "token_owner" and "pattern" not in params:
        errs.append(f"{label}: needs a pattern")
    for k in ("allow", "paths", "refs", "aliases"):
        if k in params and not _is_str_list(params[k]):
            errs.append(f"{label}: {k} must be a list of strings")
    if "allow" in params and _is_str_list(params["allow"]):
        for a in params["allow"]:
            if not _symbol_ref_ok(a):
                errs.append(f"{label}: allow entry {a!r} must be path#symbol")
    if method == "delegation":
        if not _is_str_list(params.get("consumers")) or not params.get("consumers"):
            errs.append(f"{label}: needs a non-empty consumers list")
        if "forbid" in params:
            if not isinstance(params["forbid"], list):
                errs.append(f"{label}: forbid must be a list of regular expressions")
            else:
                for f in params["forbid"]:
                    entry = _forbid_entry(f)
                    if entry is None:
                        errs.append(f"{label}: forbid entry {f!r} must be a regular expression string or "
                                    "{\"text\": REGEX} or {\"command\": REGEX}")
                        continue
                    try:
                        re.compile(entry[1])
                    except re.error:
                        errs.append(f"{label}: forbid entry {f!r} is not a valid regular expression")
    return errs


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

@dataclass
class Definition:
    path: str
    name: str               # simple name
    qual: str               # qualified symbol (Class.method / function / CONSTANT)
    lang: str               # "py" | "sh"
    kind: str               # "func" | "const"
    lineno: int
    end_lineno: int
    embedded: bool = False
    node: object = None     # ast node (py)
    body: list = field(default_factory=list)   # normalised-ready body lines (sh)

    def where(self):
        return f"{self.path}:{self.lineno}" + (" (embedded python)" if self.embedded else "")


@dataclass
class Unit:
    """One executable region a guard reads: an outermost function, a method, or
    the residual code outside any definition of a file or embedded payload."""
    path: str
    qual: str
    lang: str
    lineno: int
    end_lineno: int
    text: str
    calls: frozenset    # names EXECUTED here: python Call targets, shell command words
    loads: frozenset    # python value references (Name loads, attributes); shell: = calls
    commands: tuple = ()    # shell: each simple command rendered as its words (see _sh_command_strings)


@dataclass
class Scan:
    root: str
    defs: list = field(default_factory=list)
    units: list = field(default_factory=list)
    trees: list = field(default_factory=list)       # (path, lineno_offset, ast.Module, embedded)
    problems: list = field(default_factory=list)    # unsupported / unparseable / unterminated production syntax
    files: dict = field(default_factory=dict)       # relpath -> "py" | "sh"
    seconds: float = 0.0
    fingerprints: dict = field(default_factory=dict)   # id(Definition) -> normalised body


def _excluded(rel: str, exclusions) -> bool:
    parts = rel.split("/")
    for ex in exclusions:
        ex = ex.strip("/")
        if "/" in ex:
            if rel == ex or rel.startswith(ex + "/"):
                return True
        elif ex in parts:
            return True
    return False


def _iter_source_files(root: Path, scan_roots, exclusions):
    seen = set()
    for sr in scan_roots:
        base = (root / sr)
        if not base.exists():
            continue
        if base.is_file() and not base.is_symlink():
            rel = os.path.relpath(base, root).replace(os.sep, "/")
            if rel not in seen and not _excluded(rel, exclusions):
                seen.add(rel)
                yield rel
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            drel = os.path.relpath(dirpath, root).replace(os.sep, "/")
            dirnames[:] = sorted(d for d in dirnames
                                 if not _excluded(f"{drel}/{d}" if drel != "." else d, exclusions)
                                 and not os.path.islink(os.path.join(dirpath, d)))
            for fn in sorted(filenames):
                rel = f"{drel}/{fn}" if drel != "." else fn
                full = os.path.join(dirpath, fn)
                if os.path.islink(full) or _excluded(rel, exclusions) or rel in seen:
                    continue
                seen.add(rel)
                yield rel


def scan_tree(root, scan_roots, exclusions) -> Scan:
    """Scan every `.py` and `.sh` file under the scan roots (minus exclusions)."""
    t0 = time.perf_counter()
    root = Path(root)
    scan = Scan(root=str(root))
    for rel in _iter_source_files(root, scan_roots, exclusions):
        if rel.endswith(".py"):
            lang = "py"
        elif rel.endswith(".sh"):
            lang = "sh"
        else:
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            scan.problems.append(f"{rel}: cannot read: {exc}")
            continue
        scan.files[rel] = lang
        if lang == "py":
            _scan_python(scan, rel, text, 0, False, "")
        else:
            _scan_shell(scan, rel, text)
    scan.seconds = time.perf_counter() - t0
    return scan


# -- python ------------------------------------------------------------------

def _is_docstring(st) -> bool:
    return isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str)


def _strip_docstring(node, root=True):
    """`node` with the docstring of EVERY module, class and function inside it
    removed (nested ones too): prose is not executable text for a guard. With
    `root=False` the node's own leading string is kept (a synthetic module of
    leftover statements has no docstring: its first string is code)."""
    for n in ast.walk(node):
        if not isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if n is node and not root:
            continue
        body = n.body
        if body and _is_docstring(body[0]):
            n.body = body[1:] or [ast.Pass()]
    return node


def _unit_text(node) -> str:
    n = _strip_docstring(copy.deepcopy(node))
    try:
        return ast.unparse(n)
    except Exception:   # unparse of a hand-built or odd node: fall back to a dump
        return ast.dump(n)


def _calls_and_loads(node):
    """`(calls, loads)` of a Python node: the names it CALLS (`f(...)` and
    `x.f(...)`) and the names it merely loads as values. A bare import is
    neither."""
    calls, loads = set(), set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                calls.add(f.id)
            elif isinstance(f, ast.Attribute):
                calls.add(f.attr)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            loads.add(n.id)
        elif isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load):
            loads.add(n.attr)
    return frozenset(calls), frozenset(loads)


def _nested_stmts(st):
    """The statements directly inside a compound statement, whatever its kind:
    its own statement children plus the bodies of except handlers and match cases."""
    out = []
    for ch in ast.iter_child_nodes(st):
        if isinstance(ch, ast.stmt):
            out.append(ch)
        elif isinstance(ch, (ast.ExceptHandler, ast.match_case)):
            out.extend(ch.body)
    return out


def _scan_python(scan, rel, text, line_offset, embedded, prefix):
    """Definitions and units of one Python source (a file or an embedded payload)."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError) as exc:
        if embedded:
            scan.problems.append(f"{rel}:{line_offset + 1}: embedded python payload is not parseable ({exc}); "
                                 "the inventory cannot scan it, so no guard sees what it defines")
        else:
            scan.problems.append(f"{rel}: not parseable as Python ({exc}); the inventory cannot scan it")
        return
    scan.trees.append((rel, line_offset, tree, embedded))

    def add_def(node, stack, kind="func", name=None):
        nm = name or node.name
        qual = ".".join(stack + [nm])
        scan.defs.append(Definition(
            path=rel, name=nm, qual=(prefix + qual), lang="py", kind=kind,
            lineno=getattr(node, "lineno", 0) + line_offset,
            end_lineno=getattr(node, "end_lineno", getattr(node, "lineno", 0)) + line_offset,
            embedded=embedded, node=node))

    def visit(stmts, stack):
        for st in stmts:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                add_def(st, stack)
                visit(st.body, stack + [st.name])
            elif isinstance(st, ast.ClassDef):
                visit(st.body, stack + [st.name])
            elif isinstance(st, ast.Assign) and not stack:
                for t in st.targets:
                    if isinstance(t, ast.Name):
                        add_def(st, stack, "const", t.id)
            elif isinstance(st, ast.AnnAssign) and not stack and isinstance(st.target, ast.Name) and st.value is not None:
                add_def(st, stack, "const", st.target.id)
            else:
                # every other statement (if/for/while/with/async variants/try/try-star/match ...):
                # descend into whatever statements it holds, keeping the class scope
                visit(_nested_stmts(st), stack)

    visit(tree.body, [])

    residual = []

    def unit_for(node, qual):
        calls, loads = _calls_and_loads(node)
        scan.units.append(Unit(
            path=rel, qual=prefix + qual, lang="py", lineno=node.lineno + line_offset,
            end_lineno=node.end_lineno + line_offset, text=_unit_text(node), calls=calls, loads=loads))

    for pos, st in enumerate(tree.body):
        if pos == 0 and _is_docstring(st):
            continue                # the module's own docstring; a later bare string is code and stays
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            unit_for(st, st.name)
        elif isinstance(st, ast.ClassDef):
            for sub_pos, sub in enumerate(st.body):
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    unit_for(sub, f"{st.name}.{sub.name}")
                elif not (sub_pos == 0 and _is_docstring(sub)):
                    residual.append(sub)
        else:
            residual.append(st)
    if residual:
        mod = _strip_docstring(ast.Module(body=copy.deepcopy(residual), type_ignores=[]), root=False)
        try:
            rtext = ast.unparse(mod)
        except Exception:
            rtext = ast.dump(mod)
        rcalls, rloads = _calls_and_loads(ast.Module(body=residual, type_ignores=[]))
        scan.units.append(Unit(path=rel, qual=prefix + "<module>", lang="py", lineno=1 + line_offset,
                               end_lineno=(tree.body[-1].end_lineno if tree.body else 1) + line_offset,
                               text=rtext, calls=rcalls, loads=rloads))


# -- shell -------------------------------------------------------------------

def _sh_interpreter(cmd):
    """The index in `cmd.words` of the word that names the program of a parsed
    command -- its first word, or the one after `env` and its `VAR=value`
    arguments -- or None."""
    k = 0
    if cmd.words and cmd.words[0].text == "env":
        k = 1
        while k < len(cmd.words) and _SH_ASSIGN_RE.match(cmd.words[k].raw):
            k += 1
    return k if k < len(cmd.words) else None


def _text_lines(text: str):
    """`(lines, offsets)` of `text`: its lines, and the offset where each begins."""
    lines = text.split("\n")
    offsets = []
    pos = 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln) + 1
    return lines, offsets


def _inline_py_words(text, cmds, lines, offsets):
    """`[(word, line_index)]`: the single-quoted `<interpreter> -c '...'` payload word of each interpreter
    command in `cmds`, when it starts on the interpreter's line -- the one word the parse reads after `-c`."""
    word_at = {w.start: w for cmd in cmds for w in cmd.words}
    out = []
    for cmd in cmds:
        k = _sh_interpreter(cmd)
        if k is None:
            continue
        st = cmd.words[k].start
        li = bisect.bisect_right(offsets, st) - 1
        m = _PY_INLINE_RE.match(text, st, offsets[li] + len(lines[li]))
        w = word_at.get(m.end() - 1) if m else None
        if w is None or not w.plain_sq or w.end - w.start < 2 or text[w.end - 1] != "'":
            continue
        out.append((w, li))
    return out


def _mask_shell(text: str):
    """`(masked_lines, payloads, problems)`: the file's lines with comments,
    heredoc bodies and python `-c '...'` payload bodies blanked (line numbers
    preserved), the extracted python payloads as `(first_line_number, source)`,
    and the `(line_number, message)` problems found (an unterminated heredoc and
    every boundary the parse reports it cannot decide: see `_sh_parse`).
    The whole text is parsed once (`_sh_parse` with bodies=True) and every span
    masked here -- comment, heredoc body, payload -- is a span that parse
    reports, so a quote opened on one line is still open on the next, and a
    `#`, a `<<` or an interpreter name inside it is text."""
    lines, offsets = _text_lines(text)
    payloads = []
    problems = []

    def line_of(off):
        return bisect.bisect_right(offsets, off) - 1

    cmds, heredocs, comments, _decls, parse_problems = _sh_parse(text, True)
    for off, msg in parse_problems:
        problems.append((line_of(off) + 1, msg))
    edits = []      # (start, end, replacement): spans of `text` to rewrite; a replacement keeps the newlines
    for s0, e0 in comments:                 # a comment goes, with the blanks before it
        while s0 > 0 and text[s0 - 1] in " \t":
            s0 -= 1
        edits.append((s0, e0, ""))
    # heredocs: blank body and terminator; a python heredoc body is a payload
    seen_cmds = set()
    for rec in heredocs:
        first = rec.cmd is None or id(rec.cmd) not in seen_cmds
        if rec.cmd is not None:
            seen_cmds.add(id(rec.cmd))
        if rec.closed is False:
            li = line_of(rec.op)
            problems.append((li + 1, f"unterminated heredoc: `<<{'-' if rec.dash else ''}{rec.tag}` opened here is "
                                     f"never closed by a line holding `{rec.tag}`; the scanner cannot tell code "
                                     "from data after it"))
        if not rec.closed:
            continue
        sb, tl = line_of(rec.body), line_of(rec.term)
        if first and rec.cmd is not None and _is_python_stdin(rec.cmd):
            body_lines = lines[sb:tl]
            if rec.dash:
                body_lines = [bl.lstrip("\t") for bl in body_lines]   # `<<-` strips leading tabs
            payloads.append((sb + 1, "\n".join(body_lines)))
        edits.append((offsets[sb], offsets[tl] + len(lines[tl]), "\n" * (tl - sb)))
    # single-quoted `<interp> -c '...'` payload: the one word the parse reads after `-c`, on the interpreter's line
    for w, li in _inline_py_words(text, cmds, lines, offsets):
        payloads.append((li + 1, w.text))
        edits.append((w.start + 1, w.end - 1, "\n" * text.count("\n", w.start, w.end)))
    payloads.sort(key=lambda pl: pl[0])
    problems.sort()
    out = []
    p = 0
    for s0, e0, repl in sorted(edits):
        if s0 < p:
            continue                        # inside a span already rewritten
        out.append(text[p:s0])
        out.append(repl)
        p = e0
    out.append(text[p:])
    return "".join(out).split("\n"), payloads, problems


def _is_python_stdin(cmd) -> bool:
    """Is `cmd` a python interpreter reading its program from standard input
    (`python3 -`, `"$MC_PY" -`, after `env` and assignments)? Its heredoc body is then python."""
    k = _sh_interpreter(cmd)
    return (k is not None and k + 1 < len(cmd.words) and cmd.words[k + 1].raw == "-"
            and re.fullmatch(r'(?i)"?' + _INTERP + '"?', cmd.words[k].raw) is not None)


def _decl_body(masked, lines, offsets, d, ln, end):
    """The body lines of declaration `d` (starting on line `ln`, closed on line `end`) as the guards normalise
    them: the masked text after the `{` on the declaration line, the masked lines between, and the masked text
    before the closing `}` on its line."""
    def col_of(off):
        li = bisect.bisect_right(offsets, off) - 1
        return li, off - offsets[li]

    bl, bcol = col_of(d.open)
    el, ecol = col_of(d.end - 1)
    m = masked[bl]
    inner = m[bcol + 1:] if m[:bcol + 1] == lines[bl][:bcol + 1] else m[m.find("{") + 1:]
    if end == ln == bl:         # a one-liner: between the braces
        cm = masked[end]
        inner = cm[bcol + 1:ecol] if cm[:ecol] == lines[end][:ecol] else inner.rsplit("}", 1)[0]
        return [inner.strip()]
    body = [inner.strip()] if bl == ln and inner.strip() else []
    body += masked[ln + 1:end]
    cm = masked[end]
    tail = cm[:ecol] if cm[:ecol] == lines[end][:ecol] else cm.rsplit("}", 1)[0]
    if tail.strip():
        body.append(tail)
    return body


def _scan_shell(scan, rel, text):
    masked, payloads, mask_problems = _mask_shell(text)
    for lineno, msg in mask_problems:
        scan.problems.append(f"{rel}:{lineno}: {msg}")
    lines, offsets = _text_lines(text)
    cmds, _heredocs, _comments, decls, _problems = _sh_parse(text, True)
    n = len(masked)

    def line_of(off):
        return bisect.bisect_right(offsets, off) - 1

    spans = []      # (start offset, end offset, first line, last line, name) of each declaration the parse closed
    for d in sorted(decls, key=lambda d: d.start):
        ln = line_of(d.start)
        problem = d.problem or ("" if d.end >= 0 else f"shell function `{d.name}` has no closing `}}`: the "
                                                      "scanner cannot bound its body")
        if problem:
            scan.problems.append(f"{rel}:{ln + 1}: {problem}")
            continue
        end = line_of(d.end - 1)
        spans.append((d.start, d.end, ln, end, d.name))
        scan.defs.append(Definition(path=rel, name=d.name, qual=d.name, lang="sh", kind="func",
                                    lineno=ln + 1, end_lineno=end + 1,
                                    body=_decl_body(masked, lines, offsets, d, ln, end)))
    # units: outermost functions + residual; a unit's commands are the ones the parse recorded inside its extent
    outer = []
    for sp in spans:
        if outer and sp[0] < outer[-1][1]:
            continue
        outer.append(sp)
    blank = {w.start: "\n" * text.count("\n", w.start, w.end) for w, _li in _inline_py_words(text, cmds, lines, offsets)}
    starts = [sp[0] for sp in outer]
    inside = [[] for _ in outer]
    rest = []
    for cmd in cmds:
        if not cmd.words:
            continue
        pos = cmd.words[0].start
        k = bisect.bisect_right(starts, pos) - 1
        (inside[k] if k >= 0 and pos < outer[k][1] else rest).append(cmd)
    covered = set()
    for (s0, e0, ln, end, name), own in zip(outer, inside):
        cw = frozenset(_cmd_words(own))
        scan.units.append(Unit(path=rel, qual=name, lang="sh", lineno=ln + 1, end_lineno=end + 1,
                               text="\n".join(masked[ln:end + 1]), calls=cw, loads=cw,
                               commands=_cmd_strings(own, blank)))
        covered.update(range(ln, end + 1))
    cw = frozenset(_cmd_words(rest))
    scan.units.append(Unit(path=rel, qual="<script>", lang="sh", lineno=1, end_lineno=n,
                           text="\n".join(masked[k] for k in range(n) if k not in covered),
                           calls=cw, loads=cw, commands=_cmd_strings(rest, blank)))
    for first_line, src in payloads:
        _scan_python(scan, rel, src, first_line - 1, True, f"<embedded L{first_line}>.")


_SH_KEEP_CMD = frozenset({"if", "then", "elif", "else", "while", "until", "do", "time", "exec", "command",
                          "builtin", "eval", "!"})
_SH_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SH_ASSIGN_RE = re.compile(r"[A-Za-z_]\w*(?:\[[^\]]*\])?\+?=")
_SH_WORD_BREAK = " \t\n;&|()<>"     # the one set that ends a word and, so, decides that a `#` starts a comment
_SH_HEREDOC_OP_RE = re.compile(r"<<(-?)[ \t]*")
_SH_REDIR_RE = re.compile(r"(?:\d+|\{\w+\})?(?:<<<|<<-?|>>|>\||>&|<&|<>|&>>|&>|>|<)")


@dataclass(frozen=True)
class _ShWord:
    start: int      # offset of the word in the parsed text
    raw: str        # the source text of the word, a backslash-newline outside single quotes removed (as bash does)
    text: str       # the word with quotes and escapes removed (a substitution renders as `$(...)`)
    end: int = 0    # offset just after the word in the parsed text
    plain_sq: bool = False      # the word is single-quoted pieces and `\'` only (a literal payload)

    @property
    def ident(self) -> str:
        """The identifier this word spells -- bare (`mc_job`) or as one quoted
        piece (`"mc_job"`, `'mc_job'`) -- else ''. `mc_job"x"`, `mc_job$x`,
        `mc_job/x` are longer words, not the identifier."""
        r = self.raw
        if _SH_IDENT_RE.fullmatch(r):
            return r
        if len(r) > 2 and r[0] in "'\"" and r[-1] == r[0] and _SH_IDENT_RE.fullmatch(r[1:-1]):
            return r[1:-1]
        return ""

    @property
    def bare(self) -> bool:
        return _SH_IDENT_RE.fullmatch(self.raw) is not None or self.raw in ("{", "}", "!")


@dataclass(frozen=True)
class _ShCmd:
    """One simple command: its words after any `VAR=value` prefixes (and
    `if`/`then`/`exec`... keywords), and the assignments before them."""
    words: tuple
    assigns: tuple


@dataclass
class _ShHeredoc:
    """A `<<` / `<<-` operator outside quotes. With `bodies=True` parsing it also
    says where the body lies: `closed` is True (body = text[body:term], the
    terminator line text[term:end]), False (never closed) or None (not examined)."""
    op: int             # offset of the `<<`
    dash: bool
    tag: str = ""
    body: int = -1
    term: int = -1
    end: int = -1
    closed: object = None
    cmd: object = None  # the simple command this operator belongs to (None: a bare redirection)
    quoted: bool = False    # the delimiter word held a quote or a backslash: the body is literal text, not expanded


@dataclass
class _ShDecl:
    """A function declaration the parse met at command position (`name() {`, `function name {`,
    `function name () {`), wherever it sits -- also inside `$( )` and backticks. `end` is the offset just
    after the `}` that closes its body (-1: none found); `problem` is non-empty for a form the scanner
    does not read."""
    name: str
    start: int          # offset of the `function` keyword, or of the name in `name()`
    open: int = -1      # offset of the `{`
    end: int = -1
    problem: str = ""


_SH_CASE_RE = re.compile(r"case(?=[ \t\n])")
_SH_DECL_MSG = ("unsupported shell function form `{name}() {rest}`: the scanner reads `name() {{` and "
                "`function name {{` only")


@functools.lru_cache(maxsize=64)
def _sh_parse(text: str, bodies: bool = False):
    """`(commands, heredocs, comments, decls, problems)` of `text`: every simple
    command -- including the ones inside `$( )`, backticks, `<( )`, `( )` and the
    body of an unquoted heredoc -- every `<<` / `<<-` operator that sits outside
    quotes (`_ShHeredoc`, with the command it belongs to), with `bodies=True` the
    `(start, end)` span of every comment, every function declaration met at
    command position (`_ShDecl`: name, start, the `{` and the offset after the
    `}` that closes it, nested ones included, none from inside a quote or a
    skipped region) and the `(offset, message)` boundaries the parse cannot
    decide exactly (a heredoc delimiter that is not a literal word, a `#`
    inside a backtick substitution, a `case` inside a skipped group): the caller reports each as a scan problem.
    `bodies=False` is for text whose comments and heredoc bodies are already
    removed; `bodies=True` is for raw source: comments are skipped and recorded
    and, at the newline after the operator, the heredoc body is consumed up to
    its terminator line (compared exactly). This is the one place that decides
    where a quote, a comment, a heredoc body, a continuation, a skipped group
    or a function body begins and ends; every skipped region (parenthesis
    group, `${ }`, `[[ ]]`, backticks) is walked by the one `skip_region` step
    with the same quote and escape reader as a word. A bounded tokenizer, not a
    Bash parser: words, quotes, `$(( ))`/`(( ))` blobs, redirections (an
    operator consumes its operand word), `case` patterns and `name() {`
    declarations are told apart from commands; `[[ ]]`, `for ((...))` headers,
    extglob groups in case patterns, array literals and any `(` that does not
    start a command are skipped whole; `eval`, `exec`, `time`, `!` and the like
    keep command position, `command -v/-V/-p` do not."""
    n = len(text)
    cmds = []
    heredocs = []
    decls = []
    problems = []       # (offset, message); never rolled back: an undecidable boundary stays reported
    closers = []        # the closer of each open parse_seq: a backtick ends a word inside a backtick substitution
    pending = []        # heredocs whose body starts at the next newline (bodies=True)
    comments = []       # (start, end) of each `#` comment outside quotes (bodies=True)
    cont = []           # offsets of the backslash-newlines the reader dropped outside single quotes, in order
    subs = [0]          # substitutions and locale quotes the reader met: a heredoc delimiter may hold none

    def joined(a, b):   # text[a:b] without those continuations: the word as bash joins it
        lo, hi = bisect.bisect_left(cont, a), bisect.bisect_left(cont, b)
        if lo == hi:
            return text[a:b]
        out, p = [], a
        for q in cont[lo:hi]:
            out.append(text[p:q])
            p = q + 2
        out.append(text[p:b])
        return "".join(out)

    def mark():
        return (len(cmds), len(heredocs), len(comments), len(decls))

    def rollback(m):    # what a skipped or refused region read is not recorded
        del cmds[m[0]:], heredocs[m[1]:], comments[m[2]:], decls[m[3]:]
        kept = {id(h) for h in heredocs}
        pending[:] = [r for r in pending if id(r) in kept]

    def take_bodies(i):     # i is just after a newline: consume the bodies of the pending heredocs
        scans = []
        while pending:
            rec = pending.pop(0)
            rec.body = i
            if rec.tag is None:     # a refused delimiter: where the body ends is unknown, so the rest is data
                i = n
                del pending[:]
                break
            j = i
            while j < n:
                e = text.find("\n", j)
                e = n if e < 0 else e
                while not rec.quoted and e < n and (len(text[j:e]) - len(text[j:e].rstrip("\\"))) % 2:
                    e = text.find("\n", e + 1)     # an unquoted body joins a backslash-newline before the terminator test
                    e = n if e < 0 else e
                line = text[j:e]
                if (line.lstrip("\t") if rec.dash else line) == rec.tag:     # exact: a trailing blank does not close it
                    rec.term, rec.end, rec.closed = j, min(e + 1, n), True
                    i = rec.end
                    if not rec.quoted:
                        scans.append((rec.body, rec.term))
                    break
                j = e + 1
            else:
                rec.closed = False
                del pending[:]      # the scanner cannot tell code from data after an unterminated body
                break
        for a, b in scans:      # an unquoted body is expanded: its substitutions run (after all bodies are placed)
            scan_body(a, b)
        return i

    def scan_body(a, b):    # the substitutions of the unquoted heredoc body text[a:b]; quotes there are literal
        j = a
        while j < b:
            c = text[j]
            if c == "\\":
                j += 2
            elif text.startswith("$((", j):
                j = skip_group(j + 1)
            elif text.startswith("$(", j):
                j = parse_seq(j + 2, ")")
            elif text.startswith("${", j):
                j = skip_region(j + 2, "}", False)
            elif c == "`":
                j = parse_seq(j + 1, "`")
            else:
                j += 1
        if j > b:
            problems.append((a, "a command substitution in an unquoted heredoc body runs past the terminator "
                                "line; the scanner cannot tell code from data there"))

    def at_quote(i):        # a quoted piece starts at text[i]: '...', "..." or $'...'
        return text[i] in "'\"" or text.startswith("$'", i)

    def skip_quote(i):      # the index after the quoted piece at text[i], read by the word reader; the piece's own
        return read_quoted(i, [])   # text is dropped, a substitution inside a double-quoted piece is recorded (it runs)

    def take_comment(i):    # text[i] == "#" starts a comment: record it, the index of its end of line
        j = text.find("\n", i)
        j = n if j < 0 else j
        comments.append((i, j))
        return j

    def skip_heredoc_op(i):     # text[i:] is a `<<` / `<<-` inside a skipped group: the index after its delimiter word;
        m = _SH_HEREDOC_OP_RE.match(text, i)    # the heredoc is recorded and its body consumed at the next newline
        w, j = read_delim(m.end())
        rec = _ShHeredoc(i, bool(m.group(1)), w.text, quoted=w.raw != w.text)
        heredocs.append(rec)
        if w.text:
            pending.append(rec)
        return j

    def read_delim(i):      # the heredoc delimiter word at text[i]: bash removes its quotes and runs nothing in it
        m, before = mark(), subs[0]
        w, j = read_word(i)
        if subs[0] == before:
            return w, j
        rollback(m)         # a substitution or locale/ANSI-C quote there is not modelled: refused, records no command
        if bodies:
            refused = _ShHeredoc(w.start, False, None)      # its body would start at the next newline and has no known end
            heredocs.append(refused)
            pending.append(refused)
        problems.append((w.start, f"unsupported heredoc delimiter `{w.raw[:30]}`: bash removes quotes from the "
                                  "delimiter and never runs a substitution in it; the scanner reads a plain or "
                                  "simply quoted word only"))
        return _ShWord(w.start, w.raw, "", j), j

    def skip_region(i, closer, arith):
        """The one step that walks a region whose own words are not commands (an array literal, `${ }`, `[[ ]]`,
        an arithmetic or a skipped `( )` group): the index after its `closer` (`)`, `}` or `]]` as a token). The
        region's words are not recorded, but a command substitution in it (`$( )`, a backtick, `<( )`/`>( )`,
        also inside a double-quoted piece) runs in bash, so it is parsed by `parse_seq` and everything it records
        is kept. i is just after the opener (just after the `[[` word for `]]`).
        `arith`: an arithmetic group, where `<<` is a shift. A backslash-newline is a join, so the character
        before it still decides whether a `#` starts a word; quotes are read by the word reader; a `#` that
        starts a word is a comment (not inside `${ }`); a heredoc operator in a command region (`)` or a
        backtick, never arithmetic) has its body consumed at the next newline; `((`, `${`, `[[` and `{`
        (inside `${ }`) open a nested region walked by this same step."""
        prev = {")": "(", "}": "{"}.get(closer, " ")     # the character before i that is not a join
        command = closer == ")"
        while i < n:
            c = text[i]
            if c == "\\":
                if not text.startswith("\n", i + 1):
                    prev = "x"
                i += 2
                continue
            if c == "\n":
                i += 1
                if pending:
                    i = take_bodies(i)
                prev = "\n"
                continue
            if at_quote(i):
                i = skip_quote(i)
                prev = "'"
                continue
            word_start = prev in _SH_WORD_BREAK
            if c == "#" and closer != "}" and word_start:
                if bodies:
                    i = take_comment(i)
                    continue
            elif (command and not arith and word_start and c == "c" and _SH_CASE_RE.match(text, i)):
                problems.append((i, "a `case` inside a skipped group: its pattern `)` cannot be told from the "
                                    "group's closer"))
            elif (bodies and c == "<" and command and not arith and text.startswith("<<", i)
                    and not text.startswith("<<<", i)
                    and not text.startswith("(", _SH_HEREDOC_OP_RE.match(text, i).end())):
                i = skip_heredoc_op(i)
                prev = " "
                continue
            if c == closer[0] and (closer != "]]" or (
                    word_start and text.startswith("]]", i) and (i + 2 >= n or text[i + 2] in _SH_WORD_BREAK))):
                return i + len(closer)
            if c == "$" and text.startswith("$(", i) and not text.startswith("$((", i):
                i = parse_seq(i + 2, ")")       # a command substitution runs: parsed and recorded
                prev = ")"
                continue
            if c in "<>" and not arith and text.startswith("(", i + 1):
                i = parse_seq(i + 2, ")")       # a process substitution runs too
                prev = ")"
                continue
            if c == "(" and (closer != "}" or text[i - 1] == "$"):
                i = skip_region(i + 1, ")", text.startswith("((", i) or (arith and text[i - 1] != "$"))
                prev = ")"
                continue
            if c == "$" and text.startswith("${", i):
                i = skip_region(i + 2, "}", False)
                prev = "}"
                continue
            if c == "{" and closer == "}":
                i = skip_region(i + 1, "}", False)
                prev = "}"
                continue
            if c == "`":
                i = parse_seq(i + 1, "`")
                prev = "`"
                continue
            if (command and not arith and word_start and text.startswith("[[", i)
                    and text[i + 2:i + 3] in (" ", "\t", "\n")):
                i = skip_region(i + 2, "]]", False)
                prev = "]"
                continue
            prev = c
            i += 1
        return n

    def skip_group(i):      # text[i] == "(": the index after its matching ")"
        return skip_region(i + 1, ")", text.startswith("((", i))

    def read_dq(i, buf):    # i is just after an opening double quote
        while i < n:
            c = text[i]
            if c == "\\":
                if i + 1 < n and text[i + 1] in '$`"\\':
                    buf.append(text[i + 1])
                elif i + 1 < n and text[i + 1] != "\n":
                    buf.append("\\")
                    i -= 1
                elif i + 1 < n:
                    cont.append(i)      # backslash-newline inside double quotes is dropped
                i += 2
                continue
            if c == '"':
                return i + 1
            if text.startswith("$((", i):
                subs[0] += 1
                j = skip_group(i + 1)
                buf.append(text[i:j])
                i = j
            elif text.startswith("$(", i):
                subs[0] += 1
                buf.append("$(...)")
                i = parse_seq(i + 2, ")")
            elif c == "`":
                subs[0] += 1
                buf.append("`...`")
                i = parse_seq(i + 1, "`")
            elif text.startswith("${", i):
                subs[0] += 1
                j = skip_region(i + 2, "}", False)
                buf.append(text[i:j])
                i = j
            else:
                buf.append(c)
                i += 1
        return n

    def read_quoted(i, buf):    # the quoted piece at text[i] ('...', "..." or $'...'): its text into buf, the index after it
        if text[i] == "'":
            j = text.find("'", i + 1)
            buf.append(text[i + 1:] if j < 0 else text[i + 1:j])
            return n if j < 0 else j + 1
        if text[i] == '"':
            return read_dq(i + 1, buf)
        j = i + 2
        while j < n and text[j] != "'":
            j += 2 if text[j] == "\\" else 1
        buf.append(text[i + 2:j])
        return j + 1

    def read_word(i):
        start = i
        buf = []
        plain = True
        while i < n:
            c = text[i]
            if c == "$" and text.startswith('$"', i):
                subs[0] += 1        # a locale-quoted word: the reader takes the `$` as text (a disclosed miss)
            if c == "\\":
                if i + 1 < n and text[i + 1] != "\n":
                    buf.append(text[i + 1])
                    plain = plain and text[i + 1] == "'"
                elif i + 1 < n:
                    cont.append(i)      # backslash-newline outside single quotes is dropped
                i += 2
            elif at_quote(i):
                plain = plain and c == "'"
                if c == "$":
                    subs[0] += 1        # an ANSI-C quote
                i = read_quoted(i, buf)
            elif c == "$" and text.startswith("$((", i):
                plain = False
                subs[0] += 1
                j = skip_group(i + 1)
                buf.append(text[i:j])
                i = j
            elif c == "$" and text.startswith("$(", i):
                plain = False
                subs[0] += 1
                buf.append("$(...)")
                i = parse_seq(i + 2, ")")
            elif c == "$" and text.startswith("${", i):
                plain = False
                subs[0] += 1
                j = skip_region(i + 2, "}", False)
                buf.append(text[i:j])
                i = j
            elif c == "`" and i > start and closers[-1] == "`":
                break           # the closing backtick of the substitution this word is in
            elif c == "`":
                plain = False
                subs[0] += 1
                buf.append("`...`")
                i = parse_seq(i + 1, "`")
            elif c in "<>" and text.startswith("(", i + 1):
                plain = False
                subs[0] += 1
                buf.append(c + "(...)")
                i = parse_seq(i + 2, ")")
            elif c == "(" and re.fullmatch(r"[A-Za-z_]\w*(?:\[[^\]]*\])?\+?=", joined(start, i)):
                plain = False
                j = skip_group(i)           # an array literal: nothing in it is a command word
                buf.append(text[i:j])
                i = j
            elif c in _SH_WORD_BREAK:
                break
            else:
                plain = False
                buf.append(c)
                i += 1
        return _ShWord(start, joined(start, i), "".join(buf), i, plain), i

    def parse_seq(i, closer):
        closers.append(closer)
        try:
            return parse_seq_body(i, closer)
        finally:
            closers.pop()

    def parse_seq_body(i, closer):
        at_cmd = True           # the next word starts a simple command
        swallow = False         # the rest of this simple command is not a command (after `command -v`, `((..))`)
        redir = False           # the next word is a redirection operand
        after_command = False   # the previous word was a bare `command`
        skip_name = False       # the previous word was `function`
        fn_start = 0            # where that `function` keyword stood
        hd_wait = None          # the `<<` whose delimiter word comes next
        pend_decl = None        # the declaration whose body `{` must come next
        braces = []             # the open `{` groups of this sequence: the _ShDecl a function body opened, else None
        cases = []              # open `case`s: 'hdr' (before `in`), 'pat' (patterns), 'body' (commands)
        words = []
        assigns = []
        own_heredocs = []       # the `<<` operators seen in the simple command being read

        def end_cmd():
            nonlocal words, assigns, swallow, redir, after_command
            if words:
                cmd = _ShCmd(tuple(words), tuple(assigns))
                cmds.append(cmd)
                for rec in own_heredocs:
                    rec.cmd = cmd
            own_heredocs.clear()
            words, assigns = [], []
            swallow = redir = after_command = False

        def bad_decl(rest):     # the declaration waiting for its `{` is a form the scanner does not read
            nonlocal pend_decl
            d, pend_decl = pend_decl, None
            d.problem = _SH_DECL_MSG.format(name=d.name, rest=rest[:30])

        def finish():
            end_cmd()
            if pend_decl is not None:
                bad_decl("")
            for d in braces:
                if d is not None:
                    d.problem = "shell function `" + d.name + "` has no closing `}`: the scanner cannot bound its body"

        while i < n:
            c = text[i]
            if c in " \t":
                i += 1
                continue
            if c == "\\" and text.startswith("\\\n", i):
                i += 2
                continue
            if c == "#" and bodies:
                if closer == "`":
                    problems.append((i, "a `#` comment inside a backtick substitution: bash ends the substitution "
                                        "at the next backtick before it reads the comment, which the scanner "
                                        "cannot reproduce"))
                i = take_comment(i)
                continue
            if c == closer and not (closer == ")" and cases and cases[-1] == "pat"):
                finish()
                return i + 1
            if c == ")":
                if cases and cases[-1] == "pat":
                    cases[-1] = "body"
                    end_cmd()
                    at_cmd = True
                i += 1
                continue
            if c in "<>&0123456789{" and not (c in "<>" and text.startswith("(", i + 1)):
                m = _SH_REDIR_RE.match(text, i)
                if m:
                    if re.fullmatch(r"(?:\d+|\{\w+\})?<<-?", m.group(0)):       # not the here-string `<<<`
                        hd_wait = _ShHeredoc(text.index("<<", i), m.group(0).endswith("-"))
                        heredocs.append(hd_wait)
                        own_heredocs.append(hd_wait)
                    redir = True
                    i = m.end()
                    continue
            if c in "\n;&|":
                if c == ";" and cases and cases[-1] == "body" and text.startswith((";;", ";&"), i):
                    cases[-1] = "pat"
                    end_cmd()
                    at_cmd = False
                    i += 3 if text.startswith(";;&", i) else 2
                    continue
                if pend_decl is not None and c != "\n":
                    bad_decl(c)
                end_cmd()
                if not (cases and cases[-1] == "pat"):
                    at_cmd = True
                skip_name = False
                i += 1
                if c == "\n" and pending:
                    i = take_bodies(i)
                continue
            if c == "(":
                if cases and cases[-1] == "pat":
                    # an extglob group (`@(`, `?(`, `*(`, `+(`, `!(`) is part of the pattern: its `)` does not end
                    # the clause; a bare `(` is the optional opener of the pattern list
                    i = skip_group(i) if i and text[i - 1] in "@?*+!" else i + 1
                    continue
                k = i + 1
                while k < n and text[k] in " \t":
                    k += 1
                if k < n and text[k] == ")":    # `()` of a declaration spelled `function f ()`
                    i = k + 1
                    at_cmd = True
                    continue
                if pend_decl is not None:
                    bad_decl("(")
                if at_cmd and text.startswith("((", i):
                    i = skip_group(i)       # an arithmetic command: nothing in it is a command word
                    swallow, at_cmd = True, False
                    continue
                if not at_cmd:
                    # a `(` that does not open a command (`for ((init; cond; step))`, a stray group after
                    # words): nothing in it is a command word
                    i = skip_group(i)
                    continue
                end_cmd()
                i = parse_seq(i + 1, ")")
                swallow, at_cmd = True, False
                continue
            if redir and hd_wait is not None:
                w, i = read_delim(i)
            else:
                w, i = read_word(i)
            if redir:
                redir = False
                if hd_wait is not None:
                    hd_wait.tag = w.text
                    hd_wait.quoted = w.raw != w.text
                    if bodies and w.text:
                        pending.append(hd_wait)
                    hd_wait = None
                continue
            if pend_decl is not None:
                if w.raw == "{":
                    pend_decl.open = w.start
                    braces.append(pend_decl)
                    pend_decl = None
                    at_cmd = True
                    continue
                bad_decl(w.text)
            state = cases[-1] if cases else ""
            if state == "hdr":
                if w.raw == "in":
                    cases[-1] = "pat"
                continue
            if state == "pat":
                if w.raw == "esac":
                    cases.pop()
                    swallow, at_cmd = True, False
                continue
            if swallow:
                continue
            if skip_name:
                skip_name = False
                at_cmd = True
                pend_decl = _ShDecl(w.text, fn_start)
                decls.append(pend_decl)
                continue
            if after_command:
                after_command = False
                if re.match(r"-[vVp]", w.raw):
                    swallow = True
                    continue
            if at_cmd:
                if _SH_ASSIGN_RE.match(w.raw):
                    assigns.append(w.text)
                    continue
                if w.raw == "[[":
                    # a conditional expression: its words and parentheses are not commands
                    i = skip_region(i, "]]", False)
                    swallow, at_cmd = True, False
                    continue
                if w.bare:
                    r = w.raw
                    if r in _SH_KEEP_CMD or r == "{":
                        after_command = r == "command"
                        if r == "{":
                            braces.append(None)
                        continue
                    if r == "case":
                        cases.append("hdr")
                        at_cmd = False
                        continue
                    if r == "esac" and cases:
                        cases.pop()
                        swallow, at_cmd = True, False
                        continue
                    if r == "function":
                        skip_name = True
                        fn_start = w.start
                        continue
                    if r == "}":
                        if braces:
                            d = braces.pop()
                            if d is not None:
                                d.end = w.end
                        swallow, at_cmd = True, False
                        continue
                    k = i
                    while k < n and text[k] in " \t":
                        k += 1
                    if text.startswith("(", k) and text[k + 1:].lstrip(" \t").startswith(")"):
                        pend_decl = _ShDecl(r, w.start)     # `name()`: a declaration, not a call
                        decls.append(pend_decl)
                        i = text.index(")", k) + 1
                        continue
                words.append(w)
                at_cmd = False
            else:
                words.append(w)
        finish()
        return n

    parse_seq(0, "")
    if pending:
        pending[0].closed = False       # an opener on the last line: there is no body to find
    return (tuple(cmds), tuple(heredocs), tuple(comments), tuple(decls),
            tuple(sorted(set(problems))))


def _cmd_strings(cmds, blank=None) -> tuple:
    """The `_sh_command_strings` rendering of parsed commands; a word whose start is a key of `blank` (a python
    `-c '...'` payload) renders as that value, the payload's newlines, as the masked text had it."""
    blank = blank or {}
    out = []
    for cmd in cmds:
        if not cmd.words:
            continue
        line = " ".join(blank.get(w.start, w.text) for w in cmd.words)
        out.append(line)
        if cmd.assigns:
            out.append(" ".join(cmd.assigns + (line,)))
    return tuple(out)


def _cmd_words(cmds) -> set:
    return {cmd.words[0].ident for cmd in cmds if cmd.words and cmd.words[0].ident}


def _sh_command_strings(text: str) -> tuple:
    """Every simple command of `text` (see _sh_parse) rendered as its words joined
    by single spaces, quotes and redirections removed, a `$( )` body rendered as
    its own command. Leading `VAR=value` assignments are REMOVED from the
    rendering, and a second rendering that keeps them is added when there were
    any, so a pattern may name `PYTHONPATH=` deliberately."""
    return _cmd_strings(_sh_parse(text)[0])


def _sh_command_words(text: str) -> set:
    """The identifiers that appear in COMMAND position of `text` (shell with
    comments and heredoc bodies already removed): the first word of a simple
    command after optional `VAR=value` assignments, and after `$(`, a backtick,
    `|`, `&&`, `||`, `;`, `&`, a newline, `!`, `{`, `(`, and the keywords
    if/then/elif/else/while/until/do/time/exec/command/builtin/eval. The word
    must be exactly the identifier, bare or as one quoted piece (`"mc_job"`).
    A word that is exactly the identifier in single or double quotes counts.
    Not command words: arguments (of `type`, `declare -f`, `command -v`,
    `hash`, `unset`, ...), the operand of a redirection, a `case` pattern, the
    contents of `(( ))`, `[[ ]]`, a `for ((...))` header, an extglob group and of an
    array literal (also one whose `name=` is continued over a backslash-newline): the
    regions' own words are not commands, but a command substitution inside them runs in
    Bash and is read, so a call in a `$( )`, backtick or `<( )` inside any of them IS seen;
    a longer word (`mc_job"x"`,
    `mc_job/x`), quoted text that is not the whole word and the name in a `name() {` /
    `function name {` declaration. Launchers (`xargs`, `find -exec`, `sudo`,
    `env`, `timeout`, `coproc`, `command -p`) and a function passed by name are
    not seen."""
    return _cmd_words(_sh_parse(text)[0])



# ---------------------------------------------------------------------------
# Symbol resolution
# ---------------------------------------------------------------------------

def _split_ref(ref):
    path, _, sym = ref.partition("#")
    return path, sym


def resolve_symbol(scan: Scan, ref: str):
    """`(Definition | None, error | None)` for a `path#symbol` reference."""
    path, sym = _split_ref(ref)
    if path not in scan.files:
        return None, f"{ref}: {path} is not under the scan roots (or not a .py/.sh file)"
    hits = [d for d in scan.defs if d.path == path and d.qual == sym and not d.embedded]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        where = ", ".join(str(h.lineno) for h in hits)
        return None, (f"{ref}: defined {len(hits)} times (lines {where}) -- a second implementation of the "
                      "owner's own symbol")
    if scan.files[path] == "sh":
        full = (Path(scan.root) / path).read_text(encoding="utf-8", errors="replace")
        if re.search(r"(?m)^\s*(?:function\s+)?" + re.escape(sym) + r"\b\s*(?:\(\s*\))?\s*[({]?", full) and \
                any(p.startswith(f"{path}:") for p in scan.problems):
            return None, (f"{ref}: the shell scanner could not read this declaration (see the scan problems "
                          "above); supported forms: `name() {` and `function name {`")
    return None, f"{ref}: symbol not found in {path}"


# ---------------------------------------------------------------------------
# Fingerprints (exact_copy)
# ---------------------------------------------------------------------------

class _PyScope:
    """One lexical scope of the function being fingerprinted: the names it
    binds, the names it imports (kept as written), and its `global` /
    `nonlocal` declarations."""

    def __init__(self, kind, parent):
        self.kind = kind  # "module" | "function" | "class" | "comp"
        self.parent = parent
        self.bound = set()
        self.kept = set()
        self.globals = set()
        self.nonlocals = set()


class _Renamer(ast.NodeTransformer):
    """Scope-aware local renaming. Run twice over the same tree: first with
    `binding=True` (collect every scope's bindings), then with it False (rewrite
    each name to a placeholder unique per (scope, binding), in first-appearance
    order). A name resolves to the nearest enclosing scope that binds it (class
    scopes are invisible to the scopes nested in them); otherwise it is external
    and kept as written. `global` names, imports, external names, names bound
    in a class body (attributes) and the parameters of a nested function that a
    call passes by keyword stay as written."""

    def __init__(self, root):
        self.root = root
        self.binding = True
        self.cur = _PyScope("module", None)
        self.scopes = {}
        self.walrus = {}
        self.slots = {}
        # Keyword names at any call site: a nested function's parameter named
        # here may be bound by a caller as written, so it keeps its name.
        self.kwnames = {k.arg for n in ast.walk(root) if isinstance(n, ast.Call)
                        for k in n.keywords if k.arg}

    # -- name resolution -------------------------------------------------
    def _decl(self, name, scope=None):
        scope = scope or self.cur
        if self.binding:
            # A name bound in a class body is an attribute, read from outside as
            # `Result.value`; attribute names are kept as written, so it is too.
            (scope.kept if scope.kind == "class" else scope.bound).add(name)
            return name
        return self._ref(name, scope)

    def _ref(self, name, start=None):
        if self.binding:
            return name
        first = start or self.cur
        s = first
        while s is not None:
            if name in s.globals:
                return name
            if name in s.nonlocals:
                pass
            elif (s is first or s.kind != "class") and (name in s.bound or name in s.kept):
                if name in s.kept:
                    return name
                return self.slots.setdefault((id(s), name), f"v{len(self.slots)}")
            s = s.parent
        return name

    def _scope(self, node, kind):
        if self.binding:
            self.scopes[id(node)] = _PyScope(kind, self.cur)
        return self.scopes[id(node)]

    def _within(self, scope, work):
        outer, self.cur = self.cur, scope
        try:
            return work()
        finally:
            self.cur = outer

    def _args(self, args, nested):
        for a in args.posonlyargs + args.args + args.kwonlyargs + [args.vararg, args.kwarg]:
            if a is not None:
                a.annotation = None
                if nested and a.arg in self.kwnames and self.binding:
                    self.cur.kept.add(a.arg)
                a.arg = self._decl(a.arg)

    def _outer_defaults(self, args):
        args.defaults = [self.visit(d) for d in args.defaults]
        args.kw_defaults = [self.visit(d) if d is not None else None for d in args.kw_defaults]

    # -- scope-opening nodes ----------------------------------------------
    def visit_FunctionDef(self, node):
        if node is not self.root:
            node.name = self._decl(node.name)
        node.returns = None
        node.decorator_list = [self.visit(d) for d in node.decorator_list]
        self._outer_defaults(node.args)

        def inside():
            self._args(node.args, node is not self.root)
            node.body = [self.visit(s) for s in node.body]

        self._within(self._scope(node, "function"), inside)
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self._outer_defaults(node.args)

        def inside():
            self._args(node.args, True)
            node.body = self.visit(node.body)

        self._within(self._scope(node, "function"), inside)
        return node

    def visit_ClassDef(self, node):
        node.name = self._decl(node.name)
        node.decorator_list = [self.visit(d) for d in node.decorator_list]
        node.bases = [self.visit(b) for b in node.bases]
        node.keywords = [self.visit(k) for k in node.keywords]
        self._within(self._scope(node, "class"),
                     lambda: setattr(node, "body", [self.visit(s) for s in node.body]))
        return node

    def _comp(self, node):
        gens = node.generators
        gens[0].iter = self.visit(gens[0].iter)

        def inside():
            for i, g in enumerate(gens):
                g.target = self.visit(g.target)
                if i:
                    g.iter = self.visit(g.iter)
                g.ifs = [self.visit(c) for c in g.ifs]
            if isinstance(node, ast.DictComp):
                node.key = self.visit(node.key)
                node.value = self.visit(node.value)
            else:
                node.elt = self.visit(node.elt)

        self._within(self._scope(node, "comp"), inside)
        return node

    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = _comp

    # -- binders and references ---------------------------------------------
    def visit_Name(self, node):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            node.id = self._decl(node.id, self.walrus.get(id(node)))
        else:
            node.id = self._ref(node.id)
        return node

    def visit_NamedExpr(self, node):
        scope = self.cur
        while scope.kind == "comp":
            scope = scope.parent
        self.walrus[id(node.target)] = scope
        node.value = self.visit(node.value)
        node.target = self.visit(node.target)
        return node

    def visit_ExceptHandler(self, node):
        if node.name:
            node.name = self._decl(node.name)
        return self.generic_visit(node)

    def visit_Import(self, node):
        if self.binding:
            for a in node.names:
                if a.name != "*":
                    self.cur.kept.add((a.asname or a.name).split(".")[0])
        return node

    visit_ImportFrom = visit_Import

    def visit_Global(self, node):
        if self.binding:
            self.cur.globals.update(node.names)
        return node

    def visit_Nonlocal(self, node):
        if self.binding:
            self.cur.nonlocals.update(node.names)
        else:
            node.names = [self._ref(n) for n in node.names]
        return node

    def visit_MatchAs(self, node):
        if node.name:
            node.name = self._decl(node.name)
        return self.generic_visit(node)

    visit_MatchStar = visit_MatchAs

    def visit_MatchMapping(self, node):
        if node.rest:
            node.rest = self._decl(node.rest)
        return self.generic_visit(node)


def py_fingerprint(fn) -> str:
    """Normalised body of a function: its own name, docstrings (at every
    depth), locations, annotations and lexically scoped, consistently renamed
    local bindings are ignored; literals, attributes, operators, decorators,
    imports and names that no enclosing function binds are kept. A name is
    renamed per scope: the same identifier bound in an outer and in a nested
    scope is two bindings."""
    node = _strip_docstring(copy.deepcopy(fn))
    node.name = "_"
    renamer = _Renamer(node)
    renamer.visit(node)
    renamer.binding = False
    renamer.visit(node)
    return ast.dump(node, annotate_fields=True, include_attributes=False)


def sh_fingerprint(d: Definition) -> str:
    """Bounded normalisation of a shell function body: whole-line and trailing
    comments, blank lines, indentation and runs of spaces are ignored; names
    declared with `local` are renamed consistently; the function's own name is
    ignored. Everything else is kept."""
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in d.body]
    lines = [ln for ln in lines if ln]
    text = "\n".join(lines)
    names = []
    for m in re.finditer(r"(?m)(?:^|[;{(]\s*|\s)local\s+([^\n;]*)", text):
        for tok in m.group(1).split():
            nm = tok.split("=", 1)[0]
            if re.fullmatch(r"[A-Za-z_]\w*", nm) and nm not in names:
                names.append(nm)
    for idx, nm in enumerate(names):
        text = re.sub(r"(?<![\w])" + re.escape(nm) + r"(?![\w])", f"@L{idx}", text)
    text = re.sub(r"(?<![\w])" + re.escape(d.name) + r"(?![\w])", "@SELF", text)
    return text


# ---------------------------------------------------------------------------
# Guard methods
# ---------------------------------------------------------------------------

def _variant_syms(row):
    return {v["symbol"] for v in row.get("variants", [])}


def _def_ref(d: Definition) -> str:
    return f"{d.path}#{d.qual}"


def _guard_name_owner(scan, row, owner, params):
    pat = re.compile(params.get("pattern") or ("^" + re.escape(owner.name) + "$"))
    out = []
    if not pat.search(owner.name):
        # a pattern that does not match its own owner protects nothing
        out.append(f"the name pattern {pat.pattern!r} does not match the owner's name {owner.name!r} "
                   f"({_def_ref(owner)}); it would guard nothing")
    allow = list(params.get("allow", []))
    for a in allow:
        _, aerr = resolve_symbol(scan, a)
        if aerr:
            out.append(f"allow entry {aerr}")
    exempt = _variant_syms(row) | set(allow)
    for d in scan.defs:
        if d is owner or not pat.search(d.name):
            continue
        if d.embedded is False and _def_ref(d) in exempt:
            continue
        out.append(f"another definition matches the owner's name pattern {pat.pattern!r}: "
                   f"{d.name} at {d.where()} (the owner is {_def_ref(owner)})")
    return out


def _fp(scan, d):
    key = id(d)
    if key not in scan.fingerprints:
        scan.fingerprints[key] = py_fingerprint(d.node) if d.lang == "py" else sh_fingerprint(d)
    return scan.fingerprints[key]


def _guard_exact_copy(scan, row, owner, params):
    if owner.kind != "func":
        return [f"exact_copy needs a function owner, {_def_ref(owner)} is a constant"]
    exempt = _variant_syms(row)
    out = []
    if owner.lang == "py":
        mine = _fp(scan, owner)
        for d in scan.defs:
            if d is owner or d.lang != "py" or d.kind != "func":
                continue
            if not d.embedded and _def_ref(d) in exempt:
                continue
            if _fp(scan, d) == mine:
                out.append(f"{d.name} at {d.where()} is a renamed or verbatim copy of the owner "
                           f"{_def_ref(owner)} (same normalised body)")
    else:
        mine = _fp(scan, owner)
        for d in scan.defs:
            if d is owner or d.lang != "sh":
                continue
            if _def_ref(d) in exempt:
                continue
            if _fp(scan, d) == mine:
                out.append(f"{d.name} at {d.where()} is a renamed or verbatim copy of the owner "
                           f"{_def_ref(owner)} (same normalised body)")
    return out


def _unit_of(scan, d: Definition):
    """The outermost unit that holds definition `d`."""
    best = None
    for u in scan.units:
        if u.path != d.path or u.lang != d.lang:
            continue
        if u.lineno <= d.lineno and d.end_lineno <= u.end_lineno and (d.embedded == u.qual.startswith("<embedded")):
            if best is None or (u.end_lineno - u.lineno) < (best.end_lineno - best.lineno):
                best = u
    return best


def _guard_token_owner(scan, row, owner, params):
    pat = re.compile(params["pattern"])
    paths = params.get("paths")
    owner_unit = _unit_of(scan, owner)
    if owner_unit is None:
        return [f"token_owner: no unit found for the owner {_def_ref(owner)}"]
    hits = []
    for u in scan.units:
        if paths and not any(u.path == p or u.path.startswith(p.rstrip("/") + "/") for p in paths):
            continue
        if pat.search(u.text):
            hits.append(u)
    out = []
    if owner_unit not in hits:
        out.append(f"the owner {_def_ref(owner)} no longer contains the executable construct {pat.pattern!r} "
                   "(the guard is stale or the owner changed)")
    for u in hits:
        if u is not owner_unit:
            out.append(f"another owner of the construct {pat.pattern!r}: {u.qual} at {u.path}:{u.lineno} "
                       f"(the owner is {_def_ref(owner)})")
    return out


def _guard_delegation(scan, row, owner, params):
    """Every named consumer must EXECUTE the owner: a shell command word, a
    Python call (`f(...)` or `x.f(...)`). A mention (a `type` probe, a bare
    import, quoted text that is not the whole command word -- `"mc_job x"`,
    `mc_job"x"`) does not count; a word that is exactly the name in quotes does.
    A constant owner is reached by a value reference (a Name load) instead."""
    refs = set(params.get("refs") or [owner.name])
    forbid = [(scope, re.compile(rx)) for scope, rx in map(_forbid_entry, params.get("forbid", []))]
    by_value = owner.kind == "const"
    out = []
    declared = {owner.name} | set(params.get("aliases", []))
    variant_names = {_split_ref(v)[1].split(".")[-1] for v in _variant_syms(row)}
    for al in params.get("aliases", []):
        if al not in variant_names:
            out.append(f"alias {al!r} is not the name of one of the row's variants "
                       f"({', '.join(sorted(variant_names)) or 'none'}); an alias is an adapter the row declares")
    for r in sorted(refs - declared):
        out.append(f"ref {r!r} is neither the owner's name {owner.name!r} nor a declared alias "
                   f"(aliases: {', '.join(sorted(params.get('aliases', []))) or 'none'}); the check would "
                   "accept an unrelated call")
    for cons in params["consumers"]:
        path, sym = _split_ref(cons)
        if path not in scan.files:
            out.append(f"consumer {cons}: {path} is not under the scan roots")
            continue
        if sym:
            cd, err = resolve_symbol(scan, cons)
            if err:
                out.append(f"consumer {err}")
                continue
            unit = _unit_of(scan, cd)
            units = [unit] if unit is not None else []
        else:
            units = [u for u in scan.units if u.path == path]
        if not units:
            out.append(f"consumer {cons}: no executable code found")
            continue
        reached = set()
        for u in units:
            reached |= (set(u.loads if by_value else u.calls) & refs)
        if not reached:
            how = "reference" if by_value else "call"
            out.append(f"consumer {cons} does not reach the job through the owner {_def_ref(owner)} "
                       f"(no executable {how} of {', '.join(sorted(refs))}; a mention, a `type` probe or an "
                       "import is not a call)")
        for u in units:
            for scope, f in forbid:
                if scope == "text":
                    hit = f.search(u.text) is not None
                else:
                    hit = any(f.match(c) for c in u.commands)
                if hit:
                    out.append(f"consumer {cons} reaches the job directly, bypassing the owner "
                               f"{_def_ref(owner)}: {scope} pattern {f.pattern!r} found in {u.qual} at "
                               f"{u.path}:{u.lineno}")
    return out


def _literal_strset(node):
    """The frozenset of strings of a literal collection node, or None."""
    elts = None
    if isinstance(node, (ast.Set, ast.Tuple, ast.List)):
        elts = node.elts
    elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
          and node.func.id in ("frozenset", "set", "tuple", "list") and len(node.args) == 1
          and isinstance(node.args[0], (ast.Set, ast.Tuple, ast.List))):
        elts = node.args[0].elts
    if not elts:
        return None
    vals = []
    for e in elts:
        if isinstance(e, ast.Constant) and isinstance(e.value, str):
            vals.append(e.value)
        else:
            return None
    return frozenset(vals)


def _guard_vocab_owner(scan, row, owner, params):
    if owner.kind != "const":
        return [f"vocab_owner needs a constant owner, {_def_ref(owner)} is a function"]
    value = owner.node.value
    mine = _literal_strset(value)
    if not mine or len(mine) < 2:
        return [f"vocab_owner: the owner {_def_ref(owner)} is not a literal collection of at least two strings"]
    exempt_ranges = {(owner.path, owner.lineno, owner.end_lineno)}
    for ref in _variant_syms(row):
        vd, _ = resolve_symbol(scan, ref)
        if vd is not None:
            exempt_ranges.add((vd.path, vd.lineno, vd.end_lineno))
    out = []
    for path, off, tree, embedded in scan.trees:
        for n in ast.walk(tree):
            if getattr(n, "lineno", None) is None or _literal_strset(n) != mine:
                continue
            ln = n.lineno + off
            if any(p == path and a <= ln <= b for p, a, b in exempt_ranges):
                continue
            out.append(f"a literal collection equal to the owner's vocabulary appears at {path}:{ln}"
                       f"{' (embedded python)' if embedded else ''}; reuse {_def_ref(owner)}")
    return out


_GUARDS = {
    "name_owner": _guard_name_owner,
    "exact_copy": _guard_exact_copy,
    "token_owner": _guard_token_owner,
    "delegation": _guard_delegation,
    "vocab_owner": _guard_vocab_owner,
}


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

def _resolve_test_id(root: Path, test: str):
    """Error text when the guard test identifier does not resolve, else None."""
    parts = test.split(".")
    if len(parts) < 3 or len(parts) > 4 or parts[0] != "tests":
        return f"test {test!r} must be tests.<module>.<Class>[.<method>] (nothing after the method)"
    f = root / "tests" / f"{parts[1]}.py"
    if not f.is_file():
        return f"test {test!r}: {f.relative_to(root)} does not exist"
    try:
        tree = ast.parse(f.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        return f"test {test!r}: cannot read {f.relative_to(root)}: {exc}"
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == parts[2]), None)
    if cls is None:
        return f"test {test!r}: class {parts[2]} not found in {f.relative_to(root)}"
    if len(parts) > 3 and not any(isinstance(n, ast.FunctionDef) and n.name == parts[3] for n in cls.body):
        return f"test {test!r}: method {parts[3]} not found in {parts[2]}"
    return None


def check_inventory(root, inv: dict | None = None, scan: Scan | None = None, check_tests=None) -> list:
    """Every finding for the repository at `root`: schema problems, scan
    problems, unresolved symbols and guard failures. Empty means clean."""
    root = Path(root)
    if inv is None:
        try:
            inv = load_inventory(root / INVENTORY_RELPATH)
        except InventoryError as exc:
            return list(exc.errors)
    errs = validate_schema(inv)
    if errs:
        return errs
    if scan is None:
        scan = scan_tree(root, inv["scan_roots"], inv["exclusions"])
    findings = [f"scan: {p}" for p in scan.problems]
    if not scan.defs:
        findings.append("scan: the scan roots matched no definitions (empty scan); check scan_roots and exclusions")
    if check_tests is None:
        check_tests = inv.get("tests_dir", "tests") is not None
    if check_tests and not (root / "tests").is_dir():
        findings.append("tests directory missing: the rows' test ids cannot be resolved (set `tests_dir: null` "
                        "in the inventory to say it has none)")
        check_tests = False
    for row in inv["rows"]:
        rid = row["id"]
        owner, err = resolve_symbol(scan, row["implementation"])
        if err:
            findings.append(f"{rid}: {err}")
            continue
        for v in row["variants"]:
            vd, verr = resolve_symbol(scan, v["symbol"])
            if verr:
                findings.append(f"{rid}: variant {verr}")
        for rp in row["related_paths"]:
            if not any(f"{rp}".rstrip("/") == p or p.startswith(rp.rstrip("/") + "/") or fnmatch.fnmatch(p, rp)
                       for p in scan.files) and not (root / rp.rstrip("/")).exists():
                findings.append(f"{rid}: related path {rp!r} matches nothing")
        if check_tests:
            terr = _resolve_test_id(root, row["test"])
            if terr:
                findings.append(f"{rid}: {terr}")
        for g in row["guards"]:
            for msg in _GUARDS[g["method"]](scan, row, owner, g["params"]):
                findings.append(f"{rid}: {g['method']}: {msg}")
    return findings


# ---------------------------------------------------------------------------
# Rendering and selection (the context pack)
# ---------------------------------------------------------------------------

def render_row(row: dict) -> str:
    """One row as plain deterministic text. A known_debt row is labelled and
    never presented as guarded."""
    debt = row["policy"] == "known_debt"
    lines = [f"[{row['id']}] {row['job']}",
             f"  policy: {row['policy']}" + (" -- KNOWN DEBT, not guarded" if debt else ""),
             f"  owner: {row['implementation']}",
             f"  contract: {row['contract']}"]
    lines.append(f"  {'intended resolution' if debt else 'why single'}: {row['why_single']}")
    for v in row["variants"]:
        lines.append(f"  {'current implementation' if debt else 'variant'}: {v['symbol']} -- {v['contract']} "
                     f"({v['reason']})")
    if debt:
        lines.append("  guards: none (known debt)")
    elif row["guards"]:
        lines.append("  guards: " + "; ".join(
            f"{g['method']}({json.dumps(g['params'], sort_keys=True, ensure_ascii=False)})" for g in row["guards"]))
    else:
        lines.append("  guards: none (a recorded deliberate variant)")
    lines.append(f"  guard misses: {row['coverage_limit']}")
    lines.append(f"  evidence: {row['evidence']}")
    return "\n".join(lines)


def render_rows(rows) -> str:
    return "\n".join(render_row(r) for r in rows)


def select_rows(inv: dict, files) -> list:
    """The rows whose implementation file, variant file or `related_paths`
    match any of the repo-relative `files` (inventory order)."""
    norm = []
    for f in files:
        f = f.replace(os.sep, "/")
        while f.startswith("./"):
            f = f[2:]
        norm.append(f)
    out = []
    for row in inv.get("rows", []):
        paths = [row["implementation"].split("#")[0]] + [v["symbol"].split("#")[0] for v in row.get("variants", [])]
        pats = list(row.get("related_paths", []))
        for f in norm:
            if (f in paths or any(f == p.rstrip("/") or f.startswith(p.rstrip("/") + "/") or fnmatch.fnmatch(f, p)
                                  for p in pats)):
                out.append(row)
                break
    return out


def _one_line_reason(errors, limit=300) -> str:
    """The first few problems on one line (a pack line must stay one line)."""
    shown = [" ".join(str(e).split()) for e in errors[:3]]
    text = "; ".join(shown) + (f" (and {len(errors) - 3} more)" if len(errors) > 3 else "")
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def load_for_code_root(code_root):
    """`(inventory, reason)` for a code repository. `(None, None)`: it carries
    no inventory (absent is silent). `(None, reason)`: the file is present but
    unusable (unreadable, bad JSON, schema errors) -- the caller says so and
    carries on; a pack never fails over it."""
    p = Path(code_root) / INVENTORY_RELPATH
    if not (p.exists() or p.is_symlink()):
        return None, None
    try:
        inv = load_inventory(p)
    except InventoryError as exc:
        return None, _one_line_reason(exc.errors)
    errs = validate_schema(inv)
    if errs:
        return None, _one_line_reason(errs)
    return inv, None


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Check docs/REUSE-INVENTORY.json against the tree.")
    ap.add_argument("root", nargs="?", default=".")
    args = ap.parse_args(argv)
    root = Path(args.root)
    inv = None
    try:
        inv = load_inventory(root / INVENTORY_RELPATH)
    except InventoryError as exc:
        for e in exc.errors:
            print(e)
        return 1
    errs = validate_schema(inv)
    if errs:
        for e in errs:
            print(e)
        return 1
    scan = scan_tree(root, inv["scan_roots"], inv["exclusions"])
    findings = check_inventory(root, inv, scan)
    for f in findings:
        print(f)
    print(f"mc_reuse: {len(inv['rows'])} rows, {len(scan.defs)} definitions, "
          f"{len(scan.files)} files scanned in {scan.seconds:.3f}s: "
          f"{'clean' if not findings else str(len(findings)) + ' finding(s)'}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
