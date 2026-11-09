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

  * function declarations: `name() {` and `function name {` (brace on the same
    line or the next); the body ends at the first line that is exactly `}` at
    the declaration's indentation, or on the declaration line itself for a
    one-liner. `name() (` and other forms are reported, never guessed at.
  * comments: whole-line `#` comments and quote-aware trailing comments are
    dropped from every text the guards read.
  * heredoc bodies are dropped from the shell text. A heredoc fed to a python
    interpreter (`python - <<'EOF'`, `"$MC_PY" - <<'EOF'`) and a single-quoted
    `<interpreter> -c '...'` payload are extracted and scanned as Python.
    A double-quoted `-c "..."` payload and Python held in a shell variable are
    NOT scanned.
"""
from __future__ import annotations

import ast
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
_HEREDOC_RE = re.compile(r"(?<!<)<<(?!<)(-?)\s*(['\"]?)([A-Za-z_]\w*)\2")
_PY_HEREDOC_LINE_RE = re.compile(r"(?i)\"?" + _INTERP + r"\"?\s+-(?:\s|<)")
_PY_INLINE_RE = re.compile(r"(?i)\"?" + _INTERP + r"\"?\s+(?:-\w+\s+)*-c\s+'")
_SH_DECL_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?:function[ \t]+(?P<fname>[A-Za-z_]\w*)[ \t]*(?:\([ \t]*\))?"
    r"|(?P<name>[A-Za-z_]\w*)[ \t]*\([ \t]*\))[ \t]*(?P<rest>.*)$")


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

def _strip_shell_comment(line: str) -> str:
    """`line` without a trailing `#` comment (quote-aware, backslash-aware)."""
    sq = dq = False
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if c == "\\" and not sq:
            i += 2
            continue
        if c == "'" and not dq:
            sq = not sq
        elif c == '"' and not sq:
            dq = not dq
        elif c == "#" and not sq and not dq and (i == 0 or line[i - 1] in " \t;&|(){}"):
            return line[:i].rstrip()
        i += 1
    return line


def _sh_interpreter_starts(cmds) -> list:
    """The offsets of the word that names the program of each parsed command:
    its first word, or the one after `env` and its `VAR=value` arguments."""
    out = []
    for cmd in cmds:
        k = 0
        if cmd.words and cmd.words[0].text == "env":
            k = 1
            while k < len(cmd.words) and _SH_ASSIGN_RE.match(cmd.words[k].raw):
                k += 1
        if k < len(cmd.words):
            out.append(cmd.words[k].start)
    return out


def _mask_shell(text: str):
    """`(masked_lines, payloads, problems)`: the file's lines with heredoc bodies
    and python `-c '...'` payload bodies blanked (line numbers preserved), the
    extracted python payloads as `(first_line_number, source)`, and the
    `(line_number, message)` problems found (an unterminated heredoc)."""
    lines = text.split("\n")
    n = len(lines)
    keep = [True] * n
    payloads = []
    problems = []
    i = 0
    offsets = []
    pos = 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln) + 1
    while i < n:
        raw = lines[i]
        code = _strip_shell_comment(raw)
        if not code.strip():
            i += 1
            continue
        # an interpreter is recognised as a command word only (after assignments, exec, env): a quoted
        # `python3 -c '...'` argument or a quoted `<<` is text
        cmds, hd_offsets = _sh_parse(code)
        starts = _sh_interpreter_starts(cmds)
        # single-quoted `<interp> -c '...'` payload
        m = None
        for st in starts:
            m = _PY_INLINE_RE.match(code, st)
            if m:
                break
        if m:
            start = offsets[i] + m.end()
            buf = []
            p = start
            ok = False
            while True:
                q = text.find("'", p)
                if q < 0:
                    break
                if text.startswith("'\\''", q):
                    buf.append(text[p:q] + "'")
                    p = q + 4
                    continue
                buf.append(text[p:q])
                ok = True
                break
            if ok:
                end_line = text.count("\n", 0, q)
                payloads.append((i + 1, "".join(buf)))
                for j in range(i + 1, end_line + 1):
                    keep[j] = False
                # the opening line keeps only the shell text before the payload
                lines[i] = raw[:m.end()] + "'"
                i = end_line + 1
                continue
        # heredocs
        hds = [hm for hm in _HEREDOC_RE.finditer(code) if hm.start() in hd_offsets]
        if hds:
            j = i + 1
            resume = j
            for hm in hds:
                dash, tag = hm.group(1), hm.group(3)
                start_body = j
                while j < n:
                    cand = lines[j].lstrip("\t") if dash else lines[j]
                    if cand.rstrip() == tag:
                        break
                    j += 1
                if j >= n:
                    problems.append((i + 1, f"unterminated heredoc: `<<{dash}{tag}` opened here is never closed "
                                            f"by a line holding `{tag}`; the scanner cannot tell code from data "
                                            "after it"))
                    j = resume      # resume after the last closed heredoc: nothing more is blanked
                    break
                body_lines = lines[start_body:j]
                if dash:
                    body_lines = [bl.lstrip("\t") for bl in body_lines]   # `<<-` strips leading tabs
                body = "\n".join(body_lines)
                if hm is hds[0] and any(_PY_HEREDOC_LINE_RE.match(code, st, hm.start()) for st in starts):
                    payloads.append((start_body + 1, body))
                for k in range(start_body, j + 1):
                    keep[k] = False
                j += 1
                resume = j
            i = max(j, i + 1)
            continue
        i += 1
    masked = [(_strip_shell_comment(lines[k]) if keep[k] else "") for k in range(n)]
    return masked, payloads, problems


def _scan_shell(scan, rel, text):
    masked, payloads, mask_problems = _mask_shell(text)
    for lineno, msg in mask_problems:
        scan.problems.append(f"{rel}:{lineno}: {msg}")
    n = len(masked)
    funcs = []   # (start, end, name, rest)
    for i in range(n):
        line = masked[i]
        if not line.strip():
            continue
        m = _SH_DECL_RE.match(line)
        if not m:
            continue
        name = m.group("fname") or m.group("name")
        rest = m.group("rest").strip()
        indent = m.group("indent")
        if rest.startswith("("):
            scan.problems.append(f"{rel}:{i + 1}: unsupported shell function form `{name}() (`: the scanner "
                                 "reads `name() {` and `function name {` only")
            continue
        has_open = rest.startswith("{")
        if not has_open and not rest:
            # brace on a following line?
            k = i + 1
            while k < n and not masked[k].strip():
                k += 1
            if k < n and masked[k].strip() == "{":
                has_open = True
                rest = ""
        if not has_open:
            # `name() command` (no brace) or a command that merely looks like a declaration
            if rest:
                scan.problems.append(f"{rel}:{i + 1}: unsupported shell function form `{name}() {rest[:30]}`: "
                                     "the scanner reads `name() {` and `function name {` only")
            continue
        after = rest[1:].strip() if rest.startswith("{") else ""
        if after.endswith("}") and rest.startswith("{") and after.count("}") >= 1 and ";" in after + ";":
            end = i
            body = [after[:-1].strip()]
        else:
            end = None
            for j in range(i + 1, n):
                if masked[j].rstrip() == indent + "}":
                    end = j
                    break
            if end is None:
                scan.problems.append(f"{rel}:{i + 1}: shell function `{name}` has no closing `}}` at its own "
                                     "indentation; the scanner cannot bound its body")
                continue
            body = ([after] if after else []) + masked[i + 1:end]
        funcs.append((i, end, name))
        scan.defs.append(Definition(path=rel, name=name, qual=name, lang="sh", kind="func",
                                    lineno=i + 1, end_lineno=end + 1, body=body))
    # units: outermost functions + residual
    outer = []
    for (s, e, name) in sorted(funcs):
        if outer and s <= outer[-1][1]:
            continue
        outer.append((s, e, name))
    covered = set()
    for s, e, name in outer:
        body = "\n".join(masked[s:e + 1])
        cw = frozenset(_sh_command_words(body))
        scan.units.append(Unit(path=rel, qual=name, lang="sh", lineno=s + 1, end_lineno=e + 1,
                               text=body, calls=cw, loads=cw, commands=_sh_command_strings(body)))
        covered.update(range(s, e + 1))
    res = "\n".join(masked[k] for k in range(n) if k not in covered)
    cw = frozenset(_sh_command_words(res))
    scan.units.append(Unit(path=rel, qual="<script>", lang="sh", lineno=1, end_lineno=n, text=res,
                           calls=cw, loads=cw, commands=_sh_command_strings(res)))
    for first_line, src in payloads:
        _scan_python(scan, rel, src, first_line - 1, True, f"<embedded L{first_line}>.")


_SH_KEEP_CMD = frozenset({"if", "then", "elif", "else", "while", "until", "do", "time", "exec", "command",
                          "builtin", "eval", "!"})
_SH_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SH_ASSIGN_RE = re.compile(r"[A-Za-z_]\w*(?:\[[^\]]*\])?\+?=")
_SH_REDIR_RE = re.compile(r"(?:\d+|\{\w+\})?(?:<<<|<<-?|>>|>\||>&|<&|<>|&>>|&>|>|<)")


@dataclass(frozen=True)
class _ShWord:
    start: int      # offset of the word in the parsed text
    raw: str        # the source text of the word
    text: str       # the word with quotes and escapes removed (a substitution renders as `$(...)`)

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


@functools.lru_cache(maxsize=64)
def _sh_parse(text: str):
    """`(commands, heredoc_offsets)` of `text` (shell with comments and heredoc
    bodies already removed): every simple command -- including the ones inside
    `$( )`, backticks, `<( )` and `( )` -- and the offsets of `<<` / `<<-`
    operators that sit outside quotes. A bounded tokenizer, not a Bash parser:
    words, quotes, `$(( ))`/`(( ))` blobs, redirections (an operator consumes
    its operand word), `case` patterns and `name() {` declarations are told
    apart from commands; `eval`, `exec`, `time`, `!` and the like keep command
    position, `command -v/-V/-p` do not."""
    n = len(text)
    cmds = []
    heredocs = []

    def skip_group(i):      # text[i] == "(": the index after its matching ")"
        depth = 0
        while i < n:
            c = text[i]
            if c == "\\":
                i += 2
                continue
            if c in "'\"":
                j = text.find(c, i + 1)
                i = n if j < 0 else j + 1
                continue
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        return n

    def skip_brace(i):      # text[i] == "{": the index after its matching "}"
        depth = 0
        while i < n:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        return n

    def read_dq(i, buf):    # i is just after an opening double quote
        while i < n:
            c = text[i]
            if c == "\\":
                if i + 1 < n and text[i + 1] in '$`"\\':
                    buf.append(text[i + 1])
                elif i + 1 < n and text[i + 1] != "\n":
                    buf.append("\\")
                    i -= 1
                i += 2
                continue
            if c == '"':
                return i + 1
            if text.startswith("$((", i):
                j = skip_group(i + 1)
                buf.append(text[i:j])
                i = j
            elif text.startswith("$(", i):
                buf.append("$(...)")
                i = parse_seq(i + 2, ")")
            elif c == "`":
                buf.append("`...`")
                i = parse_seq(i + 1, "`")
            elif text.startswith("${", i):
                j = skip_brace(i + 1)
                buf.append(text[i:j])
                i = j
            else:
                buf.append(c)
                i += 1
        return n

    def read_word(i):
        start = i
        buf = []
        while i < n:
            c = text[i]
            if c == "\\":
                if i + 1 < n and text[i + 1] != "\n":
                    buf.append(text[i + 1])
                i += 2
            elif c == "'":
                j = text.find("'", i + 1)
                buf.append(text[i + 1:] if j < 0 else text[i + 1:j])
                i = n if j < 0 else j + 1
            elif c == '"':
                i = read_dq(i + 1, buf)
            elif c == "$" and text.startswith("$'", i):
                j = i + 2
                while j < n and text[j] != "'":
                    j += 2 if text[j] == "\\" else 1
                buf.append(text[i + 2:j])
                i = j + 1
            elif c == "$" and text.startswith("$((", i):
                j = skip_group(i + 1)
                buf.append(text[i:j])
                i = j
            elif c == "$" and text.startswith("$(", i):
                buf.append("$(...)")
                i = parse_seq(i + 2, ")")
            elif c == "$" and text.startswith("${", i):
                j = skip_brace(i + 1)
                buf.append(text[i:j])
                i = j
            elif c == "`":
                buf.append("`...`")
                i = parse_seq(i + 1, "`")
            elif c in "<>" and text.startswith("(", i + 1):
                buf.append(c + "(...)")
                i = parse_seq(i + 2, ")")
            elif c == "(" and re.fullmatch(r"[A-Za-z_]\w*\+?=", text[start:i]):
                j = skip_group(i)           # an array literal: nothing in it is a command word
                buf.append(text[i:j])
                i = j
            elif c in " \t\n;&|()<>":
                break
            else:
                buf.append(c)
                i += 1
        return _ShWord(start, text[start:i], "".join(buf)), i

    def parse_seq(i, closer):
        at_cmd = True           # the next word starts a simple command
        swallow = False         # the rest of this simple command is not a command (after `command -v`, `((..))`)
        redir = False           # the next word is a redirection operand
        after_command = False   # the previous word was a bare `command`
        skip_name = False       # the previous word was `function`
        cases = []              # open `case`s: 'hdr' (before `in`), 'pat' (patterns), 'body' (commands)
        words = []
        assigns = []

        def end_cmd():
            nonlocal words, assigns, swallow, redir, after_command
            if words:
                cmds.append(_ShCmd(tuple(words), tuple(assigns)))
            words, assigns = [], []
            swallow = redir = after_command = False

        while i < n:
            c = text[i]
            if c in " \t":
                i += 1
                continue
            if c == "\\" and text.startswith("\\\n", i):
                i += 2
                continue
            if c == closer and not (closer == ")" and cases and cases[-1] == "pat"):
                end_cmd()
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
                    if m.group(0).endswith("<<") or m.group(0).endswith("<<-"):
                        heredocs.append(text.index("<<", i))
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
                end_cmd()
                if not (cases and cases[-1] == "pat"):
                    at_cmd = True
                skip_name = False
                i += 1
                continue
            if c == "(":
                if cases and cases[-1] == "pat":
                    i += 1
                    continue
                if at_cmd and text.startswith("((", i):
                    i = skip_group(i)       # an arithmetic command: nothing in it is a command word
                    swallow, at_cmd = True, False
                    continue
                k = i + 1
                while k < n and text[k] in " \t":
                    k += 1
                if k < n and text[k] == ")":    # `()` of a declaration spelled `function f ()`
                    i = k + 1
                    at_cmd = True
                    continue
                end_cmd()
                i = parse_seq(i + 1, ")")
                swallow, at_cmd = True, False
                continue
            w, i = read_word(i)
            if redir:
                redir = False
                continue
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
                if w.bare:
                    r = w.raw
                    if r in _SH_KEEP_CMD or r == "{":
                        after_command = r == "command"
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
                        continue
                    if r == "}":
                        swallow, at_cmd = True, False
                        continue
                    k = i
                    while k < n and text[k] in " \t":
                        k += 1
                    if text.startswith("(", k) and text[k + 1:].lstrip(" \t").startswith(")"):
                        i = text.index(")", k) + 1      # `name()`: a declaration, not a call
                        continue
                words.append(w)
                at_cmd = False
            else:
                words.append(w)
        end_cmd()
        return n

    parse_seq(0, "")
    return tuple(cmds), tuple(heredocs)


def _sh_command_strings(text: str) -> tuple:
    """Every simple command of `text` (see _sh_parse) rendered as its words joined
    by single spaces, quotes and redirections removed, a `$( )` body rendered as
    its own command. Leading `VAR=value` assignments are REMOVED from the
    rendering, and a second rendering that keeps them is added when there were
    any, so a pattern may name `PYTHONPATH=` deliberately."""
    out = []
    for cmd in _sh_parse(text)[0]:
        if not cmd.words:
            continue
        line = " ".join(w.text for w in cmd.words)
        out.append(line)
        if cmd.assigns:
            out.append(" ".join(cmd.assigns + (line,)))
    return tuple(out)


def _sh_command_words(text: str) -> set:
    """The identifiers that appear in COMMAND position of `text` (shell with
    comments and heredoc bodies already removed): the first word of a simple
    command after optional `VAR=value` assignments, and after `$(`, a backtick,
    `|`, `&&`, `||`, `;`, `&`, a newline, `!`, `{`, `(`, and the keywords
    if/then/elif/else/while/until/do/time/exec/command/builtin/eval. The word
    must be exactly the identifier, bare or as one quoted piece (`"mc_job"`).
    Not command words: arguments (of `type`, `declare -f`, `command -v`,
    `hash`, `unset`, ...), the operand of a redirection, a `case` pattern, the
    contents of `(( ))` and of an array literal, a longer word (`mc_job"x"`,
    `mc_job/x`), single-quoted text and the name in a `name() {` /
    `function name {` declaration. Launchers (`xargs`, `find -exec`, `sudo`,
    `env`, `timeout`, `coproc`, `command -p`), a function passed by name and a
    `$( )` nested inside `$(( ))` are not seen."""
    return {cmd.words[0].ident for cmd in _sh_parse(text)[0] if cmd.words and cmd.words[0].ident}



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

class _Renamer(ast.NodeTransformer):
    def __init__(self, local_names):
        self.locals = local_names
        self.map = {}

    def _name(self, ident):
        if ident not in self.locals:
            return ident
        if ident not in self.map:
            self.map[ident] = f"v{len(self.map)}"
        return self.map[ident]

    def visit_Name(self, node):
        node.id = self._name(node.id)
        return node

    def visit_arg(self, node):
        node.arg = self._name(node.arg)
        node.annotation = None
        return node

    def visit_ExceptHandler(self, node):
        if node.name:
            node.name = self._name(node.name)
        return self.generic_visit(node)

    def visit_FunctionDef(self, node):
        node.name = self._name(node.name)
        node.returns = None
        return self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_AnnAssign(self, node):
        return self.generic_visit(node)


def py_fingerprint(fn) -> str:
    """Normalised body of a function: its own name, docstring, locations,
    annotations and consistently renamed local bindings are ignored; literals,
    attributes, operators, decorators, imports and global names are kept."""
    node = _strip_docstring(copy.deepcopy(fn))
    locals_ = set()
    declared_global = set()
    imported = set()
    for n in ast.walk(node):
        if isinstance(n, ast.arg):
            locals_.add(n.arg)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            locals_.add(n.id)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            locals_.add(n.name)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not node:
            locals_.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            declared_global.update(n.names)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                imported.add((a.asname or a.name).split(".")[0])
    locals_ -= declared_global | imported
    node.name = "_"
    renamed = _Renamer(locals_).visit(node)
    return ast.dump(renamed, annotate_fields=True, include_attributes=False)


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
    import, a string) does not count. A constant owner is reached by a value
    reference (a Name load) instead."""
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
