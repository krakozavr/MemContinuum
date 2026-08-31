#!/usr/bin/env python
"""memlint.py -- docs/SCHEMA.md section 7 linter for Store D markdown.

Rules implemented (exactly the set enumerated in the Store D build brief,
a subset of docs/SCHEMA.md section 7 -- the append-only git-hash-mismatch rule is
out of scope here; see README for why):

  * a link whose ruling.authority is owner-verbatim/owner-ratified with no
    ruling.text and/or ruling.source            -> error
  * a link with status: superseded and no superseded_by                -> error
  * a link with reverses: set and no reason_for_change                 -> error
  * frontmatter `current`, if present, must equal the newest link whose
    status is active; mismatch names the correct value                -> error
  * a topic in area processing/* or deletion/* with no code_refs       -> warning
  * any status / authority / kind value outside the five/five/five
    enumerated in docs/SCHEMA.md section 3                                  -> error

Exit 1 if any error was found anywhere under ROOT (warnings alone -> exit 0).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from memidx import (
    AUTHORITIES,
    EDGE_RELS,
    KINDS,
    STATUSES,
    declared_symbol_names,
    newest_active_link,
    parse_frontmatter,
    walk_markdown,
)

# ---------------------------------------------------------------------------
# concept-rule helpers (Anatomy's intent index -- memidx.py's code-reindex/
# code-search sibling additions to the concept-record rules below)
# ---------------------------------------------------------------------------

_NOT_THIS_RE = re.compile(r"\bNOT\b|not this concept|Does NOT")


def _declared_symbols(text: str) -> set[str]:
    """Finding 5: every #symbol fragment memlint accepts must be something
    code-search/code-reindex would actually recognize -- so this reuses
    the chunker's own lexer-aware scan (memidx.declared_symbol_names)
    rather than a from-scratch regex, which used to miss init, subscript,
    computed var names, and backtick-quoted names, AND could false-positive
    on a name that only ever appeared inside a comment or string literal
    (a regex has no notion of "inside a comment" at all -- the chunker's
    mask already blanks those out)."""
    return declared_symbol_names(text)


def lint_topic(path: Path, fm: dict) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    links = fm.get("links") or []

    for link in links:
        name = link.get("link", "?")
        prefix = f"{path}:{name}"

        status = link.get("status")
        if status is not None and status not in STATUSES:
            errors.append(f"{prefix}: unknown status {status!r} (must be one of {sorted(STATUSES)})")

        kind = link.get("kind")
        if kind is not None and kind not in KINDS:
            errors.append(f"{prefix}: unknown kind {kind!r} (must be one of {sorted(KINDS)})")

        ruling = link.get("ruling") or {}
        auth = ruling.get("authority")
        if auth is not None and auth not in AUTHORITIES:
            errors.append(f"{prefix}: unknown ruling.authority {auth!r} (must be one of {sorted(AUTHORITIES)})")

        if auth in ("owner-verbatim", "owner-ratified"):
            missing = [f for f in ("text", "source") if not ruling.get(f)]
            if missing:
                errors.append(
                    f"{prefix}: {auth} ruling missing required field(s) {missing} "
                    f"(owner-verbatim/owner-ratified rulings must carry ruling.text and ruling.source)"
                )

        if status == "superseded" and not link.get("superseded_by"):
            errors.append(f"{prefix}: status: superseded but no superseded_by")

        if link.get("reverses") and not link.get("reason_for_change"):
            errors.append(f"{prefix}: reverses {link['reverses']!r} but no reason_for_change")

        # docs/SCHEMA.md.1 addendum SS1: edges{}.rel must be one of the seven
        # enumerated relations.
        for edge in link.get("edges") or []:
            rel = edge.get("rel")
            if rel is not None and rel not in EDGE_RELS:
                errors.append(
                    f"{prefix}: unknown edge rel {rel!r} (must be one of {sorted(EDGE_RELS)})"
                )

    current_field = fm.get("current")
    if current_field is not None:
        expected_link = newest_active_link(links)
        expected_id = expected_link.get("link") if expected_link else None
        if current_field != expected_id:
            errors.append(
                f"{path}: current: {current_field!r} does not match the newest active link "
                f"-- should be {expected_id!r}"
            )

    area = str(fm.get("area") or "")
    if (area.startswith("processing/") or area.startswith("deletion/")) and not fm.get("code_refs"):
        warnings.append(f"{path}: topic in area {area!r} has no code_refs")

    return errors, warnings


def lint_concept(
    path: Path,
    fm: dict,
    code_root: Path | None,
    body: str = "",
    known_topic_ids: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """docs/SCHEMA.md.1 addendum SS4: type: concept records.

    - implemented_by/tested_by path that doesn't exist on disk (relative to
      code_root, "#symbol" fragment stripped) -> error. Skipped entirely
      when code_root is not given (existence isn't checkable without one).
    - a "#symbol" fragment that doesn't actually match a func/struct/enum/
      class/subscript/static-func declared in that file -> error (the
      fragment used to be stripped and never checked).
    - implemented_by WITHOUT a "#symbol" fragment on a file over 400
      lines -> error (an unqualified claim on a large file is too vague to
      be useful -- narrow it to a symbol).
    - governed_by referencing a topic id not found anywhere in the linted
      corpus -> error. Only enforced when known_topic_ids is given AND
      non-empty (a corpus with zero topic records has no registry to
      validate against -- same "skip when unverifiable" pattern as the
      code_root-less path-existence check).
    - no tested_by entries -> warning (promotion needs at least one).
    - concept body with no "not this concept" sentence (what this concept
      is explicitly NOT) -> warning.
    """
    errors: list[str] = []
    warnings: list[str] = []
    cid = fm.get("id") or path.stem

    if code_root is not None:
        for field in ("implemented_by", "tested_by"):
            for ref in fm.get(field) or []:
                ref_str = str(ref)
                ref_path, _, frag = ref_str.partition("#")
                full = code_root / ref_path
                if not full.exists():
                    errors.append(
                        f"{path}: {cid} {field} path {ref_path!r} does not exist under {code_root}"
                    )
                    continue
                if frag:
                    try:
                        declared = _declared_symbols(full.read_text(encoding="utf-8", errors="ignore"))
                    except OSError:
                        declared = set()
                    if frag not in declared:
                        errors.append(
                            f"{path}: {cid} {field} fragment {frag!r} is not a func/struct/enum/"
                            f"class/subscript declared in {ref_path!r}"
                        )
                elif field == "implemented_by":
                    try:
                        with full.open(encoding="utf-8", errors="ignore") as fh:
                            line_count = sum(1 for _ in fh)
                    except OSError:
                        line_count = 0
                    if line_count > 400:
                        errors.append(
                            f"{path}: {cid} implemented_by {ref_path!r} has no #symbol fragment and "
                            f"is {line_count} lines (>400) -- narrow the claim to a specific symbol"
                        )

    if known_topic_ids:
        for gid in fm.get("governed_by") or []:
            if gid not in known_topic_ids:
                errors.append(f"{path}: {cid} governed_by references unknown topic id {gid!r}")

    if not fm.get("tested_by"):
        warnings.append(f"{path}: concept {cid} has no tested_by (required before promotion)")

    if not _NOT_THIS_RE.search(body):
        warnings.append(
            f"{path}: concept {cid} body has no \"not this concept\" sentence "
            f"(state what this concept explicitly is NOT)"
        )

    return errors, warnings


def lint_record(path: Path, fm: dict) -> tuple[list[str], list[str]]:
    """Enum-validate a standalone (non-topic) record's top-level status/authority."""
    errors: list[str] = []
    status = fm.get("status")
    if status is not None and status not in STATUSES:
        errors.append(f"{path}: unknown status {status!r} (must be one of {sorted(STATUSES)})")
    authority = fm.get("authority")
    if authority is not None and authority not in AUTHORITIES:
        errors.append(f"{path}: unknown authority {authority!r} (must be one of {sorted(AUTHORITIES)})")
    return errors, []


def lint_file(
    path: Path,
    code_root: Path | None = None,
    known_topic_ids: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    fm, body = parse_frontmatter(path)
    if fm.get("type") == "concept":
        return lint_concept(path, fm, code_root, body=body, known_topic_ids=known_topic_ids)
    is_topic = bool(fm.get("links")) or fm.get("type") == "topic"
    if is_topic:
        return lint_topic(path, fm)
    return lint_record(path, fm)


def _duplicate_claim_errors(root: Path) -> list[str]:
    """docs/SCHEMA.md.1 addendum SS4 extension: two concepts must never
    both claim the same implemented_by "path#symbol" -- that's not two
    concepts sharing ownership, it's an unresolved ambiguity about which
    one actually implements it. tested_by is deliberately excluded (many
    concepts legitimately share a test file). Message avoids concatenating
    "path#symbol" as one literal substring so it can't be mistaken for
    (or collide with an assertion aimed at) the path-existence checks
    above, which do use that exact concatenation."""
    claims: dict[str, list[tuple[str, Path]]] = {}
    for f in sorted(walk_markdown(root)):
        fm, _body = parse_frontmatter(f)
        if fm.get("type") != "concept":
            continue
        cid = fm.get("id") or f.stem
        for ref in fm.get("implemented_by") or []:
            ref_str = str(ref)
            if "#" not in ref_str:
                continue
            ref_path, _, frag = ref_str.partition("#")
            claims.setdefault(f"{ref_path}\x00{frag}", []).append((cid, f))

    errors = []
    for key, owners in claims.items():
        if len(owners) < 2:
            continue
        ref_path, frag = key.split("\x00", 1)
        owner_desc = ", ".join(f"{cid} ({fp})" for cid, fp in owners)
        errors.append(
            f"duplicate implemented_by claim on {ref_path!r} symbol {frag!r}: {owner_desc}"
        )
    return errors


def lint_root(root: Path, code_root: Path | None = None) -> tuple[list[str], list[str]]:
    known_topic_ids: set[str] = set()
    for f in sorted(walk_markdown(root)):
        fm, _body = parse_frontmatter(f)
        is_topic = bool(fm.get("links")) or fm.get("type") == "topic"
        if is_topic:
            tid = fm.get("id") or f.stem
            known_topic_ids.add(str(tid))

    all_errors: list[str] = []
    all_warnings: list[str] = []
    for f in sorted(walk_markdown(root)):
        errors, warnings = lint_file(f, code_root, known_topic_ids=known_topic_ids)
        all_errors.extend(errors)
        all_warnings.extend(warnings)
    all_errors.extend(_duplicate_claim_errors(root))
    return all_errors, all_warnings


def parse_argv(argv: list[str]) -> tuple[str | None, str | None]:
    """ROOT positional + optional --code-root PATH, in either order."""
    root = None
    code_root = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--code-root":
            i += 1
            code_root = argv[i] if i < len(argv) else None
        elif root is None:
            root = a
        i += 1
    return root, code_root


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: memlint.py ROOT [--code-root PATH]", file=sys.stderr)
        return 2
    root_str, code_root_str = parse_argv(argv)
    if not root_str:
        print("usage: memlint.py ROOT [--code-root PATH]", file=sys.stderr)
        return 2
    root = Path(root_str).resolve()
    code_root = Path(code_root_str).resolve() if code_root_str else None
    errors, warnings = lint_root(root, code_root)
    for w in warnings:
        print(f"WARNING: {w}")
    for e in errors:
        print(f"ERROR: {e}")
    if errors:
        print(f"memlint: {len(errors)} error(s), {len(warnings)} warning(s)")
        return 1
    print(f"memlint: clean ({len(warnings)} warning(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
