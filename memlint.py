#!/usr/bin/env python
"""memlint.py -- docs/SCHEMA.md section 7 linter for MemContinuum store markdown.

Rules implemented, a subset of docs/SCHEMA.md section 7:

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

Task A2-1 (TOP-0122 L1 rule 3) adds a second, independent mode:
`memlint.py --against-ref REF [--staged] ROOT` checks the append-only
history invariant instead of the schema rules above -- see
`check_append_only` and `main`'s dispatch for the full contract.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import chunkers

from memidx import (
    AUTHORITIES,
    CONSTRAINT_AUTHORITIES,
    EDGE_RELS,
    HOLD_ELIGIBLE_AUTHORITIES,
    INVARIANT_KINDS,
    KINDS,
    STATUSES,
    ParseResult,
    _uncheckable_remedy,
    code_ref_is_named,
    code_ref_matches,
    fragment_declaration_status,
    fragment_matches_symbol,
    iter_code_files,
    lang_for_source_file,
    newest_active_link,
    parse_record,
    parse_record_text,
    validated_evidence_list,
    walk_markdown,
)

# ---------------------------------------------------------------------------
# concept-rule helpers (Anatomy's intent index -- memidx.py's code-reindex/
# code-search sibling additions to the concept-record rules below)
# ---------------------------------------------------------------------------

_NOT_THIS_RE = re.compile(r"\bNOT\b|not this concept|Does NOT")


def _symbol_declaration_status(frag: str, text: str, rel_path: str) -> tuple:
    """Finding 5 (init/subscript/computed var/backtick names) AND finding 4
    (a QUALIFIED fragment, e.g. "Outer.outerFunc", must validate exactly
    like code-search's runtime concept attachment accepts it): this reuses
    memidx.fragment_declaration_status -- the SAME single-source-of-truth
    predicate concept_matches_for_chunk uses at attach time -- rather than
    a from-scratch regex or a flattened bare-name set. Also never
    false-positives on a name that only appears inside a comment or string
    literal (the chunker's mask already blanks those out).

    `rel_path` (the record's own ref_path when the caller has one) is
    forwarded to fragment_declaration_status, which resolves the file's
    language from it and asks THAT backend's own `declared_symbols` --
    so a "#symbol" fragment on a Python implemented_by/tested_by path is
    checked against Python's vocabulary, not Swift's, with no
    language-specific branch anywhere on this path.

    Tri-state, `(verdict, reason, remedy)`: True/False are the backend's own
    answer, None means nothing could be read -- the backend for that
    language does not run in this python (an optional grammar wheel this
    interpreter lacks), or it runs and could not read this file (over the
    per-file byte cap, or it did not parse) -- `reason` says which, and
    `remedy` says what to do about that particular one. lint_concept warns
    on None and errors only on False -- see that call site."""
    return fragment_declaration_status(frag, text, rel_path=rel_path)


def _is_topic_frontmatter(fm: dict) -> bool:
    """Whether `fm` belongs to a TOPIC record: `links` present, or an
    explicit `type: topic`. The one place this predicate is written (A2-1
    review finding N1 -- it used to be copied three times: here, lint_root's
    pre-pass, and _is_topic_like's own docstring-acknowledged mirror below)."""
    return bool(fm.get("links")) or fm.get("type") == "topic"


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

        # lint-question-mark-verbatim: owner-verbatim is a literal
        # transcript of the owner's own words (SCHEMA section 3/5) -- a
        # question is not a ruling, whoever asked it, so a
        # question-mark-terminated owner-verbatim text is schema-usage
        # laundering, not a citable decision. owner-ratified is the
        # orchestrator's own paraphrase of what the owner affirmed, never a
        # literal transcript, so it is not covered by this rule. Trim
        # trailing quote characters and whitespace first (a copy-paste
        # artifact can leave a stray quote or space after the real "?");
        # skip when text is falsy -- the missing-field error above already
        # covers that case.
        if auth == "owner-verbatim":
            text = ruling.get("text")
            if text and str(text).strip(" \t\r\n\"'").endswith("?"):
                errors.append(
                    f"{prefix}: owner-verbatim text is a question, not a ruling"
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

    # Critical (external-fix round, coordinator review of F4): a code_refs
    # entry that is empty ("") or fragment-only ("#Foo") names no path --
    # code_ref_matches now refuses to match one, but an unvalidated entry
    # like this reaching a live topic was the reachability path for the
    # bug in the first place, so it is rejected here too, at the source.
    topic_id = fm.get("id") or path.stem
    for ref in fm.get("code_refs") or []:
        ref_str = str(ref)
        if not code_ref_is_named(ref_str):
            errors.append(
                f"{path}: topic {topic_id!r} code_refs entry {ref_str!r} is empty or "
                "fragment-only -- a code_ref must name a path"
            )

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
                    declared, reason, remedy = _symbol_declaration_status(
                        frag, text, rel_path=ref_path
                    )
                    if declared is None:
                        # Nothing could be read: either the chunker backend
                        # for this file's language does not run in this
                        # python (an optional grammar wheel it lacks), or it
                        # runs and could not read this particular file (over
                        # the per-file byte cap, or it did not parse). The
                        # symbol is neither proven present nor proven
                        # absent, and a record stays VALID across either
                        # gap: every other surface fails open on both -- the
                        # file lands not-indexed and is retried, the index
                        # reports itself incomplete.
                        #
                        # The reason says which gap this is and the remedy
                        # what to do about THAT one -- both decided at
                        # memidx.fragment_declaration_status, the one place
                        # that sees the failure's own type. A missing wheel
                        # sends the reader to backend-preflight; an
                        # over-cap or unparseable file must not, because
                        # backend-preflight reports that language ok and
                        # would answer a question nobody asked.
                        warnings.append(
                            f"{path}: {cid} {field} fragment {frag!r} is not checked -- "
                            f"{ref_path!r} is uncheckable in this python ({reason}); "
                            f"{remedy}"
                        )
                    elif not declared:
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
    """Design R2 (audit MC-P1-03, TOP-0123 L2): every diagnostic
    parse_record surfaced becomes `ERROR: <path>: <field>: <message>` for
    an INVALID (canonical, malformed/wrongly-shaped) record -- and the
    rule pass below is skipped entirely for it (there is nothing typed
    enough left to check). A valid record's own diagnostics (only
    reachable for a note under the lenient fallback) are WARNINGs instead,
    and the rule pass still runs normally on top of them."""
    result = parse_record(path)
    if not result.valid:
        errors = [f"{path}: {field}: {message}" for field, message in result.diagnostics]
        return errors, []
    warnings = [f"{path}: {field}: {message}" for field, message in result.diagnostics]
    fm, body = result.frontmatter, result.body
    if fm.get("type") == "concept":
        errors, more_warnings = lint_concept(path, fm, code_roots or [], body=body, known_topic_ids=known_topic_ids)
    else:
        is_topic = _is_topic_frontmatter(fm)
        if is_topic:
            errors, more_warnings = lint_topic(path, fm)
        else:
            errors, more_warnings = lint_record(path, fm)
    return errors, warnings + more_warnings


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
        result = parse_record(f)
        if not result.valid:
            continue
        fm = result.frontmatter
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
        result = parse_record(f)
        if not result.valid:
            continue
        fm = result.frontmatter
        is_topic = _is_topic_frontmatter(fm)
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
    if code_roots:
        marker_errors, marker_warnings = lint_markers(root, code_roots)
        all_errors.extend(marker_errors)
        all_warnings.extend(marker_warnings)
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


# ---------------------------------------------------------------------------
# Constraint marker comments verified both ways (task A2-2, TOP-0122 L1 rule
# 2b). SCHEMA sec2/sec8.3: a `decision: TOP-xxxx Ln` comment on a symbol's
# definition line, or within the three lines above it, mirrors a CONSTRAINT
# or HOLD link at the code it binds; memlint checks the pair both ways.
# Rule 1 (SCHEMA sec2): only a `path#symbol` code_refs entry takes part in
# marker verification -- a bare path or an fnmatch glob keeps serving
# retrieval (code_ref_matches, unchanged) but names no SYMBOL, so it can
# never satisfy either direction below.
#
# Gated on code_roots exactly like lint_concept's own checks: nothing here
# is checkable without at least one code root, and lint_root skips this
# section entirely when none is given. Never runs under --against-ref
# (check_append_only is a wholly separate mode; see its own module comment).
# ---------------------------------------------------------------------------


_DECISION_MARKER_RE = re.compile(r"decision:\s*(TOP-\d{4})\s+(L\d+)\b")


def _find_marker(lines: list[str], start_line: int) -> tuple[str, str, int] | None:
    """(topic_id, link_id, 1-indexed marker_line) for the first decision
    marker on `start_line` (a chunk's own definition line, 1-indexed) or
    within the three lines immediately above it -- spec test (h): three
    lines above counts, four does not. None when no line in that window
    matches. The regex is applied to the raw line text regardless of the
    file's comment syntax (SCHEMA sec8.3: "language-agnostic ... the regex
    ignores the comment leader")."""
    lo = max(0, start_line - 4)
    for lineno, line in enumerate(lines[lo:start_line], start=lo + 1):
        m = _DECISION_MARKER_RE.search(line)
        if m:
            return m.group(1), m.group(2), lineno
    return None


def _link_tier(link: dict) -> str:
    """"constraint" | "hold" | "context" -- SCHEMA sec3/sec4's citation
    tiers, for marker verification specifically (task A2-2 rule 3). Not
    memidx.invariant_enforcement_class: that predicate reads a SQLite row
    shape (`link_row["ruling_authority"]`, JSON-encoded evidence) and is
    only ever called on a link that already carries an invariant (drift's
    own precondition); this reads the raw YAML link dict memlint already
    parses, and is deliberately NARROWER for agent-inference than that
    uniform rule (ruling 76) -- SCHEMA sec3's authority table is explicit
    that agent-inference needs an invariant AND validated evidence to be a
    HOLD eligible for a marker, not evidence alone. Mem-2 (task-a2-2-
    review.md): the CONSTRAINT/HOLD authority sets themselves are
    memidx's own CONSTRAINT_AUTHORITIES/HOLD_ELIGIBLE_AUTHORITIES,
    imported rather than re-typed here, so a future authority never
    silently drifts between the two classifiers."""
    if link.get("status") != "active":
        return "context"
    ruling = link.get("ruling") or {}
    authority = ruling.get("authority")
    if authority in CONSTRAINT_AUTHORITIES:
        return "constraint"
    evidence = validated_evidence_list(link.get("evidence"))
    if authority in HOLD_ELIGIBLE_AUTHORITIES and evidence and (
        authority != "agent-inference" or link.get("invariant")
    ):
        return "hold"
    return "context"


def _collect_topics(root: Path) -> dict[str, dict]:
    """id -> {"path": Path, "fm": dict} for every topic-shaped record under
    root. A pass of its own (not lint_root's id-collision pre-pass, which
    only needs bare ids) because marker verification reads each topic's
    full `links`/`code_refs`."""
    topics: dict[str, dict] = {}
    for f in sorted(walk_markdown(root)):
        result = parse_record(f)
        if not result.valid:
            continue
        fm = result.frontmatter
        if not _is_topic_frontmatter(fm):
            continue
        tid = str(fm.get("id") or f.stem)
        topics[tid] = {"path": f, "fm": fm}
    return topics


def _root_containing(code_roots: list[Path], rel_path: str) -> Path | None:
    """The code root that contains `rel_path`, when several are given --
    longest path first, so a nested root wins over a shallower one that
    also happens to contain a same-named file (rule 6)."""
    for root in sorted(code_roots, key=lambda r: -len(str(r))):
        if (root / rel_path).exists():
            return root
    return None


def _read_and_chunk(full_path: Path, rel_path: str) -> tuple[str | None, list[dict] | None, str, str]:
    """Reads `full_path` and chunks it via the registry. Returns (text,
    chunks, reason, remedy): `chunks` is None when this file could not be
    attempted at all -- no chunker for its language, the backend cannot run
    in this python (an optional grammar wheel it lacks), or it ran and
    could not read THIS file (over the per-file byte cap, or a parse
    failure) -- the same tri-state memidx.fragment_declaration_status
    already carries for the single-symbol check, applied here to the whole
    file's chunk list. `text` is populated whenever the read itself
    succeeded, even when `chunks` ends up None, so a caller that also needs
    the tri-state single-symbol predicate (fragment_declaration_status)
    never has to re-read the file.

    Mem-3 (task-a2-2-review.md): `remedy` mirrors what
    fragment_declaration_status/_uncheckable_remedy already give the
    identical exception classes for the single-symbol check -- empty for
    "no chunker for this file's language" (nothing to remedy: this
    language is simply not wired here) and for a plain OSError reading the
    file itself, populated for every exception-driven path (a missing
    grammar wheel, or a backend that ran but could not read this file),
    via the SAME helper, not a second copy of its exception-to-sentence
    mapping."""
    try:
        text = full_path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return None, None, f"could not read file: {exc}", ""
    lang = lang_for_source_file(full_path)
    if lang is None:
        return text, None, "no chunker for this file's language", ""
    try:
        backend = chunkers.get_chunker(lang)
    except chunkers.BackendUnavailable as exc:
        return text, None, str(exc), _uncheckable_remedy(exc)
    except Exception as exc:
        return text, None, f"{lang}: {type(exc).__name__}: {exc}", _uncheckable_remedy(exc)
    try:
        result = backend.chunk_file(text, rel_path)
    except Exception as exc:
        return text, None, f"{lang}: {type(exc).__name__}: {exc}", _uncheckable_remedy(exc)
    return text, result.chunks, "", ""


def _uncheckable_message(prefix: str, reason: str, remedy: str) -> str:
    """The shared "markers not checked" wording, matching lint_concept's own
    tri-state phrasing (Mem-3): the remedy is appended only when one exists
    (empty for "no chunker for this file's language", which has none)."""
    if remedy:
        return f"{prefix}: markers not checked ({reason}); {remedy}"
    return f"{prefix}: markers not checked ({reason})"


def _scan_set_for_markers(code_roots: list[Path], topics: dict) -> list[tuple[Path, Path, str]]:
    """[(full_path, containing_root, rel_path)] for every file to scan for
    decision markers -- rule 4's bounded scan set: a file under any given
    code root that at least one topic's code_refs names, by ANY form
    (prefix, glob, or path#symbol -- rule 1 restricts which forms take part
    in marker VERIFICATION, not which files are worth opening to look for
    one). Never the whole tree otherwise.

    A file reachable under more than one given root (nested roots) is
    attributed to the LONGEST (its own, most specific) root only -- roots
    are walked longest-first and a file's resolved absolute path, once
    claimed, is never revisited under a shallower root, so its rel_path is
    never computed against the wrong root."""
    all_refs = [
        str(ref)
        for info in topics.values()
        for ref in (info["fm"].get("code_refs") or [])
        if code_ref_is_named(str(ref))
    ]
    if not all_refs or not code_roots:
        return []
    ordered_roots = sorted(code_roots, key=lambda r: -len(str(r)))
    claimed: set[Path] = set()
    out: list[tuple[Path, Path, str]] = []
    for root in ordered_roots:
        for full in iter_code_files(root):
            resolved = full.resolve()
            if resolved in claimed:
                continue
            claimed.add(resolved)
            try:
                rel = full.relative_to(root)
            except ValueError:
                continue
            rel_str = str(rel).replace("\\", "/")
            if any(code_ref_matches(rel_str, ref) for ref in all_refs):
                out.append((full, root, rel_str))
    return out


def _marker_to_store_errors(
    full: Path, marker_line: int, topic_id: str, link_id: str,
    rel_path: str, chunk: dict, topics: dict,
) -> list[str]:
    """Rule 4: one found marker, validated against the store. Returns zero
    or more ERROR strings (no `ERROR:` prefix -- callers add that)."""
    prefix = f"{full}:{marker_line}: decision marker {topic_id} {link_id}"
    info = topics.get(topic_id)
    if info is None:
        return [f"{prefix}: no such topic {topic_id!r}"]
    fm = info["fm"]
    link = next((l for l in fm.get("links") or [] if str(l.get("link")) == link_id), None)
    if link is None:
        return [f"{prefix}: no such link {link_id!r} in topic {topic_id}"]
    if link.get("status") != "active":
        return [f"{prefix}: link status is {link.get('status')!r}, not active"]
    if _link_tier(link) == "context":
        return [
            f"{prefix}: link is CONTEXT, not CONSTRAINT/HOLD -- "
            "a marker may only cite a CONSTRAINT or HOLD link"
        ]

    matched_exact = False
    matched_glob_or_path = False
    wrong_symbols: list[str] = []
    for ref in fm.get("code_refs") or []:
        ref = str(ref)
        if not code_ref_matches(rel_path, ref):
            continue
        _ref_path, has_frag, ref_symbol = ref.partition("#")
        if not has_frag:
            matched_glob_or_path = True
            continue
        if fragment_matches_symbol(ref_symbol, chunk["symbol"], chunk["qualified_name"]):
            matched_exact = True
            break
        if ref_symbol not in wrong_symbols:
            wrong_symbols.append(ref_symbol)
    if matched_exact:
        return []
    if wrong_symbols:
        # Mem-1 (task-a2-2-review.md): a `path#symbol` ref for THIS file
        # exists, it just names a DIFFERENT symbol than the one under the
        # marker (a wrong-symbol typo, or a marker meant for a container
        # whose own #symbol ref the chunker attributes to a nearby member
        # instead) -- a real, distinct situation from "no path#symbol ref
        # at all", and the message says so truthfully rather than denying
        # there is one (the old message conflated both into the glob/
        # bare-path wording below, which is both factually wrong here --
        # there IS a path#symbol ref -- and prescribes the wrong fix).
        named = ", ".join(f"{rel_path}#{s}" for s in wrong_symbols)
        return [
            f"{prefix}: topic {topic_id}'s code_refs name {named}, "
            f"not {rel_path}#{chunk['symbol']}"
        ]
    if matched_glob_or_path:
        return [
            f"{prefix}: topic {topic_id}'s code_refs match {rel_path} only via a path/glob ref -- "
            f"globs (and bare paths) are never marker-verified; add a path#symbol entry for "
            f"{rel_path}#{chunk['symbol']}"
        ]
    return [f"{prefix}: topic {topic_id}'s code_refs do not name {rel_path}"]


def _store_to_code_check(
    tid: str, link_id: str, path_part: str, symbol_part: str,
    code_roots: list[Path], warned_uncheckable: set, warned_symbol_unverifiable: set,
) -> tuple[list[str], list[str]]:
    """Rule 5, one (topic, active CONSTRAINT/HOLD link, path#symbol ref)
    triple: locates the symbol and checks for a matching marker. Returns
    (errors, warnings) -- a dangling ref (the path is missing under every
    root given, or the symbol's NAME is genuinely absent from the file
    text -- ruling 144, TOP-0122 L4) is an ERROR; an uncheckable file is a
    WARNING naming the reason (rule 2), deduped per absolute path across
    the whole run; a symbol whose name is present but that the chunker
    reports no declaration for (a container type, or ruling 144's
    unverifiable case -- a Swift protocol requirement, say) is also a
    WARNING, deduped per (file, symbol) via `warned_symbol_unverifiable`;
    a checkable symbol with no marker is the plain WARNING rule 5 names."""
    errors: list[str] = []
    warnings: list[str] = []
    root = _root_containing(code_roots, path_part)
    if root is None:
        roots_desc = ", ".join(str(r) for r in code_roots)
        errors.append(
            f"{tid}:{link_id}: {path_part}#{symbol_part}: decision ref is dangling -- "
            f"{path_part!r} does not exist under any code root given ({roots_desc})"
        )
        return errors, warnings
    full = (root / path_part).resolve()
    text, chunks, reason, remedy = _read_and_chunk(full, path_part)
    if chunks is None:
        if text is None:
            errors.append(
                f"{tid}:{link_id}: {path_part}#{symbol_part}: decision ref is dangling -- "
                f"{path_part!r} could not be read ({reason})"
            )
        elif full not in warned_uncheckable:
            warned_uncheckable.add(full)
            warnings.append(_uncheckable_message(str(full), reason, remedy))
        return errors, warnings

    match = next(
        (c for c in chunks if fragment_matches_symbol(symbol_part, c["symbol"], c["qualified_name"])),
        None,
    )
    if match is None:
        # Not a chunk the registry reports -- ask the SAME tri-state
        # predicate lint_concept already uses (reuse, not a duplicate
        # existence check, per rule 5's own instruction) to tell a
        # genuinely dangling ref apart from an uncheckable file apart
        # from a real symbol chunk_file simply never emits its own chunk
        # for (a container type: class/struct/enum/... -- SCHEMA sec8.3).
        verdict, fd_reason, fd_remedy = fragment_declaration_status(
            symbol_part, text, rel_path=path_part
        )
        if verdict is False:
            # Ruling 144 (TOP-0122 L4, task-a2-2-review.md): the chunker
            # reporting no declaration is an ERROR only when the symbol's
            # NAME is genuinely absent from the file text -- a
            # word-boundary text search on the last dotted component,
            # never a declaration proof of its own. When the name IS
            # present (a Swift protocol requirement -- signature only, no
            # body -- or a container the chunker layer does not emit its
            # own chunk for), the declaration truly cannot be verified by
            # this engine's parser layer, not disproven, so this is a
            # WARNING and the marker check is skipped for this ref, the
            # same way an uncheckable file already is.
            name = symbol_part.rsplit(".", 1)[-1]
            if re.search(r"\b" + re.escape(name) + r"\b", text):
                key = (full, symbol_part)
                if key not in warned_symbol_unverifiable:
                    warned_symbol_unverifiable.add(key)
                    warnings.append(
                        f"{tid}:{link_id}: {path_part}#{symbol_part} cannot be verified "
                        "by the chunker (name present, no declaration reported)"
                    )
            else:
                errors.append(
                    f"{tid}:{link_id}: {path_part}#{symbol_part}: decision ref is dangling -- "
                    f"{symbol_part!r} is not declared in {path_part}"
                )
        elif verdict is None:
            if full not in warned_uncheckable:
                warned_uncheckable.add(full)
                warnings.append(_uncheckable_message(str(full), fd_reason, fd_remedy))
        else:
            key = (full, symbol_part)
            if key not in warned_symbol_unverifiable:
                warned_symbol_unverifiable.add(key)
                warnings.append(
                    f"{full}: markers not checked ({symbol_part!r} is a container type; "
                    "the chunker reports no start line for it)"
                )
        return errors, warnings

    found = _find_marker(text.splitlines(), match["start_line"])
    if found and found[0] == tid and found[1] == link_id:
        return errors, warnings
    warnings.append(f"{tid}:{link_id}: no marker at {path_part}#{symbol_part}")
    return errors, warnings


def lint_markers(root: Path, code_roots: list[Path]) -> tuple[list[str], list[str]]:
    """Task A2-2 (TOP-0122 L1 rule 2b): constraint/hold decision markers
    verified both ways -- see the module comment above this section for
    the rule summary. Skipped entirely when code_roots is empty (like
    lint_concept, nothing here is checkable without at least one root)."""
    errors: list[str] = []
    warnings: list[str] = []
    if not code_roots:
        return errors, warnings
    topics = _collect_topics(root)
    warned_uncheckable: set[Path] = set()
    warned_symbol_unverifiable: set[tuple] = set()

    # direction 1: marker -> store (errors)
    for full, _file_root, rel_path in _scan_set_for_markers(code_roots, topics):
        text, chunks, reason, remedy = _read_and_chunk(full, rel_path)
        if chunks is None:
            if full not in warned_uncheckable:
                warned_uncheckable.add(full)
                warnings.append(_uncheckable_message(str(full), reason, remedy))
            continue
        lines = text.splitlines()
        for chunk in chunks:
            found = _find_marker(lines, chunk["start_line"])
            if not found:
                continue
            topic_id, link_id, marker_line = found
            errors.extend(
                _marker_to_store_errors(full, marker_line, topic_id, link_id, rel_path, chunk, topics)
            )

    # direction 2: store -> code (warnings; a dangling ref is an error)
    for tid, info in topics.items():
        fm = info["fm"]
        for link in fm.get("links") or []:
            if link.get("status") != "active" or _link_tier(link) not in ("constraint", "hold"):
                continue
            link_id = str(link.get("link"))
            for ref in fm.get("code_refs") or []:
                ref = str(ref)
                if not code_ref_is_named(ref):
                    continue
                path_part, has_frag, symbol_part = ref.partition("#")
                if not has_frag:
                    continue
                errs, warns = _store_to_code_check(
                    tid, link_id, path_part, symbol_part, code_roots,
                    warned_uncheckable, warned_symbol_unverifiable,
                )
                errors.extend(errs)
                warnings.extend(warns)

    return errors, warnings


# ---------------------------------------------------------------------------
# Append-only history (task A2-1, TOP-0122 L1 rule 3): `memlint.py
# --against-ref REF [--staged] ROOT`. A wholly separate check from
# lint_root/lint_file above -- when --against-ref is given, main() runs
# ONLY this and never the schema-rule pass, deliberately: an ADOPTED store
# may carry pre-existing schema findings the installer already tolerates
# (docs/INTERNALS.md), and this check must never fail a commit over a
# condition nobody ruled on just because it happens to also run lint_root.
# ---------------------------------------------------------------------------


class GitError(Exception):
    """A root that is not a git repository, or a REF that does not resolve
    to a commit -- main() maps this to `memlint: <message>` on stderr and
    exit 2 (spec test (i)). Never raised for a content problem (malformed
    frontmatter on either side is a diagnostic -- see _parse_git_blob --
    and surfaces as an ordinary ERROR: line / exit 1, not this)."""


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Runs git, returns the completed process (stdout/stderr as bytes,
    never decoded here). Never raises for the ordinary "this ref/path does
    not exist" case -- callers that care check the return code themselves;
    this only wraps the "git itself could not even be started" case (a bad
    cwd, no git on PATH) into a GitError so a caller never has to catch
    OSError separately."""
    try:
        proc = subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True)
    except OSError as exc:
        raise GitError(f"could not run git in {cwd}: {exc}") from exc
    return proc


def _git_toplevel(root: Path) -> Path:
    proc = _run_git(["-C", str(root), "rev-parse", "--show-toplevel"], root)
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"{root} is not inside a git repository" + (f" ({stderr})" if stderr else ""))
    return Path(proc.stdout.decode("utf-8", "replace").strip()).resolve()


def _resolve_ref(toplevel: Path, ref: str) -> None:
    proc = _run_git(["-C", str(toplevel), "rev-parse", "--verify", "-q", f"{ref}^{{commit}}"], toplevel)
    if proc.returncode != 0:
        raise GitError(f"unknown ref {ref!r} in {toplevel}")


def _git_show(cwd: Path, spec: str) -> bytes | None:
    """None means "this path does not exist at this ref/index stage" --
    the ordinary, expected shape for a brand-new or deleted path; callers
    decide what None means from the diff status they already have, they
    never have to guess from git's exit code alone."""
    proc = _run_git(["show", spec], cwd)
    if proc.returncode != 0:
        return None
    return proc.stdout


def _read_worktree(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _parse_name_status_z(raw: bytes) -> list[tuple[str, str]]:
    """`git diff --name-status -z --no-renames` output: NUL-separated
    STATUS, PATH pairs (a trailing NUL leaves one empty token at the end).
    --no-renames means a rename/copy never appears as one R/C entry with a
    similarity score -- it is always a plain D (old path) + A (new path)
    pair instead, which is exactly what lets "a topic file deleted or
    renamed" share one code path below (see check_append_only): the OLD
    path's own D is the only entry that matters, regardless of whether a
    similarly-shaped A shows up elsewhere in the same diff."""
    tokens = raw.split(b"\x00")
    entries: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok:
            i += 1
            continue
        status = tok.decode("utf-8", "replace")[:1]
        path_tok = tokens[i + 1] if i + 1 < len(tokens) else b""
        path = path_tok.decode("utf-8", "surrogateescape")
        entries.append((status, path))
        i += 2
    return entries


def _parse_git_blob(data: bytes | None, label) -> ParseResult:
    """The typed-parse entry point for git-blob content (a path that does
    not exist at this side becomes an empty, non-canonical ParseResult --
    "no record here", never an error of its own; a missing path is judged
    entirely by the diff status the caller already has)."""
    if data is None:
        return ParseResult({}, "", [], valid=True, fallback=False)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ParseResult({}, "", [("file", "not UTF-8")], valid=False, fallback=False)
    return parse_record_text(text, label)


def _is_topic_like(result: ParseResult) -> bool:
    """A canonical-but-unparseable record (result.valid is False) is
    treated as topic-relevant unconditionally -- it was schema-shaped
    (an id/type/links marker was present in the raw text) even though it
    could not be safely parsed, and silently skipping it would let a
    broken-but-canonical record's link history go completely unchecked.
    A record that parsed cleanly is topic-relevant the same way lint_file
    decides it (N1: shares _is_topic_frontmatter rather than its own copy)."""
    if not result.valid:
        return True
    return _is_topic_frontmatter(result.frontmatter)


# Ruling 142 (TOP-0122 L3, fix round 1) plus ruling 143 (task A2-2): append-
# only freezes a recorded link's BODY -- every field except these THREE,
# which are lifecycle fields allowed to move FORWARD ONLY and ONCE. `status`
# may move from active/provisional to superseded/historical/declined (never
# back to active/provisional, never between the three terminal values -- so
# a provisional record can only ever be PROMOTED by a NEW link per
# docs/SCHEMA.md section 5, never by editing this field to `active`).
# `superseded_by` may be ADDED once status is (or becomes, in the SAME
# link) `superseded`; it is immutable once set, and may never be present
# when status is not `superseded`. `promoted_by` (ruling 143) may be ADDED
# once, with no such status coupling -- SCHEMA section 5 step 3: a later
# owner-ratified link promotes an agent-inference/provisional one by
# appending a NEW link and adding `promoted_by: L<n>` to the OLD link,
# whatever its own status; it is immutable once set, exactly like
# `superseded_by`. `link` (the id) is excluded from the generic body-field
# diff below for a different reason -- it is the key callers already match
# old/new links by, so it is definitionally equal and never worth its own
# diagnostic.
_LIFECYCLE_ONLY_STATUSES = ("active", "provisional")
_LIFECYCLE_TERMINAL_STATUSES = ("superseded", "historical", "declined")
_LINK_NON_BODY_FIELDS = {"link", "status", "superseded_by", "promoted_by"}


def _link_diff_errors(full_path, lid: str, old_link: dict, new_link: dict) -> list[str]:
    """Compares one link present at both REF and now; returns zero or more
    ERROR strings (no path/`ERROR:` prefix -- callers add that), each
    naming the one field it is about. A lifecycle move (`status` and/or
    `superseded_by` and/or `promoted_by`, ruling 143) is valid only when it
    is the SOLE change on the link -- any co-occurring body-field edit
    invalidates it too, each getting its own message (so a status change
    bundled with a `ruling.text` edit reports both, not just one)."""
    errors: list[str] = []

    body_fields = (set(old_link) | set(new_link)) - _LINK_NON_BODY_FIELDS
    body_changed = sorted(f for f in body_fields if old_link.get(f) != new_link.get(f))
    for field in body_changed:
        errors.append(
            f"{full_path}:{lid}: {field}: link field changed after being recorded "
            "(append-only; add a new link instead)"
        )

    old_status = old_link.get("status")
    new_status = new_link.get("status")
    old_sb = old_link.get("superseded_by")
    new_sb = new_link.get("superseded_by")
    lifecycle_only = not body_changed

    if old_status != new_status:
        forward_ok = old_status in _LIFECYCLE_ONLY_STATUSES and new_status in _LIFECYCLE_TERMINAL_STATUSES
        if not forward_ok:
            errors.append(
                f"{full_path}:{lid}: status: changed from {old_status!r} to {new_status!r} "
                "after being recorded (append-only; only active/provisional -> "
                "superseded/historical/declined is allowed, once -- a promotion to "
                "active/provisional is a NEW link, never an edit to this one)"
            )
        elif not lifecycle_only:
            errors.append(
                f"{full_path}:{lid}: status: changed from {old_status!r} to {new_status!r} "
                "together with other field edit(s) after being recorded (append-only; "
                "a lifecycle move must be the only change on a recorded link)"
            )

    if old_sb != new_sb:
        if old_sb is not None:
            errors.append(
                f"{full_path}:{lid}: superseded_by: changed after being recorded "
                "(append-only; immutable once set)"
            )
        elif new_status != "superseded":
            errors.append(
                f"{full_path}:{lid}: superseded_by: added but status is {new_status!r}, "
                "not superseded (append-only)"
            )
        elif not lifecycle_only:
            errors.append(
                f"{full_path}:{lid}: superseded_by: added together with other field "
                "edit(s) after being recorded (append-only; a lifecycle move must be "
                "the only change on a recorded link)"
            )

    old_pb = old_link.get("promoted_by")
    new_pb = new_link.get("promoted_by")
    if old_pb != new_pb:
        if old_pb is not None:
            errors.append(
                f"{full_path}:{lid}: promoted_by: changed after being recorded "
                "(append-only; immutable once set)"
            )
        elif not lifecycle_only:
            errors.append(
                f"{full_path}:{lid}: promoted_by: added together with other field "
                "edit(s) after being recorded (append-only; a lifecycle move must be "
                "the only change on a recorded link)"
            )

    return errors


def check_append_only(root: Path, ref: str, staged: bool) -> tuple[list[str], int]:
    """Returns (errors, changed) -- `changed` is the number of topic files
    the diff actually concerned (topic-relevant at REF), independent of
    whether any of them produced an error. Raises GitError for a root that
    is not a git repository or a REF that does not resolve to a commit
    (spec test (i)); every other failure mode is an ordinary ERROR: entry
    in the returned list (spec test (j) -- never a traceback)."""
    toplevel = _git_toplevel(root)
    _resolve_ref(toplevel, ref)
    try:
        rel_root = root.relative_to(toplevel)
    except ValueError:
        rel_root = Path(".")
    prefix = "" if str(rel_root) == "." else str(rel_root).replace("\\", "/") + "/"
    pathspec = prefix.rstrip("/") or "."

    diff_args = ["diff", "--no-renames", "--name-status", "-z"]
    if staged:
        diff_args.append("--cached")
    diff_args += [ref, "--", pathspec]
    raw = _run_git(diff_args, toplevel)
    if raw.returncode != 0:
        stderr = raw.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git diff against {ref!r} failed" + (f" ({stderr})" if stderr else ""))
    entries = _parse_name_status_z(raw.stdout)

    errors: list[str] = []
    changed = 0
    for status, relpath in entries:
        path_in_root = relpath[len(prefix):] if prefix and relpath.startswith(prefix) else relpath
        full_path = root / path_in_root

        if status == "A":
            # Nothing existed at REF for this path -- no history to
            # protect; a brand-new topic (or a brand-new anything) is free.
            continue

        old_blob = _git_show(toplevel, f"{ref}:{relpath}")
        old_result = _parse_git_blob(old_blob, f"{relpath} (at {ref})")
        if not _is_topic_like(old_result):
            continue
        changed += 1

        if status == "D":
            errors.append(
                f"{full_path}: topic file deleted or renamed after being recorded "
                "(append-only; a store never loses history)"
            )
            continue

        if staged:
            new_blob = _git_show(toplevel, f":{relpath}")
        else:
            new_blob = _read_worktree(root / path_in_root)
        if new_blob is None:
            errors.append(
                f"{full_path}: topic file deleted or renamed after being recorded "
                "(append-only; a store never loses history)"
            )
            continue
        new_result = _parse_git_blob(new_blob, path_in_root)

        if not old_result.valid:
            for field, message in old_result.diagnostics:
                errors.append(f"{full_path}: {field}: {message} (at {ref})")
            continue
        if not new_result.valid:
            for field, message in new_result.diagnostics:
                errors.append(f"{full_path}: {field}: {message}")
            continue

        old_links = old_result.frontmatter.get("links") or []
        new_links_by_id = {
            str(l.get("link")): l
            for l in (new_result.frontmatter.get("links") or [])
            if l.get("link") is not None
        }
        for old_link in old_links:
            lid = old_link.get("link")
            if lid is None:
                continue
            lid = str(lid)
            new_link = new_links_by_id.get(lid)
            if new_link is None:
                errors.append(
                    f"{full_path}:{lid}: link removed after being recorded "
                    "(append-only; a store never loses history)"
                )
            elif new_link != old_link:
                errors.extend(_link_diff_errors(full_path, lid, old_link, new_link))

    return errors, changed


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
       memlint.py --against-ref REF [--staged] ROOT

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
                     error (one reference must name one file). Also drives
                     decision-marker verification: a `decision: TOP-xxxx Ln`
                     comment under a code root must name an active
                     CONSTRAINT/HOLD link whose topic's code_refs name that
                     file (an error otherwise), and every such link with a
                     path#symbol ref is checked for a marker at that symbol
                     (a warning if none is found yet). Omit --code-root
                     entirely and all of these checks are skipped; every
                     other rule still runs.
  -h, --help         print this and exit

Append-only history mode (a second, independent check -- given
--against-ref, this runs INSTEAD of the schema rules above, never both):

  --against-ref REF  compare every topic file's links now against what they
                     were at REF (a commit-ish git understands). A link
                     present at REF must be unchanged; a link removed, or a
                     topic file deleted or renamed, is an error. New links,
                     and changes to current/title/tags/code_refs/the body,
                     are free. Exit 1 on any append-only error; exit 2 if
                     ROOT is not inside a git repository or REF does not
                     resolve to a commit.
  --staged           compare REF to the INDEX (what `git commit` would
                     actually commit) instead of the working tree -- the
                     default with --against-ref and no --staged.

Rule reference: docs/SCHEMA.md sections 7 and 8.4; the complete table of what
this linter checks is in docs/INTERNALS.md (memlint section)."""


def _extract_against_ref_flags(argv: list[str]) -> tuple[list[str], str | None, bool, bool]:
    """Pulls --against-ref REF and --staged out of argv before the
    remainder reaches parse_argv unchanged -- parse_argv's own 3-tuple
    contract (and the tests that call it directly) stays exactly as it
    was; this is a preprocessing pass, not a parse_argv change.

    The fourth return value, `saw_against_ref`, is True whenever the
    `--against-ref` TOKEN appeared in argv at all, independent of whether a
    REF followed it (A2-1 review finding L1). Without it, `--against-ref`
    at the very end of argv left `against_ref` None and the flag silently
    discarded, so `main()` fell through to the ORDINARY schema-lint mode
    instead of refusing the malformed invocation -- a mistyped
    `memlint.py ROOT --against-ref` used to exit 0 printing `memlint: clean`,
    never mentioning the missing REF."""
    rest: list[str] = []
    against_ref = None
    staged = False
    saw_against_ref = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--against-ref":
            saw_against_ref = True
            i += 1
            if i < len(argv):
                against_ref = argv[i]
        elif a == "--staged":
            staged = True
        else:
            rest.append(a)
        i += 1
    return rest, against_ref, staged, saw_against_ref


def _run_append_only(root_str: str, ref: str, staged: bool) -> int:
    root = Path(root_str).resolve()
    try:
        errors, changed = check_append_only(root, ref, staged)
    except GitError as exc:
        print(f"memlint: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # never a bare traceback -- spec test (j)/(i)
        print(f"memlint: unexpected failure checking append-only history: {exc}", file=sys.stderr)
        return 2
    for e in errors:
        print(f"ERROR: {e}")
    print(f"memlint: append-only against {ref}: changed={changed} errors={len(errors)}")
    return 1 if errors else 0


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(USAGE, file=sys.stderr)
        return 2
    if "-h" in argv or "--help" in argv:
        print(USAGE)
        return 0
    rest, against_ref, staged, saw_against_ref = _extract_against_ref_flags(argv)
    if saw_against_ref and against_ref is None:
        print("--against-ref requires REF", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2
    if staged and against_ref is None:
        print("--staged requires --against-ref", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2
    root_str, code_root_strs, unknown = parse_argv(rest)
    if unknown is not None:
        print(f"unknown argument: {unknown}", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2
    if not root_str:
        print(USAGE, file=sys.stderr)
        return 2
    if against_ref is not None:
        return _run_append_only(root_str, against_ref, staged)
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
