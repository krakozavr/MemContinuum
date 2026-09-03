#!/usr/bin/env python
"""memlint.py -- docs/SCHEMA.md section 7 linter for MemContinuum store markdown.

Rules implemented, a subset of docs/SCHEMA.md section 7 -- the append-only
git-hash-mismatch rule is deliberately out of scope (see that section: the
check belongs where a canonical store's commits are made, not in the linter):

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
    INVARIANT_KINDS,
    KINDS,
    STATUSES,
    fragment_declared_in_text,
    newest_active_link,
    parse_frontmatter,
    validated_evidence_list,
    walk_markdown,
)

# ---------------------------------------------------------------------------
# concept-rule helpers (Anatomy's intent index -- memidx.py's code-reindex/
# code-search sibling additions to the concept-record rules below)
# ---------------------------------------------------------------------------

_NOT_THIS_RE = re.compile(r"\bNOT\b|not this concept|Does NOT")


def _symbol_declared(frag: str, text: str, rel_path: str) -> bool:
    """Finding 5 (init/subscript/computed var/backtick names) AND finding 4
    (a QUALIFIED fragment, e.g. "Outer.outerFunc", must validate exactly
    like code-search's runtime concept attachment accepts it): this reuses
    memidx.fragment_declared_in_text -- the SAME single-source-of-truth
    predicate concept_matches_for_chunk uses at attach time -- rather than
    a from-scratch regex or a flattened bare-name set. Also never
    false-positives on a name that only appears inside a comment or string
    literal (the chunker's mask already blanks those out).

    `rel_path` (the record's own ref_path when the caller has one) is
    forwarded to fragment_declared_in_text, which resolves the file's
    language from it and asks THAT backend's own `declared_symbols` --
    so a "#symbol" fragment on a Python implemented_by/tested_by path is
    checked against Python's vocabulary, not Swift's, with no
    language-specific branch anywhere on this path."""
    return fragment_declared_in_text(frag, text, rel_path=rel_path)


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

        # F3 (external-fix round, coordinator ruling 70): rationale/
        # alternatives carry their own authority field, unchecked until now.
        rationale = link.get("rationale") or {}
        rauth = rationale.get("authority")
        if rauth is not None and rauth not in AUTHORITIES:
            errors.append(f"{prefix}: unknown rationale.authority {rauth!r} (must be one of {sorted(AUTHORITIES)})")

        for alt in link.get("alternatives") or []:
            aauth = alt.get("authority")
            if aauth is not None and aauth not in AUTHORITIES:
                errors.append(f"{prefix}: unknown alternatives[].authority {aauth!r} (must be one of {sorted(AUTHORITIES)})")

        # F3: an invariant's own kind/pattern must be checkable, and its
        # enforceability under the trust model is validated here too --
        # drift's runtime classifier (invariant_enforcement_class) applies
        # the same rule, but a bad invariant should never reach a real
        # `drift` run silently in the first place.
        invariant = link.get("invariant")
        if invariant:
            ikind = invariant.get("kind")
            if ikind not in INVARIANT_KINDS:
                errors.append(f"{prefix}: unknown invariant.kind {ikind!r} (must be one of {sorted(INVARIANT_KINDS)})")
            ipattern = invariant.get("pattern")
            if ipattern:
                try:
                    re.compile(ipattern)
                except re.error as exc:
                    errors.append(f"{prefix}: invariant.pattern {ipattern!r} does not compile: {exc}")
            # Ruling 76 (overrides this task's original agent-inference
            # exclusion): agent-inference is HOLD-eligible exactly like
            # reviewer-finding/code-derived -- validated evidence makes it
            # a HOLD, not an error; empty evidence gets the same ERROR
            # every other non-CONSTRAINT authority gets below. No more
            # special-cased always-error branch.
            if auth not in ("owner-verbatim", "owner-ratified"):
                validated = validated_evidence_list(link.get("evidence"))
                if not validated:
                    errors.append(
                        f"{prefix}: invariant present but authority {auth!r} is not CONSTRAINT and "
                        "evidence has no validated (non-blank) content -- a non-CONSTRAINT invariant "
                        "only enforces as a HOLD with real evidence, and even then only under --strict-holds"
                    )

    topic_link_ids = {str(l.get("link")) for l in links if l.get("link")}
    seen_link_ids: dict[str, int] = {}
    for link in links:
        lid = str(link.get("link") or "")
        if lid:
            seen_link_ids[lid] = seen_link_ids.get(lid, 0) + 1
        rev = link.get("reverses")
        if rev and str(rev) not in topic_link_ids:
            errors.append(f"{path}:{link.get('link','?')}: reverses {rev!r} does not match any link id in this topic")
        sb = link.get("superseded_by")
        if sb and str(sb) not in topic_link_ids:
            errors.append(f"{path}:{link.get('link','?')}: superseded_by {sb!r} does not match any link id in this topic")
    for lid, count in seen_link_ids.items():
        if count > 1:
            errors.append(f"{path}: link id {lid!r} used {count} times within this topic -- link ids must be unique per topic")

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
    code_roots: list[Path],
    body: str = "",
    known_topic_ids: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """docs/SCHEMA.md.1 addendum SS4: type: concept records.

    - implemented_by/tested_by path that doesn't exist on disk under any of
      code_roots ("#symbol" fragment stripped) -> error naming every root
      tried. Skipped entirely when code_roots is empty (existence isn't
      checkable without at least one root).
    - a path that resolves and exists under MORE than one of code_roots ->
      ERROR (not a warning): one reference must name one file, so a ref
      that is ambiguous across roots is exactly as unresolved as two
      concepts claiming the same symbol (see _duplicate_claim_errors).
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
      code_roots-less path-existence check).
    - no tested_by entries -> warning (promotion needs at least one).
    - concept body with no "not this concept" sentence (what this concept
      is explicitly NOT) -> warning.
    """
    errors: list[str] = []
    warnings: list[str] = []
    cid = fm.get("id") or path.stem

    resolved_roots = [r.resolve() for r in (code_roots or [])]
    if resolved_roots:
        roots_desc = ", ".join(str(r) for r in resolved_roots)
        for field in ("implemented_by", "tested_by"):
            for ref in fm.get(field) or []:
                ref_str = str(ref)
                ref_path, _, frag = ref_str.partition("#")

                # Finding 4 (containment): an absolute ref_path silently
                # discarded code_root entirely (Path's `/` operator drops
                # the left side for an absolute right-hand side), and a
                # relative "../.." path could walk outside code_root with
                # no check at all -- either used to be judged only by
                # whatever full.exists() happened to say about wherever it
                # landed. Both are now a hard, named error instead. This
                # is root-independent (an absolute path can't be relative
                # to ANY root), so it's checked once, not per root.
                if Path(ref_path).is_absolute():
                    errors.append(
                        f"{path}: {cid} {field} path {ref_path!r} is absolute -- "
                        f"must be relative to a code root ({roots_desc})"
                    )
                    continue

                # The `..`-escape check runs against each root: the same
                # relative ref_path can escape one root while staying
                # contained in another (a shallower root has less room to
                # walk up out of). A root it escapes contributes no hit.
                hits: list[Path] = []
                escaped_from: list[Path] = []
                for root in resolved_roots:
                    full = (root / ref_path).resolve()
                    try:
                        full.relative_to(root)
                    except ValueError:
                        escaped_from.append(root)
                        continue
                    if full.exists():
                        hits.append(root)

                if len(hits) > 1:
                    hits_desc = ", ".join(str(r) for r in hits)
                    errors.append(
                        f"{path}: {cid} {field} path {ref_path!r} exists under several code "
                        f"roots ({hits_desc}) -- one reference must name one file"
                    )
                    continue
                if not hits:
                    if len(escaped_from) == len(resolved_roots):
                        errors.append(
                            f"{path}: {cid} {field} path {ref_path!r} escapes code_root "
                            f"({roots_desc})"
                        )
                    else:
                        errors.append(
                            f"{path}: {cid} {field} path {ref_path!r} does not exist under any "
                            f"code root tried ({roots_desc})"
                        )
                    continue

                root = hits[0]
                full = (root / ref_path).resolve()
                if frag:
                    try:
                        text = full.read_text(encoding="utf-8", errors="ignore")
                    except OSError:
                        text = ""
                    if not _symbol_declared(frag, text, rel_path=ref_path):
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
    code_roots: list[Path] | None = None,
    known_topic_ids: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    fm, body = parse_frontmatter(path)
    if fm.get("type") == "concept":
        return lint_concept(path, fm, code_roots or [], body=body, known_topic_ids=known_topic_ids)
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


def lint_root(root: Path, code_roots: list[Path] | None = None) -> tuple[list[str], list[str]]:
    """One pre-pass walk collects everything id-shaped (known ids for concept
    validation, explicit-id owners for the duplicate check, stem fallbacks for
    the collision warning) so the duplicate-id check costs no walk of its own
    (round-3 reviewer finding 8)."""
    known_topic_ids: set[str] = set()
    id_owners: dict[str, list[Path]] = {}
    stem_owners: dict[str, list[Path]] = {}
    for f in sorted(walk_markdown(root)):
        fm, _body = parse_frontmatter(f)
        is_topic = bool(fm.get("links")) or fm.get("type") == "topic"
        if is_topic:
            tid = fm.get("id") or f.stem
            known_topic_ids.add(str(tid))
        rid = fm.get("id")
        if rid:
            id_owners.setdefault(str(rid), []).append(f)
        elif is_topic or fm.get("type"):
            # Any STRUCTURED record (an explicit type:, or links) without an
            # id falls back to its stem as a lookup id, so every such kind --
            # investigations and sources included -- gets the collision
            # warning (regate finding 5). Untyped plain markdown (a README,
            # an inbox drop) is exempt: nobody chains those by stem, and
            # inbox/*/README.md colliding is the normal state of the tree.
            stem_owners.setdefault(f.stem, []).append(f)

    all_errors: list[str] = []
    all_warnings: list[str] = []
    for f in sorted(walk_markdown(root)):
        errors, warnings = lint_file(f, code_roots, known_topic_ids=known_topic_ids)
        all_errors.extend(errors)
        all_warnings.extend(warnings)
    all_errors.extend(_duplicate_claim_errors(root))
    for rid, files in id_owners.items():
        if len(files) > 1:
            listing = ", ".join(str(f) for f in files)
            all_errors.append(
                f"duplicate id {rid!r} claimed by {len(files)} records: {listing} "
                "-- chain/edge/citation lookups by this id are ambiguous; renumber all but one"
            )
    # Records with NO explicit id fall back to the file stem as their lookup
    # id, so two same-named files in different areas are just as ambiguous to
    # `chain <stem>` -- but only a WARNING: renaming a topic's area must not
    # become an error, and the durable fix is giving each an explicit id.
    for stem, files in stem_owners.items():
        if len(files) > 1:
            listing = ", ".join(str(f) for f in files)
            all_warnings.append(
                f"stem {stem!r} shared by {len(files)} records without explicit ids: {listing} "
                f"-- `chain {stem}` is ambiguous; give each an explicit id"
            )
    return all_errors, all_warnings


def parse_argv(argv: list[str]) -> tuple[str | None, list[str], str | None]:
    """ROOT positional + repeatable --code-root PATH, in either order.

    --code-root accumulates: `--code-root A --code-root B` yields
    ["A", "B"], not "B" silently winning over "A" -- the code index is
    root-scoped, so a concept ref can legitimately live under any one of
    several roots, and every root given must be checked.

    H6: any other `--flag` used to fall through the `elif root is None`
    branch below and get accepted AS the ROOT positional -- `memlint.py
    --anything` linted a nonexistent path named "--anything", found nothing
    under it, and printed "memlint: clean" at exit 0. The third return value
    names the first such flag seen, so the caller can refuse it instead of
    treating it as a path.
    """
    root = None
    code_roots: list[str] = []
    unknown = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--code-root":
            i += 1
            if i < len(argv):
                code_roots.append(argv[i])
        elif a.startswith("--") and unknown is None:
            unknown = a
        elif root is None:
            root = a
        i += 1
    return root, code_roots, unknown


USAGE = """usage: memlint.py ROOT [--code-root PATH ...]

Validate every MemContinuum record under ROOT against the schema and print one
ERROR:/WARNING: line per finding. Exit 1 if any error was found, 0 otherwise
(warnings alone do not fail).

  ROOT               the markdown store root to walk
  --code-root PATH   a code checkout, enabling the concept-record checks that
                     need one: implemented_by/tested_by paths must exist under
                     one of them, and a #symbol fragment must name something
                     the chunker recognizes in that file. Repeatable, for a
                     project with several code roots -- a path found under
                     exactly one root is fine; found under none is an error
                     naming every root tried; found under more than one is an
                     error (one reference must name one file). Omit it
                     entirely and those checks are skipped; every other rule
                     still runs.
  -h, --help         print this and exit

Rule reference: docs/SCHEMA.md sections 7 and 8.4; the complete table of what
this linter checks is in docs/INTERNALS.md (memlint section)."""


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(USAGE, file=sys.stderr)
        return 2
    if "-h" in argv or "--help" in argv:
        print(USAGE)
        return 0
    root_str, code_root_strs, unknown = parse_argv(argv)
    if unknown is not None:
        print(f"unknown argument: {unknown}", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2
    if not root_str:
        print(USAGE, file=sys.stderr)
        return 2
    root = Path(root_str).resolve()
    # Dedupe by resolved path, preserving first-seen order: `--code-root A
    # --code-root A` (or two spellings of the same directory) must not turn
    # every ref found under it into a false "exists under several roots".
    code_roots: list[Path] = []
    seen: set[Path] = set()
    for s in code_root_strs:
        resolved = Path(s).resolve()
        if resolved not in seen:
            seen.add(resolved)
            code_roots.append(resolved)
    errors, warnings = lint_root(root, code_roots)
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
