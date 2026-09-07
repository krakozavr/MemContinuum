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

import os
import re
import subprocess
import sys
from pathlib import Path

import chunkers

from memidx import (
    AUTHORITIES,
    CANONICAL_ID_PREFIXES,
    CANONICAL_TYPES,
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
    is_binary_file,
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
        # trailing quote/bracket characters and whitespace first (a
        # copy-paste artifact can leave a stray quote, space, or closing
        # bracket after the real "?" -- whole-branch-review MODERATE-2:
        # `)]}` joined the strip set so "...decisionmaking?)" is still
        # caught, not silently passed because of one trailing paren);
        # skip when text is falsy -- the missing-field error above already
        # covers that case. Ruling 149 (whole-branch-review MODERATE-2):
        # scoped to status active/provisional only -- a superseded link is
        # history, and the append-only guard already forbids rewriting it,
        # so flagging it here can never be cleared by superseding (the
        # exact permanently-red-store bug the finding reproduced on both
        # real stores).
        if auth == "owner-verbatim" and status in ("active", "provisional"):
            text = ruling.get("text")
            if text and str(text).strip(" \t\r\n\"')]}").endswith("?"):
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

    # Codex 2 (BLOCKING, fix wave 1 G1): a duplicate link id within one
    # topic used to be flagged HERE, but only when the record otherwise
    # parsed cleanly enough to reach lint_topic at all -- check_append_only
    # (a dict keyed by id) and reindex (memidx.build_record's own link
    # rows) each silently kept a DIFFERENT occurrence (last-wins), so a
    # duplicate id could bypass append-only comparison entirely and index
    # a different link's content than the one a human reading the file
    # sees first. The check now lives in memidx.validate_record_shape --
    # the one typed-parse gate every consumer (this linter, reindex,
    # check_append_only's own git-blob parse) already goes through -- so a
    # topic with a duplicate link id is QUARANTINED (ParseResult.valid is
    # False) before any of those three ever sees it, rather than being
    # caught three different ways with three different blast radii. See
    # lint_file's own `ERROR: <path>: links: duplicate link id ...` line,
    # which fires from that same diagnostic.
    topic_link_ids = {str(l.get("link")) for l in links if l.get("link")}
    topic_links_by_id = {str(l.get("link")): l for l in links if l.get("link")}
    for link in links:
        rev = link.get("reverses")
        if rev and str(rev) not in topic_link_ids:
            errors.append(f"{path}:{link.get('link','?')}: reverses {rev!r} does not match any link id in this topic")
        elif rev and link.get("kind") == "reversed":
            # Replaces the backlog row "two active links in one chain is an
            # error" (owner ruling 2026-09-06 10:41, TOP-0122 L5): that
            # check would fail every area topic, where several active
            # rulings apply at once by design. The mechanical check that
            # survives is narrower -- a link declaring kind: reversed must
            # point at a link that is no longer active or provisional.
            # kind: amended leaves its predecessor active on purpose
            # (SCHEMA section 3/6.1's "reverses:" table): no rule for it.
            target = topic_links_by_id.get(str(rev))
            target_status = target.get("status") if target else None
            if target_status in ("active", "provisional"):
                errors.append(
                    f"{path}:{link.get('link','?')}: reverses {rev!r} which is still "
                    f"{target_status} (mark it superseded)"
                )
        sb = link.get("superseded_by")
        if sb and str(sb) not in topic_link_ids:
            errors.append(f"{path}:{link.get('link','?')}: superseded_by {sb!r} does not match any link id in this topic")

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


# Codex 16 / whole-branch-review LOW-3 (fix wave 1 G2): `TOP-\d+`, not
# `TOP-\d{4}` -- SCHEMA sec8.3's own running example topic is `id: TOP-42`
# (SCHEMA.md's own `id: TOP-42` convention, sec1), so its copy-pasted
# marker example `# decision: TOP-42 L4` used to silently never match this
# regex at all. Any positive integer id, matching how ids are actually
# authored elsewhere in this store (no fixed digit count is enforced on
# `id:` itself).
_DECISION_MARKER_RE = re.compile(r"decision:\s*(TOP-\d+)\s+(L\d+)\b")


def _declaration_boundaries(chunks: list[dict]) -> list[int]:
    """Sorted, deduped 1-indexed start_lines for every chunk in a file --
    the set of "another declaration's line" a marker window must never
    cross (Codex 8, fix wave 1 G2)."""
    return sorted({c["start_line"] for c in chunks})


def _find_markers(
    lines: list[str], start_line: int, boundaries: list[int] | None = None,
) -> list[tuple[str, str, int]]:
    """Every decision marker belonging to the declaration whose definition
    line is `start_line` (1-indexed): a marker on that line itself, or on
    any of the (up to three) lines immediately above it -- spec test (h):
    three lines above counts, four does not.

    Searched NEAREST FIRST (Codex 7, fix wave 1 G2): a stale or
    neighboring declaration's marker sitting farther up used to be found
    ahead of a valid marker sitting ON the definition line itself, simply
    because the old scan went top-down and returned the FIRST hit --
    reversed here so the closest line to `start_line` is checked first,
    the farthest last.

    NEVER crosses another declaration's own line (Codex 8): when
    `boundaries` is given, the window's upper (backward) limit is clipped
    just below the nearest PRECEDING boundary strictly less than
    `start_line` -- a marker belonging to an earlier declaration must
    never also be attributed to this one merely because it falls within
    the flat 3-line count (two adjacent short declarations, or one right
    after another with no body lines between them).

    EVERY marker actually inside the (possibly clipped) window is
    returned, nearest first (Codex 7): a valid marker followed -- farther
    up -- by a second, bogus one used to be entirely invisible once the
    first (nearest) one was found; both are now returned and the caller
    (rule 4's own marker->store validation) examines each one, not just
    the first. The regex is applied to the raw line text regardless of
    the file's comment syntax (SCHEMA sec8.3: "language-agnostic ... the
    regex ignores the comment leader")."""
    lo = max(0, start_line - 4)
    if boundaries:
        prev = max((b for b in boundaries if b < start_line), default=None)
        if prev is not None:
            lo = max(lo, prev)
    found: list[tuple[str, str, int]] = []
    for lineno in range(start_line, lo, -1):
        if lineno < 1 or lineno > len(lines):
            continue
        m = _DECISION_MARKER_RE.search(lines[lineno - 1])
        if m:
            found.append((m.group(1), m.group(2), lineno))
    return found


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
    one). Never the whole tree otherwise -- Codex 13 (fix wave 1 G2): this
    used to walk `iter_code_files(root)`, which opens and reads EVERY file
    under `root` for its binary-file check before this function's own ref
    match filter ever runs (a probe observed a wholly unreferenced
    not-referenced.txt being opened alongside the single referenced x.py,
    contradicting INTERNALS' "the scan never walks a whole code root").
    The directory walk itself still has to visit every directory (there is
    no way to know which subtrees a glob/prefix code_ref might reach
    without looking), but each FILENAME is matched against every code_ref
    -- pure string work, no I/O -- BEFORE it is ever opened; `is_binary_
    file` (an actual read) only ever runs on a file that already matched.

    A file reachable under more than one given root (nested roots) is
    attributed to the LONGEST (its own, most specific) root only -- roots
    are walked longest-first and a file's resolved absolute path, once
    claimed, is never revisited under a shallower root, so its rel_path is
    never computed against the wrong root.

    The macOS duplicate warning (whole-branch-review, reproduced on CI):
    `full` in the returned tuple is the PHYSICAL path (`.resolve()`,
    matching `_store_to_code_check`'s own `(root / path_part).resolve()`)
    -- keyed on the raw, possibly-symlinked path instead, `/var/folders/
    ...` and macOS's own `/private/var/folders/...` alias for the exact
    same file were two different dict/set keys to lint_markers' shared
    `warned_uncheckable`, so the identical "markers not checked" warning
    for one physical file was emitted once per spelling. `rel` is
    still computed from the UNRESOLVED `full` against the UNRESOLVED
    `root` (matching how `root` was actually walked) -- resolving first
    would break `relative_to` whenever `root` itself sits behind a
    symlink component `full` no longer shares a literal prefix with."""
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
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in chunkers.UNIVERSAL_SKIP_DIRS]
            for fname in filenames:
                full = Path(dirpath) / fname
                try:
                    rel = full.relative_to(root)
                except ValueError:
                    continue
                rel_str = str(rel).replace("\\", "/")
                if not any(code_ref_matches(rel_str, ref) for ref in all_refs):
                    continue
                resolved = full.resolve()
                if resolved in claimed:
                    continue
                claimed.add(resolved)
                if is_binary_file(full):
                    continue
                out.append((resolved, root, rel_str))
    return out


def _marker_to_store_errors(
    full: Path, marker_line: int, topic_id: str, link_id: str,
    rel_path: str, chunk: dict, topics: dict, text: str, chunks: list[dict],
) -> list[str]:
    """Rule 4: one found marker, validated against the store. Returns zero
    or more ERROR strings (no `ERROR:` prefix -- callers add that).

    `text`/`chunks` (the WHOLE file's text and full chunk list, not just
    `chunk`) are needed for the container carve-out below (Codex 8 /
    ruling 144)."""
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
    container_ref_seen = False
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
        # Codex 8 (fix wave 1 G2): a ref naming a symbol NO chunk in this
        # file reports of its own (a container -- class/struct/enum/... --
        # chunk_file never gives one its own chunk; see chunk_source's own
        # docstring) is unverifiable by this marker-window check, not a
        # genuine wrong-symbol mismatch -- ruling 144's own carve-out for
        # exactly this case (ruling144_swift_protocol_requirement /
        # _store_to_code_check's identical `verdict is True, no chunk`
        # branch, direction 2). Reporting "not rel_path#chunk['symbol']"
        # here would misattribute a container's own marker to whichever
        # member chunk merely happens to sit within 3 lines of it, AND
        # prescribe the wrong fix (there is no member to point the code_ref
        # at). Direction 2 already gives its own "markers not checked
        # (container type)" warning for this same ref; direction 1 stays
        # silent rather than inventing a second, contradictory finding.
        if not any(
            fragment_matches_symbol(ref_symbol, c["symbol"], c["qualified_name"]) for c in chunks
        ):
            verdict, _reason, _remedy = fragment_declaration_status(ref_symbol, text, rel_path=rel_path)
            if verdict is True:
                container_ref_seen = True
                continue
        if ref_symbol not in wrong_symbols:
            wrong_symbols.append(ref_symbol)
    if matched_exact:
        return []
    if wrong_symbols:
        # Mem-1 (task-a2-2-review.md): a `path#symbol` ref for THIS file
        # exists, it just names a DIFFERENT symbol than the one under the
        # marker (a wrong-symbol typo) -- a real, distinct situation from
        # "no path#symbol ref at all", and the message says so truthfully
        # rather than denying there is one (the old message conflated both
        # into the glob/bare-path wording below, which is both factually
        # wrong here -- there IS a path#symbol ref -- and prescribes the
        # wrong fix).
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
    if container_ref_seen:
        return []
    return [f"{prefix}: topic {topic_id}'s code_refs do not name {rel_path}"]


def _marker_to_store_errors_no_chunker(
    full: Path, marker_line: int, topic_id: str, link_id: str, rel_path: str, topics: dict,
) -> tuple[list[str], list[str]]:
    """Rule 4's counterpart for a marker found in a file whose language has
    no chunker at all (Grok re-gate R6): there is no chunk and no symbol,
    so nothing here can be located against one -- but the store-side rules
    that need no symbol location at all still apply (a marker naming a
    dead topic/link, an inactive link, a CONTEXT-tier link, or a topic
    whose code_refs do not even name this FILE, is exactly as wrong here
    as it would be in a chunked file). `code_ref_matches` already strips
    any `#symbol` fragment before comparing, so a path#symbol entry counts
    exactly like a bare path or glob for this file-level check -- a
    specific symbol can never be verified here regardless of which form
    named the file. Returns (errors, warnings): when every store-side
    check passes, the one thing that genuinely cannot be done -- pointing
    the marker at a specific symbol -- is reported as the WARNING, never
    an error; there is nothing wrong with the record, only something this
    engine's parser layer cannot do for this language."""
    prefix = f"{full}:{marker_line}: decision marker {topic_id} {link_id}"
    info = topics.get(topic_id)
    if info is None:
        return [f"{prefix}: no such topic {topic_id!r}"], []
    fm = info["fm"]
    link = next((l for l in fm.get("links") or [] if str(l.get("link")) == link_id), None)
    if link is None:
        return [f"{prefix}: no such link {link_id!r} in topic {topic_id}"], []
    if link.get("status") != "active":
        return [f"{prefix}: link status is {link.get('status')!r}, not active"], []
    if _link_tier(link) == "context":
        return [
            f"{prefix}: link is CONTEXT, not CONSTRAINT/HOLD -- "
            "a marker may only cite a CONSTRAINT or HOLD link"
        ], []
    if not any(code_ref_matches(rel_path, str(ref)) for ref in fm.get("code_refs") or []):
        return [f"{prefix}: topic {topic_id}'s code_refs do not name {rel_path}"], []
    return [], [
        f"{full}:{marker_line}: marker cannot be attributed to a symbol "
        "(no chunker for this file's language)"
    ]


def _store_to_code_check(
    tid: str, link_id: str, path_part: str, symbol_part: str,
    code_roots: list[Path], warned_uncheckable: set, warned_symbol_unverifiable: set,
    chunkerless_pending: dict | None = None, chunkerless_covered: set | None = None,
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
    a checkable symbol with no marker is the plain WARNING rule 5 names.

    Round 2b (NIT 4): when this file has no chunker AND direction 1
    already earned it a held-back attribution warning (`full` is a key of
    `chunkerless_pending`), the generic per-file "markers not checked (no
    chunker for this file's language)" line would only repeat the same
    fact that file-level warning already carries -- this ref's own line
    instead names ITS symbol specifically (never deduped against another
    (topic, link) pair naming the same file: each is a genuinely distinct
    record needing its own answer), and the file is recorded in
    `chunkerless_covered` so `lint_markers` never also appends the
    held-back generic warning for it."""
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
        elif (
            reason == "no chunker for this file's language"
            and chunkerless_pending is not None
            and full in chunkerless_pending
        ):
            if chunkerless_covered is not None:
                chunkerless_covered.add(full)
            warnings.append(
                f"{tid}:{link_id}: symbol {path_part}#{symbol_part} cannot be located "
                "(no chunker for this file's language)"
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

    # Codex 7: every marker in the window is examined, not just the
    # nearest -- two legitimate markers can sit in the same window (one
    # per topic/link a member satisfies), and the expected (tid, link_id)
    # pair may be either one, not necessarily the first found.
    found_markers = _find_markers(
        text.splitlines(), match["start_line"], _declaration_boundaries(chunks)
    )
    if any(f[0] == tid and f[1] == link_id for f in found_markers):
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
    # Round 2b (NIT 4): a no-chunker file that both carries a marker
    # (direction 1's own attribution warning) AND is named by an active
    # CONSTRAINT/HOLD link's path#symbol ref (direction 2) used to warn
    # about the identical underlying fact -- no chunker for this file's
    # language -- twice, once generically per file, once again as the
    # file-level attribution warning. Direction 1's attribution warning
    # for such a file is now HELD BACK (not appended to `warnings`
    # directly) in `chunkerless_pending` (full path -> its one message);
    # direction 2, for each active CONSTRAINT/HOLD path#symbol ref it
    # finds into a pending file, reports THAT ref's own inability to
    # locate its symbol instead (naming the ref, not deduped -- two
    # different links citing the same file each get their own answer)
    # and records the file in `chunkerless_covered`. Once both directions
    # have run, any file that earned an attribution warning but was NEVER
    # reached by direction 2 (no path#symbol ref names it -- only a bare
    # path or glob does, or none at all) still gets its one held-back
    # warning appended at the end -- direction 2 never had anything more
    # specific to say about it.
    chunkerless_pending: dict[Path, str] = {}
    chunkerless_covered: set[Path] = set()

    # direction 1: marker -> store (errors)
    for full, _file_root, rel_path in _scan_set_for_markers(code_roots, topics):
        text, chunks, reason, remedy = _read_and_chunk(full, rel_path)
        if chunks is None:
            if reason == "no chunker for this file's language":
                # Grok re-gate R6: this is not a failure -- the language is
                # simply not wired here, and the file's own text (`text` is
                # always populated for this exact reason -- see
                # _read_and_chunk) is still fully readable. Scan it with
                # the marker regex alone, with no chunk-derived window
                # (there is no declaration to anchor one to): silent when
                # it holds no marker at all, instead of the old blanket
                # per-file warning that fired regardless. A real backend
                # failure (a missing grammar wheel, an unexpected chunking
                # exception) still falls through to the ordinary
                # uncheckable-file warning below -- that IS a genuine gap
                # worth naming, unlike a language that was never wired.
                for lineno, line in enumerate(text.splitlines(), start=1):
                    m = _DECISION_MARKER_RE.search(line)
                    if not m:
                        continue
                    errs, warns = _marker_to_store_errors_no_chunker(
                        full, lineno, m.group(1), m.group(2), rel_path, topics,
                    )
                    errors.extend(errs)
                    if warns:
                        chunkerless_pending.setdefault(full, warns[0])
                continue
            if full not in warned_uncheckable:
                warned_uncheckable.add(full)
                warnings.append(_uncheckable_message(str(full), reason, remedy))
            continue
        lines = text.splitlines()
        boundaries = _declaration_boundaries(chunks)
        for chunk in chunks:
            # Codex 7: every marker actually in the window is examined,
            # not just the first (nearest) one found -- a valid marker
            # followed (farther up) by a bogus one must still error on
            # the bogus one; two valid markers must each satisfy their
            # own topic.
            for topic_id, link_id, marker_line in _find_markers(lines, chunk["start_line"], boundaries):
                errors.extend(
                    _marker_to_store_errors(
                        full, marker_line, topic_id, link_id, rel_path, chunk, topics, text, chunks,
                    )
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
                    chunkerless_pending, chunkerless_covered,
                )
                errors.extend(errs)
                warnings.extend(warns)

    # Round 2b (NIT 4): a pending attribution warning direction 2 never
    # reached (no path#symbol ref names that file) is still owed -- append
    # it now, exactly once per file, same as before this fix.
    for full, msg in chunkerless_pending.items():
        if full not in chunkerless_covered:
            warnings.append(msg)

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


_KIND_BY_ID_PREFIX = {
    "TOP-": "topic", "INC-": "incident", "INV-": "investigation", "CON-": "concept",
}


def _record_kind(fm: dict) -> str | None:
    """Best-effort real kind ("topic"/"incident"/"investigation"/
    "concept") of a record's frontmatter, even a partially recovered one
    -- an explicit `type:` first (a FALLBACK_SCALAR_FIELD, always
    recovered by parse_record_text's lenient fallback even when the rest
    of the YAML is broken), else the id prefix (also a scalar, always
    recovered), else a `links` list read as "topic" (the one CANONICAL
    signal is_topic_frontmatter/is_canonical_frontmatter treat that way).
    None when nothing usable was recovered at all (an unterminated block,
    a non-mapping document, an unreadable file) -- there is no real kind
    to report there, only "canonical but a total blank"."""
    t = fm.get("type")
    if t in CANONICAL_TYPES:
        return t
    rid = fm.get("id")
    if isinstance(rid, str):
        for prefix in CANONICAL_ID_PREFIXES:
            if rid.startswith(prefix):
                return _KIND_BY_ID_PREFIX[prefix]
    if fm.get("links"):
        return "topic"
    return None


def _record_kind_label(result: ParseResult) -> str:
    """The real kind for check_append_only's own messages ("topic file
    deleted", etc.) -- Grok N11: an unparseable blob must never be
    universally reported as "topic" just because _is_topic_like's
    conservative default (below) still treats it as protected; "record"
    is the honest generic fallback only when the real kind truly cannot
    be recovered at all."""
    if result.valid:
        return "topic" if _is_topic_frontmatter(result.frontmatter) else (
            _record_kind(result.frontmatter) or "record"
        )
    return _record_kind(result.frontmatter) or "record"


def _is_topic_like(result: ParseResult) -> bool:
    """Grok N11 / whole-branch-review MODERATE-1: an unparseable blob is
    topic-like only when the recoverable signal actually NAMES topic --
    `type: topic`, a `TOP-` id, or (on a clean parse) a real `links` list.
    A `type: investigation`/`incident`/`concept` record -- MODERATE-1's
    real-world repro, a partner store's own INV- record with no links at
    all, broken only by an unquoted colon in its title -- is not protected by
    this append-only mechanism (there is no recorded link history to
    freeze) and must never be reported as one; the old unconditional
    `True` blocked exactly that record's own repair commit. Only when
    NOTHING at all could be recovered (an unterminated block, a
    non-mapping document, an unreadable/non-UTF-8 file -- `_record_kind`
    returns None) does this still default to True: a genuinely corrupted
    TOPIC's link history must never go silently unprotected just because
    nothing could be read from it. A record that parsed cleanly is
    topic-relevant the same way lint_file decides it (N1: shares
    _is_topic_frontmatter rather than its own copy)."""
    if not result.valid:
        kind = _record_kind(result.frontmatter)
        if kind is not None:
            return kind == "topic"
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


def _recovered_old_links(fm: dict) -> list[dict] | None:
    """Grok re-gate MAJOR 1: pulls a usable, de-duplicated `links` list out
    of a REF-side frontmatter dict whose record failed FULL validation --
    a shape error on some UNRELATED field (`tags: not-a-list`), or two
    links sharing an id -- either of which leaves `fm["links"]` fully
    populated: `validate_record_shape` only ever ADDS a diagnostic, and
    `_drop_note_shape_violations`'s dropping never runs for a CANONICAL
    record (one with a real `links` list is always canonical). Returns
    None when nothing usable survives at all (`links` absent/empty, not a
    list, or with no entry carrying a scalar id) -- callers treat that as
    "no recorded history to protect", the pre-existing repair path.
    Otherwise returns one dict per distinct id, KEEPING THE FIRST
    OCCURRENCE when an id repeats (file order) -- the same first-wins
    reading `validate_record_shape`'s own duplicate-id diagnostic is built
    from (it counts occurrences in file order without ever picking a
    "winner" itself; the first is what a human reading the raw file sees
    first for that id, so it is the history to protect)."""
    links = fm.get("links")
    if not isinstance(links, list):
        return None
    recovered: dict[str, dict] = {}
    for link in links:
        if not isinstance(link, dict):
            continue
        lid = link.get("link")
        if lid is None or lid == "" or isinstance(lid, (dict, list)):
            continue
        lid_key = str(lid)
        if lid_key not in recovered:
            recovered[lid_key] = link
    return list(recovered.values()) if recovered else None


def check_append_only(root: Path, ref: str, staged: bool) -> tuple[list[str], int, list[str]]:
    """Returns (errors, changed, notes) -- `changed` is the number of
    topic files the diff actually concerned (topic-relevant at REF),
    independent of whether any of them produced an error; `notes` are
    informational, non-error lines (a REPAIR of a record that never
    parsed at REF -- see below). Raises GitError for a root that is not a
    git repository or a REF that does not resolve to a commit (spec test
    (i)); every other failure mode is an ordinary ERROR: entry in the
    returned list (spec test (j) -- never a traceback)."""
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
    notes: list[str] = []
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
            kind = _record_kind_label(old_result)
            errors.append(
                f"{full_path}: {kind} file deleted or renamed after being recorded "
                "(append-only; a store never loses history)"
            )
            continue

        if staged:
            new_blob = _git_show(toplevel, f":{relpath}")
        else:
            new_blob = _read_worktree(root / path_in_root)
        if new_blob is None:
            kind = _record_kind_label(old_result)
            errors.append(
                f"{full_path}: {kind} file deleted or renamed after being recorded "
                "(append-only; a store never loses history)"
            )
            continue
        new_result = _parse_git_blob(new_blob, path_in_root)

        repair_note = None
        if not old_result.valid:
            old_links = _recovered_old_links(old_result.frontmatter)
            if old_links is None:
                if new_result.valid:
                    # Grok M2 / whole-branch-review MODERATE-1: the REF-side
                    # blob never parsed -- it recorded no link history at all
                    # (the real-world repro: a `type: investigation` record
                    # with no links, broken only by an unquoted colon in its
                    # title) -- and the new blob parses cleanly. This is a
                    # REPAIR, not a history edit: nothing here to freeze, so
                    # it is never an append-only error, only a note.
                    old_reasons = "; ".join(message for _field, message in old_result.diagnostics)
                    notes.append(
                        f"{full_path}: repaired -- the blob at {ref} could not be safely "
                        f"parsed ({old_reasons}); the new blob parses cleanly, so there is "
                        "no recorded link history here to protect"
                    )
                    continue
                # Both sides unparseable: still fail closed (test_j: malformed
                # -> malformed is still refused), reported from the OLD side's
                # diagnostics, same as before this fix.
                for field, message in old_result.diagnostics:
                    errors.append(f"{full_path}: {field}: {message} (at {ref})")
                continue
            # Grok re-gate MAJOR 1: the REF blob failed full validation but
            # its `links` were still recovered (see _recovered_old_links) --
            # there IS recorded link history here, so the repair/skip path
            # above must not apply. Fall through to the ordinary comparison
            # below using the recovered links instead.
            old_reasons = "; ".join(message for _field, message in old_result.diagnostics)
            raw_links = old_result.frontmatter.get("links")
            dup_note = (
                " (a duplicate link id was recovered as its first occurrence)"
                if isinstance(raw_links, list) and len(raw_links) != len(old_links)
                else ""
            )
            repair_note = (
                f"{full_path}: the blob at {ref} could not be safely parsed "
                f"({old_reasons}){dup_note}, but its links were recovered and are "
                "still compared against the current blob for append-only violations"
            )
        else:
            old_links = old_result.frontmatter.get("links") or []

        if not new_result.valid:
            for field, message in new_result.diagnostics:
                errors.append(f"{full_path}: {field}: {message}")
            continue

        if repair_note is not None:
            notes.append(repair_note)

        # Codex 2 (BLOCKING): a duplicate link id on the NEW side already
        # made new_result invalid above (memidx.validate_record_shape's
        # own diagnostic -- the one typed-parse gate new_result.valid
        # already goes through), so a NEW blob with a duplicate id never
        # reaches this point: it is refused above, naming the id, via that
        # shared diagnostic rather than a second copy of the same check
        # here. A duplicate id on the OLD (REF) side is different since
        # the recovered-links fix (Grok re-gate MAJOR 1): old_result is
        # ALSO invalid there, but `old_links` was populated above from
        # `_recovered_old_links`, which already resolved the duplicate to
        # its FIRST occurrence -- `old_links` here carries at most one
        # entry per id either way, so the dict comprehension below never
        # has an old-side duplicate to silently pick a "last one wins"
        # winner from.
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

    return errors, changed, notes


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
                     were at REF (a commit-ish git understands). Without
                     --staged (the default), "now" means the WORKING TREE;
                     with --staged, it means the INDEX -- what `git commit`
                     would actually commit. A link's body present at REF
                     must be unchanged; its three lifecycle fields --
                     status, superseded_by, promoted_by -- may each move
                     forward once. A link removed, or a topic file deleted
                     or renamed, is an error. New links, and changes to
                     current/title/tags/code_refs/the body text outside a
                     link, are free. A REF that never parsed is repaired
                     (a note, not an error) when the new side now parses
                     cleanly -- there is no recorded link history to
                     freeze on a blob that was never validly a record.
                     REF must not start with "-" (it would otherwise
                     swallow the next flag, e.g. --staged, as if it were
                     the ref). --code-root is rejected together with
                     --against-ref (append-only mode never uses a code
                     root). Exit 1 on any append-only error; exit 2 if
                     ROOT is not inside a git repository or REF does not
                     resolve to a commit.
  --staged           compare REF to the INDEX (what `git commit` would
                     actually commit) instead of the working tree (the
                     default).

Rule reference: docs/SCHEMA.md sections 7 and 8.4; the complete table of what
this linter checks is in docs/INTERNALS.md (memlint section)."""


def _extract_against_ref_flags(argv: list[str]) -> tuple[list[str], str | None, bool, bool, str | None]:
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
    never mentioning the missing REF.

    The fifth return value, `dash_ref`, is the flag-shaped token
    immediately following `--against-ref` when it was refused as a REF
    (Grok M8): `--against-ref --staged HEAD` used to swallow the literal
    string "--staged" as REF (a GitError trying to resolve ref
    '--staged'), silently discarding the real --staged flag that followed
    it. A token starting with "-" is never consumed as REF -- it is left
    in place so the NEXT loop iteration still recognizes it as its own
    flag -- and `against_ref` stays None so main()'s "--against-ref
    requires REF" refusal fires, now naming the flag-shaped token it
    refused instead of silently misreading it."""
    rest: list[str] = []
    against_ref = None
    staged = False
    saw_against_ref = False
    dash_ref = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--against-ref":
            saw_against_ref = True
            if i + 1 < len(argv):
                candidate = argv[i + 1]
                if candidate.startswith("-"):
                    dash_ref = candidate
                else:
                    against_ref = candidate
                    i += 1
            i += 1
            continue
        if a == "--staged":
            staged = True
            i += 1
            continue
        rest.append(a)
        i += 1
    return rest, against_ref, staged, saw_against_ref, dash_ref


def _run_append_only(root_str: str, ref: str, staged: bool) -> int:
    root = Path(root_str).resolve()
    try:
        errors, changed, notes = check_append_only(root, ref, staged)
    except GitError as exc:
        print(f"memlint: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # never a bare traceback -- spec test (j)/(i)
        print(f"memlint: unexpected failure checking append-only history: {exc}", file=sys.stderr)
        return 2
    for n in notes:
        print(f"NOTE: {n}")
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
    rest, against_ref, staged, saw_against_ref, dash_ref = _extract_against_ref_flags(argv)
    if saw_against_ref and against_ref is None:
        if dash_ref is not None:
            print(
                f"--against-ref REF must not start with '-' ({dash_ref!r} looks like "
                "another option, not a ref) -- reorder the flags",
                file=sys.stderr,
            )
        else:
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
        if code_root_strs:
            # NIT-3 (whole-branch-review): silently ignoring --code-root
            # here used to let `memlint.py --against-ref HEAD STORE
            # --code-root DIR` exit 0 running only the append-only pass,
            # with no sign the flag did nothing -- rejected instead.
            print(
                "--code-root is rejected together with --against-ref "
                "(append-only mode never uses a code root)",
                file=sys.stderr,
            )
            print(USAGE, file=sys.stderr)
            return 2
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
