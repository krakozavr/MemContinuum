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

import sys
from pathlib import Path

from memidx import (
    AUTHORITIES,
    EDGE_RELS,
    KINDS,
    STATUSES,
    newest_active_link,
    parse_frontmatter,
    walk_markdown,
)


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


def lint_concept(path: Path, fm: dict, code_root: Path | None) -> tuple[list[str], list[str]]:
    """docs/SCHEMA.md.1 addendum SS4: type: concept records.

    - implemented_by/tested_by path that doesn't exist on disk (relative to
      code_root, "#symbol" fragment stripped) -> error. Skipped entirely
      when code_root is not given (existence isn't checkable without one).
    - no tested_by entries -> warning (promotion needs at least one).
    """
    errors: list[str] = []
    warnings: list[str] = []
    cid = fm.get("id") or path.stem

    if code_root is not None:
        for field in ("implemented_by", "tested_by"):
            for ref in fm.get(field) or []:
                ref_path = str(ref).split("#", 1)[0]
                if not (code_root / ref_path).exists():
                    errors.append(
                        f"{path}: {cid} {field} path {ref_path!r} does not exist under {code_root}"
                    )

    if not fm.get("tested_by"):
        warnings.append(f"{path}: concept {cid} has no tested_by (required before promotion)")

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


def lint_file(path: Path, code_root: Path | None = None) -> tuple[list[str], list[str]]:
    fm, _body = parse_frontmatter(path)
    if fm.get("type") == "concept":
        return lint_concept(path, fm, code_root)
    is_topic = bool(fm.get("links")) or fm.get("type") == "topic"
    if is_topic:
        return lint_topic(path, fm)
    return lint_record(path, fm)


def lint_root(root: Path, code_root: Path | None = None) -> tuple[list[str], list[str]]:
    all_errors: list[str] = []
    all_warnings: list[str] = []
    for f in sorted(walk_markdown(root)):
        errors, warnings = lint_file(f, code_root)
        all_errors.extend(errors)
        all_warnings.extend(warnings)
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
