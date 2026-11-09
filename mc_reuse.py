"""mc_reuse.py -- the reuse inventory checker (docs/C7-INVENTORY.json).

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
INVENTORY_RELPATH = "docs/C7-INVENTORY.json"

_INTERP = r"(?:python[\w.]*|\$\{?\w*PY\w*\}?)"
_HEREDOC_RE = re.compile(r"(?<!<)<<(?!<)(-?)\s*(['\"]?)([A-Za-z_]\w*)\2")
_PY_HEREDOC_LINE_RE = re.compile(r"(?i)" + _INTERP + r"\"?\s+-(?:\s|<)")
_PY_INLINE_RE = re.compile(r"(?i)" + _INTERP + r"\"?\s+(?:-\w+\s+)*-c\s+'")
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
        if k not in TOP_FIELDS:
            errs.append(f"inventory: unknown top-level field {k!r}")
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


def _validate_guard_params(label, method, params) -> list:
    errs = []
    allowed = {
        "name_owner": {"pattern", "allow"},
        "exact_copy": set(),
        "token_owner": {"pattern", "paths"},
        "delegation": {"consumers", "refs", "forbid"},
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
    for k in ("allow", "paths", "refs"):
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
            if not _is_str_list(params["forbid"]):
                errs.append(f"{label}: forbid must be a list of regular expressions")
            else:
                for f in params["forbid"]:
                    try:
                        re.compile(f)
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
    refs: frozenset


@dataclass
class Scan:
    root: str
    defs: list = field(default_factory=list)
    units: list = field(default_factory=list)
    trees: list = field(default_factory=list)       # (path, lineno_offset, ast.Module, embedded)
    problems: list = field(default_factory=list)    # unsupported / unparseable production syntax
    unparsed: list = field(default_factory=list)    # (path, lineno, text) embedded payloads that did not parse
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

def _strip_docstring(node):
    body = getattr(node, "body", None)
    if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        node.body = body[1:] or [ast.Pass()]
    return node


def _unit_text(node) -> str:
    n = _strip_docstring(copy.deepcopy(node))
    try:
        return ast.unparse(n)
    except Exception:   # unparse of a hand-built or odd node: fall back to a dump
        return ast.dump(n)


def _refs_of(node) -> frozenset:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                out.add(a.name.split(".")[-1])
                if a.asname:
                    out.add(a.asname)
    return frozenset(out)


def _scan_python(scan, rel, text, line_offset, embedded, prefix):
    """Definitions and units of one Python source (a file or an embedded payload)."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError) as exc:
        if embedded:
            scan.unparsed.append((rel, line_offset + 1, text))
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

    def visit(stmts, stack, top):
        for st in stmts:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                add_def(st, stack)
                visit(st.body, stack + [st.name], False)
            elif isinstance(st, ast.ClassDef):
                visit(st.body, stack + [st.name], False)
            elif isinstance(st, ast.Assign) and not stack:
                for t in st.targets:
                    if isinstance(t, ast.Name):
                        add_def(st, stack, "const", t.id)
            elif isinstance(st, ast.AnnAssign) and not stack and isinstance(st.target, ast.Name) and st.value is not None:
                add_def(st, stack, "const", st.target.id)
            elif isinstance(st, (ast.If, ast.Try, ast.For, ast.While, ast.With)):
                for fld in ("body", "orelse", "finalbody"):
                    visit(getattr(st, fld, []) or [], stack, top)
                for h in getattr(st, "handlers", []) or []:
                    visit(h.body, stack, top)

    visit(tree.body, [], True)

    residual = []

    def unit_for(node, qual):
        scan.units.append(Unit(
            path=rel, qual=prefix + qual, lang="py", lineno=node.lineno + line_offset,
            end_lineno=node.end_lineno + line_offset, text=_unit_text(node), refs=_refs_of(node)))

    for st in tree.body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            unit_for(st, st.name)
        elif isinstance(st, ast.ClassDef):
            for sub in st.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    unit_for(sub, f"{st.name}.{sub.name}")
                else:
                    residual.append(sub)
        else:
            residual.append(st)
    if residual:
        mod = ast.Module(body=copy.deepcopy(residual), type_ignores=[])
        if (mod.body and isinstance(mod.body[0], ast.Expr) and isinstance(mod.body[0].value, ast.Constant)
                and isinstance(mod.body[0].value.value, str)):
            mod.body = mod.body[1:]
        try:
            rtext = ast.unparse(mod)
        except Exception:
            rtext = ast.dump(mod)
        scan.units.append(Unit(path=rel, qual=prefix + "<module>", lang="py", lineno=1 + line_offset,
                               end_lineno=(tree.body[-1].end_lineno if tree.body else 1) + line_offset,
                               text=rtext, refs=_refs_of(ast.Module(body=residual, type_ignores=[]))))


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


def _mask_shell(text: str):
    """`(masked_lines, payloads)`: the file's lines with heredoc bodies and
    python `-c '...'` payload bodies blanked (line numbers preserved), and the
    extracted python payloads as `(first_line_number, source)`."""
    lines = text.split("\n")
    n = len(lines)
    keep = [True] * n
    payloads = []
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
        # single-quoted `<interp> -c '...'` payload
        m = _PY_INLINE_RE.search(code)
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
        hds = list(_HEREDOC_RE.finditer(code))
        if hds:
            j = i + 1
            for hm in hds:
                dash, tag = hm.group(1), hm.group(3)
                start_body = j
                while j < n:
                    cand = lines[j].lstrip("\t") if dash else lines[j]
                    if cand.rstrip() == tag:
                        break
                    j += 1
                if j >= n:
                    break
                body = "\n".join(lines[start_body:j])
                if _PY_HEREDOC_LINE_RE.search(code[:hm.start()]) and hm is hds[0]:
                    payloads.append((start_body + 1, body))
                for k in range(start_body, j + 1):
                    keep[k] = False
                j += 1
            i = max(j, i + 1)
            continue
        i += 1
    masked = [(_strip_shell_comment(lines[k]) if keep[k] else "") for k in range(n)]
    return masked, payloads


def _scan_shell(scan, rel, text):
    masked, payloads = _mask_shell(text)
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
        scan.units.append(Unit(path=rel, qual=name, lang="sh", lineno=s + 1, end_lineno=e + 1,
                               text=body, refs=frozenset(_sh_words(body))))
        covered.update(range(s, e + 1))
    res = "\n".join(masked[k] for k in range(n) if k not in covered)
    scan.units.append(Unit(path=rel, qual="<script>", lang="sh", lineno=1, end_lineno=n, text=res,
                           refs=frozenset(_sh_words(res))))
    for first_line, src in payloads:
        _scan_python(scan, rel, src, first_line - 1, True, f"<embedded L{first_line}>.")


def _sh_words(text):
    return re.findall(r"[A-Za-z_]\w*", text)


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
    exempt = _variant_syms(row) | set(params.get("allow", []))
    out = []
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
    for path, lineno, text in scan.unparsed:
        if pat.search(text) and (not paths or any(path.startswith(p) for p in paths)):
            out.append(f"an embedded python payload at {path}:{lineno} contains {pat.pattern!r} but is not "
                       "parseable; the scanner cannot tell whether it is a second owner")
    return out


def _guard_delegation(scan, row, owner, params):
    refs = set(params.get("refs") or [owner.name])
    forbid = [re.compile(f) for f in params.get("forbid", [])]
    out = []
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
            reached |= (set(u.refs) & refs)
        if not reached:
            out.append(f"consumer {cons} does not reach the job through the owner {_def_ref(owner)} "
                       f"(no reference to {', '.join(sorted(refs))})")
        for u in units:
            for f in forbid:
                m = f.search(u.text)
                if m:
                    out.append(f"consumer {cons} reaches the job directly, bypassing the owner "
                               f"{_def_ref(owner)}: {f.pattern!r} found in {u.qual} at {u.path}:{u.lineno}")
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
    if len(parts) < 3 or parts[0] != "tests":
        return f"test {test!r} must be tests.<module>.<Class>[.<method>]"
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
        check_tests = (root / "tests").is_dir()
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


def load_for_code_root(code_root) -> dict | None:
    """The inventory a code repository carries, or None (absent or unusable;
    a pack never fails over it)."""
    p = Path(code_root) / INVENTORY_RELPATH
    if not p.is_file():
        return None
    try:
        inv = load_inventory(p)
    except InventoryError:
        return None
    return None if validate_schema(inv) else inv


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Check docs/C7-INVENTORY.json against the tree.")
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
