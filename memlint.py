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
  * any status / authority value outside the enumerated five/five in
    docs/SCHEMA.md section 3                                                -> error
  * an unknown `kind` value, or an owner-verbatim ruling.text ending in `?`
    -> warning for links recorded before the check; error under
    `--against-ref` for links new relative to REF (kind cannot be edited)
  * a topic's `standing:` pointer names a link not in that topic, not
    active, or not owner-verbatim/owner-ratified (TOP-0132 L2)        -> error
  * a topic's `standing:` pointer whose link's ruling.text has a CR/LF -> error
  * the store-wide `standing:` set exceeds 24 links or 4,800 bytes
    (UTF-8) of the complete digest                                    -> error

Exit 1 if any error was found anywhere under ROOT (warnings alone -> exit 0).

Task A2-1 (TOP-0122 L1 rule 3) adds a second, independent mode:
`memlint.py --against-ref REF [--staged] ROOT` checks the append-only
history invariant instead of the schema rules above -- see
`check_append_only` and `main`'s dispatch for the full contract.
"""
from __future__ import annotations

import functools
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import chunkers

from memidx import (
    ANCHOR_ALGO,
    ANCHOR_CAPTURE_KINDS,
    ANCHOR_DIGEST_RE,
    ANCHOR_SPANS,
    ANCHOR_WORKTREE_STATES,
    AUTHORITIES,
    CANONICAL_ID_PREFIXES,
    CANONICAL_TYPES,
    CONSTRAINT_AUTHORITIES,
    EDGE_RELS,
    HOLD_ELIGIBLE_AUTHORITIES,
    INVARIANT_KINDS,
    KINDS,
    STANDING_CAP_BYTES,
    STANDING_CAP_LINKS,
    STATUSES,
    ParseResult,
    _uncheckable_remedy,
    anchor_row_key,
    anchor_rows,
    code_ref_is_anchorable,
    code_ref_is_named,
    code_ref_matches,
    fragment_declaration_status,
    fragment_matches_symbol,
    is_binary_file,
    lang_for_source_file,
    newest_active_link,
    parse_record,
    parse_record_raw,
    code_ref_set,
    ref_latest_action,
    ref_mark_ident,
    ref_mark_rows,
    standing_digest_text,
    REF_MARK_ACTIONS,
    REF_MARK_FIELDS,
    STANDING_MARK_ACTIONS,
    STANDING_MARK_FIELDS,
    standing_ids,
    standing_line,
    standing_link_problems,
    standing_mark_ident,
    standing_mark_rows,
    standing_sort_key,
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


def _new_link_errors(prefix: str, link: dict) -> list[str]:
    """The two grandfathered link checks (owner ruling 2026-10-13): unknown
    `kind`, and a question-form `owner-verbatim` ruling text. Returns plain
    messages; the CALLER picks severity -- lint_topic (plain mode) reports
    them as warnings for every link, check_append_only (`--against-ref`)
    as errors for links absent at REF, including every link of a brand-new
    file. `kind` is append-only frozen and owner quotes may not be altered,
    so an old link can never be repaired in place; a new one is written
    correctly from the start.

    A question is not a ruling, whoever asked it (SCHEMA section 3/5);
    `owner-ratified` is the orchestrator's own paraphrase, so it is not
    covered. Trailing quote/bracket characters and whitespace are trimmed
    before looking for the final "?". Scoped to status active/provisional:
    a superseded link is history and can never be cleared by superseding
    (ruling 149)."""
    out: list[str] = []
    kind = link.get("kind")
    if kind is not None and kind not in KINDS:
        out.append(f"{prefix}: unknown kind {kind!r} (must be one of {sorted(KINDS)})")
    ruling = link.get("ruling") or {}
    if ruling.get("authority") == "owner-verbatim" and link.get("status") in ("active", "provisional"):
        text = ruling.get("text")
        if text and str(text).strip(" \t\r\n\"')]}").endswith("?"):
            out.append(f"{prefix}: owner-verbatim text is a question, not a ruling")
    return out


def _anchor_duplicate_keys(rows: list) -> list:
    """The `(ref, link)` keys that more than one anchor row carries (one
    baseline per key in this release). Shared by the plain lint and the
    --against-ref freeze so the rule is written once."""
    seen: set = set()
    dup: list = []
    for row in rows:
        key = anchor_row_key(row)
        if key in seen and key not in dup:
            dup.append(key)
        seen.add(key)
    return dup


def _anchor_errors(path, fm: dict, topic_link_ids: set) -> list[str]:
    """Validation of a topic's `anchors:` rows (docs/SCHEMA.md, "Anchors")."""
    rows = anchor_rows(fm)
    if not rows:
        return []
    errors: list[str] = []
    code_refs = {str(r) for r in fm.get("code_refs") or []}
    for i, row in enumerate(rows):
        ref = row.get("ref")
        prefix = f"{path}: anchors[{i}] ({ref!r})"
        if ref is not None and str(ref) not in code_refs and ref_latest_action(fm, str(ref)) == "remove":
            # unbound: the newest `ref_marks:` row for this ref is a `remove`, so the frozen
            # capture stays as history (a later `add` re-arms the requirement)
            pass
        elif ref is None or str(ref) not in code_refs:
            errors.append(f"{prefix}: ref is not present verbatim in this topic's code_refs "
                          "(or unbound by a newest `remove` row in ref_marks)")
        elif not code_ref_is_anchorable(str(ref)):
            errors.append(
                f"{prefix}: ref is a prefix or glob -- those are 'not checked' by definition "
                "and never carry an anchor row"
            )
        link = row.get("link")
        if link is None or str(link) not in topic_link_ids:
            errors.append(f"{prefix}: link {link!r} is not a link in this topic")
        if row.get("algo") != ANCHOR_ALGO:
            errors.append(f"{prefix}: algo {row.get('algo')!r} must be {ANCHOR_ALGO!r}")
        digest = row.get("digest")
        if not isinstance(digest, str) or not ANCHOR_DIGEST_RE.fullmatch(digest):
            errors.append(f"{prefix}: digest must be 64 lowercase hex characters")
        span = row.get("span")
        if span not in ANCHOR_SPANS:
            errors.append(f"{prefix}: unknown span {span!r} (must be one of {list(ANCHOR_SPANS)})")
        captured = row.get("captured")
        if captured not in ANCHOR_CAPTURE_KINDS:
            errors.append(
                f"{prefix}: unknown captured {captured!r} (must be one of {list(ANCHOR_CAPTURE_KINDS)})"
            )
        worktree = row.get("worktree")
        if worktree not in ANCHOR_WORKTREE_STATES:
            errors.append(
                f"{prefix}: unknown worktree {worktree!r} (must be one of {list(ANCHOR_WORKTREE_STATES)})"
            )
        if span == "symbol" and not row.get("chunker_version"):
            errors.append(f"{prefix}: span: symbol requires chunker_version")
        captured_at = row.get("captured_at")
        if captured_at is None or isinstance(captured_at, (dict, list)) or str(captured_at).strip() == "":
            errors.append(f"{prefix}: captured_at is required (part of the row's identity)")
    for ref, link in _anchor_duplicate_keys(rows):
        errors.append(
            f"{path}: anchors: more than one row for ({ref!r}, {link!r}) -- one baseline per "
            "key in this release; a new baseline is a new link"
        )
    return errors


_MARK_RECORDED_AT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")


def _mark_time_problems(row: dict) -> list[str]:
    """`recorded_at` must be ISO-8601 with an offset and `date` its date part
    as written (the row's own offset, never converted)."""
    recorded_at = row.get("recorded_at")
    if not isinstance(recorded_at, str) or not _MARK_RECORDED_AT_RE.fullmatch(recorded_at):
        return ["recorded_at must be a quoted ISO-8601 timestamp with an offset "
                "(2026-11-03T09:12:41-05:00)"]
    try:
        datetime.fromisoformat(recorded_at)
    except ValueError:
        return ["recorded_at is not a valid ISO-8601 timestamp"]
    if str(row.get("date")) != recorded_at[:10]:
        return [f"date {str(row.get('date'))!r} is not the date part of recorded_at ({recorded_at[:10]!r})"]
    return []


def _mark_rows_errors(path, rows: list, block: str, fields, actions, id_field: str, ident_of,
                      authority_problem, member_problem) -> list[str]:
    """The shared plain-mode validation of an audit block's rows (`standing_marks:`,
    `ref_marks:`): required fields present and scalar, no unknown fields, the
    action, `recorded_by` a string, `why` one line, `recorded_at`/`date` shape,
    one row per identity. What differs per block is passed in: `authority_problem(
    value)` and `member_problem(value)` return a message tail or None."""
    errors: list[str] = []
    seen: set = set()
    for i, row in enumerate(rows):
        prefix = (f"{path}: {block}[{i}] ({row.get(id_field)!r}, {row.get('action')!r}, "
                  f"{row.get('recorded_at')!r})")
        missing = [f for f in fields
                   if row.get(f) is None or isinstance(row.get(f), (dict, list))
                   or str(row.get(f)).strip() == ""]
        unknown = [str(k) for k in row if k not in fields]
        if missing:
            errors.append(f"{prefix}: missing field(s) {missing}")
        if unknown:
            errors.append(f"{prefix}: unknown field(s) {unknown}")
        if "action" not in missing and row.get("action") not in actions:
            errors.append(f"{prefix}: action {row.get('action')!r} must be one of "
                          f"{list(actions)}")
        if "authority" not in missing:
            problem = authority_problem(row.get("authority"))
            if problem:
                errors.append(f"{prefix}: {problem}")
        if "recorded_by" not in missing and not isinstance(row.get("recorded_by"), str):
            errors.append(f"{prefix}: recorded_by {row.get('recorded_by')!r} is not a string "
                          "(YAML read it as another type; quote it)")
        why = row.get("why")
        if isinstance(why, str) and ("\n" in why or "\r" in why):
            errors.append(f"{prefix}: why must be a single line (no CR/LF)")
        if "recorded_at" not in missing:
            errors.extend(f"{prefix}: {p}" for p in _mark_time_problems(row))
        member = row.get(id_field)
        if member is not None:
            problem = member_problem(member)
            if problem:
                errors.append(f"{prefix}: {problem}")
        ident = ident_of(row)
        if ident is not None:
            if ident in seen:
                errors.append(f"{prefix}: duplicate row -- ({id_field}, action, recorded_at) "
                              "identifies a row once")
            seen.add(ident)
    return errors


def _standing_marks_errors(path, fm: dict, topic_link_ids: set) -> list[str]:
    """Validation of a topic's `standing_marks:` rows (docs/SCHEMA.md, "Standing
    marks"). Rows are NEVER required: a topic marked before this block existed
    carries none and is clean (TOP-0136 L1)."""
    def authority_problem(value):
        if value in CONSTRAINT_AUTHORITIES:
            return None
        return (f"authority {value!r} must be one of {sorted(CONSTRAINT_AUTHORITIES)} (an "
                "agent-inference mark would put a constraint in every session start)")

    def member_problem(link):
        return None if str(link) in topic_link_ids else f"link {link!r} is not a link in this topic"

    return _mark_rows_errors(path, standing_mark_rows(fm), "standing_marks", STANDING_MARK_FIELDS,
                             STANDING_MARK_ACTIONS, "link", standing_mark_ident,
                             authority_problem, member_problem)


def _ref_marks_errors(path, fm: dict) -> list[str]:
    """Validation of a topic's `ref_marks:` rows (docs/SCHEMA.md, "Reference
    marks"). Narrower than the standing rules: a ref is a path, not a link id,
    and any of the five authorities is legal (a bind is not a constraint).
    Rows are NEVER required by the plain lint: a topic whose references predate
    the block carries none and is clean."""
    def authority_problem(value):
        return None if value in AUTHORITIES else (
            f"authority {value!r} must be one of {sorted(AUTHORITIES)}")

    def member_problem(ref):
        if not isinstance(ref, str):
            return f"ref {ref!r} is not a string (YAML read it as another type; quote it)"
        return None if code_ref_is_named(ref) else f"ref {ref!r} is empty or fragment-only"

    return _mark_rows_errors(path, ref_mark_rows(fm), "ref_marks", REF_MARK_FIELDS,
                             REF_MARK_ACTIONS, "ref", ref_mark_ident,
                             authority_problem, member_problem)


def _standing_pointer_errors(path, fm: dict, topic_links_by_id: dict) -> list[str]:
    """Duplicate ids in `standing:` and the eligibility of every pointer
    (memidx.standing_link_problems). One definition for the plain-mode
    `lint_topic` and for the staged blob in `check_append_only` (TOP-0137 L14:
    the hook lints the worktree, so the staged side needs the same rules)."""
    errors: list[str] = []
    standing_seen: set = set()
    for lid in fm.get("standing") or []:
        lid_str = str(lid)
        if lid_str in standing_seen:
            errors.append(f"{path}: standing lists {lid_str!r} more than once")
            continue
        standing_seen.add(lid_str)
        errors.extend(
            f"{path}: {problem}"
            for problem in standing_link_problems(lid_str, topic_links_by_id.get(lid_str))
        )
    return errors


def _staged_standing_errors(path, fm: dict, links: list) -> list[str]:
    """The plain-mode `standing:` / `standing_marks:` / `ref_marks:` rules, run on
    the blob that is about to be committed (rows' fields and authority, duplicate
    and ineligible pointers)."""
    by_id = {str(l.get("link")): l for l in links if l.get("link")}
    return (_standing_pointer_errors(path, fm, by_id) + _standing_marks_errors(path, fm, set(by_id))
            + _ref_marks_errors(path, fm))


def lint_topic(
    path: Path,
    fm: dict,
    code_roots: list[Path] | None = None,
    strict_citations: bool = False,
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    links = fm.get("links") or []

    for link in links:
        name = link.get("link", "?")
        prefix = f"{path}:{name}"

        if code_roots:
            ruling_for_citations = link.get("ruling") or {}
            cite_errors, cite_warnings = _citation_errors(
                prefix,
                ruling_for_citations.get("source"),
                link.get("evidence"),
                code_roots,
                strict=strict_citations,
            )
            errors.extend(cite_errors)
            warnings.extend(cite_warnings)

        status = link.get("status")
        if status is not None and status not in STATUSES:
            errors.append(f"{prefix}: unknown status {status!r} (must be one of {sorted(STATUSES)})")

        # Grandfathered pair (owner, 2026-10-13): the kind-vocabulary check
        # and the question-form owner-verbatim check are WARNINGS here (plain
        # mode re-judges every recorded link, and history cannot be edited
        # to satisfy a later-stricter rule); they are ERRORS for links NEW
        # relative to the committed state, enforced by `--against-ref`
        # (check_append_only -> _new_link_errors). One definition, both modes.
        warnings.extend(
            f"{e} [grandfathered: an error for new links under --against-ref]"
            for e in _new_link_errors(prefix, link)
        )
        # Declined-rulings rule (DESIGN-declined-check): same severity split.
        # This loop judges the link's own topic only; plain mode ALSO warns
        # across topics, in the grandfathered cross-topic pass in lint_root
        # after the whole store is read (do not drop it: a brand-new topic's
        # re-adoption is an --against-ref error, a warning in plain mode).
        topic_id = str(fm.get("id") or Path(str(path)).stem)
        warnings.extend(
            f"{readoption_message(prefix, topic_id, row)} "
            "[grandfathered: an error for new links under --against-ref]"
            for row in readoption_matches(link, rejection_rows(links), _link_number(link.get("link")))
        )

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

    errors.extend(_anchor_errors(path, fm, topic_link_ids))

    # TOP-0132 L2: `standing:` names which of THIS topic's own links are
    # true of the whole project, always -- never derived, never a per-link
    # flag (see the design record's rejection of both). A non-list shape is
    # already a quarantining error from validate_record_shape (same
    # treatment as tags/code_refs) and never reaches here; every entry
    # that DOES reach here must resolve inside this topic to an ACTIVE
    # link at the declared CONSTRAINT gate -- a pointer to a superseded
    # link is an error ON PURPOSE (it forces the supersession and the
    # pointer update into the same commit, per the design record), not
    # something this linter silently drops.
    # Eligibility is memidx.standing_link_problems -- the one definition
    # `mark-standing --add` shares. (Fix-round MAJOR, both reviewers: a CR/LF
    # in a standing ruling.text is rejected here; every other Unicode line
    # separator or tab is collapsed to one space by `standing_line` at
    # projection time -- defence in depth, not a lint error.)
    errors.extend(_standing_pointer_errors(path, fm, topic_links_by_id))

    errors.extend(_standing_marks_errors(path, fm, topic_link_ids))
    errors.extend(_ref_marks_errors(path, fm))

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


# ---------------------------------------------------------------------------
# Cited commit / file:line verification (docs/SCHEMA.md sec7/sec9 addendum;
# INC-0124 -- "invented commit subjects" is the one class of fabrication that
# is mechanically checkable: a source:/evidence: entry naming a commit can be
# verified against the wired code repository's history, the same way
# code_refs/implemented_by are already verified against paths above). Gated
# on code_roots exactly like lint_concept's own checks: nothing here is
# checkable with no root, and both lint_topic's and lint_record's citation
# pass are skipped entirely when code_roots is empty -- no line is printed
# about citations at all in that case (never noise for a store that never
# asked for this check).
#
# THE RECOGNIZER IS THE WHOLE DIFFICULTY (see module docstring's design
# note): this store's own evidence carries many hex-looking tokens that are
# NOT commits -- 16-hex snapshot hashes, sha256 prefixes, session ids,
# render fingerprints, TOP-xxxx ids, dates. Three independent, narrow
# patterns, not one greedy one:
#
#   (a) COMMIT/MERGE-triggered, subject-eligible: a hex run immediately
#       preceded by "commit " or "merge " -- the only two words this
#       schema's own §3 example pairs with a quoted subject
#       (`commit a1b2c3d "the exact subject"`) -- MAY also carry a claimed
#       subject right after it.
#   (b) AT/AS-triggered, subject-INeligible: a hex run immediately preceded
#       by "at " (as in "gate on PR #18 at f7fe011") or " as " (as in
#       "merged as 87663ec") -- this store's own terse cross-reference
#       words, which never carry a subject in real use. Deliberately never
#       subject-eligible even when a quote happens to follow (fix round,
#       Opus MINOR): this store's owner-verbatim convention often puts a
#       quote of the OWNER'S words shortly after ANY kind of reference --
#       "...gate at af95af2 "Codex is back, use it rather than Opus""
#       quotes the owner about something unrelated, not the commit's
#       subject -- and treating that as a subject claim produced a false
#       ERROR (the real commit's actual subject never matches an
#       unconnected owner quote). Splitting the subject-eligible grammar
#       out of the at/as alternative entirely, rather than trying to
#       pattern-match "which quote is real," is what fixes this class
#       outright instead of chasing one fixture.
#   (c) BACKTICK-wrapped: a hex run inside `` `...` `` with no context word
#       at all (a reviewer note quoting a bare hash) -- also never
#       subject-eligible, for the same reason as (b).
#
# ALL THREE shapes require the hex run to be EXACTLY 7 or 40 characters
# long -- git's two canonical hash lengths (abbreviated short form, full
# form) -- enforced with a trailing \b so a longer OR SHORTER run (an
# 8-hex near-miss, a 12-hex render fingerprint, a 16-hex snapshot hash, a
# 40+ non-hash token) never partially matches at either length: this store
# carries `` `e28e83a8` `` (8-hex) three times and none of them are read as
# commits. This is what keeps the recognizer precise rather than greedy:
# verified against this project's own real store (2026-09-19), it is what
# correctly EXCLUDES "rendered by 118974ef7600 against an engine at
# 049884e8b2ed" (INC-0117 evidence; both 12-hex, both render fingerprints,
# "at" is a trigger word but the length gate rejects them) while still
# catching every real 7-char commit hash the store cites through
# "commit "/"merge "/"at "/" as ".
#
# A CLAIMED SUBJECT (branch (a) only) must be a double-quoted string on the
# SAME LINE as the hash, separated only by spaces/tabs, and containing no
# newline itself -- both restrictions exist for the same reason (fix round,
# Opus MINOR): the original `\s*"..."` used `\s` (matches newline) and `.`
# implicitly via `[^"]` (also matches newline), so a YAML `|` block scalar
# -- which preserves real newlines -- let "commit X" bind to a completely
# unrelated quoted sentence many PARAGRAPHS later, as long as no other `"`
# appeared first. `[ \t]*"[^\n]*"` makes both directions of that
# impossible: a hash and its subject must share one physical line.
#
# THE QUOTE CAPTURE IS GREEDY TO THE LAST `"` ON THE LINE (fix round,
# second pass, MINOR): a real commit subject can itself contain a quote
# (this repo's own history has several -- `git log --format=%s | grep -c
# '"'` is 8 as of this writing), and a NON-greedy `[^"\n]*` stopped at the
# FIRST inner quote, so `commit abc1234 "Fix the "foo" parser"` captured
# only `Fix the ` -- an unconditional ERROR against the real subject,
# exactly the false-accusation class this whole feature exists to avoid.
# `[^\n]*"` (greedy, backtracking only as needed) matches to the LAST `"`
# on the line instead, so an inner quote survives intact. Residual known
# miss: two SEPARATE quoted strings after one hash on one line
# (`commit abc1234 "real subject" and also "an unrelated quote"`) still
# over-captures everything between the first and the last quote as one
# claimed subject -- not solvable without knowing which quote is the
# subject and which is unrelated prose, the same fundamental ambiguity
# that motivated restricting subject-eligibility to commit/merge triggers
# in the first place. Not observed in this store's real citations today.
#
# KNOWN MISSES, by design, not oversight:
#   - a bare hash with NO context word and no backticks ("...fixed by
#     e21bfa0 the same day") is invisible -- the whole point of requiring a
#     trigger is refusing to guess that an arbitrary hex-looking word is a
#     commit.
#   - "against <hash>" (used by this store's own append-only NOTE lines,
#     e.g. INC-0124's own evidence) is not a trigger word, on purpose: this
#     store already uses "commit "/"at " for a CITATION and "against" for
#     describing a git compare, and folding "against" in would flag prose
#     that names a ref, not a claim about that ref's authenticity.
#   - an ordinary ENGLISH WORD that happens to be exactly 7 characters, all
#     drawn from a-f, is indistinguishable from a short hash once it
#     follows "at "/"as " -- "deadbee" and "acceded" are both valid
#     lowercase hex AND plausible prose (Grok fix-round finding). Not
#     solvable by the recognizer (it cannot know English from hex); a false
#     positive here reads as an unresolved WARNING, never an ERROR, unless
#     --strict-citations is set for a store that promises never to do this.
#   - a BACKTICK-wrapped 40-hex token is indistinguishable by design from a
#     sha256 (or other 40-hex) prefix that is not a commit at all -- a
#     40-character hex string could be either, and there is no mechanical
#     way to tell them apart without resolving it (which is exactly what
#     this checker then does; a non-commit 40-hex string simply reports as
#     an unresolved WARNING, same as any other miss).
#   - a hash quoted alongside `hash: rest-of-sentence` (no context word
#     before the hash) is invisible -- see (a)/(b) above; this is
#     deliberate, not a gap discovered late.
#   - a comma- or second-colon-separated line locator ("progress.md:86,89",
#     "file.py:123:45", a "-"-joined range "file.py:123-130") -- only the
#     FIRST number after the first colon is ever checked; the citation
#     format this schema documents is one path, one line.
#   - `file.py:L123` (an "L"-prefixed line number, a shape this store does
#     not use but some do) is invisible -- the line-number group requires
#     bare digits immediately after the colon.
#   - only a DOUBLE-QUOTED string immediately following a commit/merge hash
#     -- same line, spaces/tabs only in between, see above -- is treated as
#     a claimed subject. A parenthetical, a colon-joined sentence, or a
#     quote separated by other punctuation is never read as a subject
#     claim -- this store's real citations never quote a subject this way
#     today, so being strict here costs nothing on the real store and
#     avoids inventing a subject out of unrelated prose that happens to
#     follow a hash.
#   - a citation to a commit or a file:line that is real but lives in a
#     DIFFERENT repository than the one --code-root points at resolves to
#     NOTHING here, indistinguishably from a fabricated one -- this checker
#     has exactly one code root's worth of ground truth and cannot tell the
#     two apart. That is why "not found" is a WARNING by default (worded
#     "unverifiable, not necessarily wrong") rather than an ERROR: an ERROR
#     it cannot substantiate is a false accusation. --strict-citations
#     promotes it anyway, for a store known to cite only the wired repo.
#     See the shipping report for the real instances this surfaced against
#     this project's own store.
# ---------------------------------------------------------------------------

_COMMIT_CITE_RE = re.compile(
    r"""
    (?:
        \b(?i:commit|merge)\b\s+(?P<hash_ctx_subj>[0-9a-f]{40}|[0-9a-f]{7})\b
        (?:[ \t]*"(?P<subject>[^\n]*)")?
      |
        \b(?i:at|as)\b\s+(?P<hash_ctx_bare>[0-9a-f]{40}|[0-9a-f]{7})\b
      |
        `(?P<hash_bt>[0-9a-f]{40}|[0-9a-f]{7})`
    )
    """,
    re.VERBOSE,
)

# path/to/file.ext:123[-456] -- the path portion must carry a dotted
# extension (so "16:09 EDT" and a bare "TOP-0124:5"-shaped token never
# match: neither contains a "."), and must not be immediately preceded by
# another path/word character, a colon, OR A BACKSLASH -- the colon is
# what keeps a URL's "host.tld:port" from matching (http://host.tld:port/
# path.py:12 would otherwise start matching right after "http:", where the
# two slashes are themselves swallowed into the path group; excluding a
# colon immediately before the match start closes that, since this
# store's own "field: value" YAML lines always have a space after the
# colon, never a bare path glued to it). The backslash exclusion (fix
# round, Grok MINOR) closes a separate hole: `\` was in neither the path
# character class nor this lookbehind, so a Windows-style
# "hooks\missing.py:3" matched starting right after the backslash,
# silently citing "missing.py" -- a real, different file at the store
# root, if one happened to exist there -- instead of correctly matching
# nothing at all (this store's own paths are always forward-slashed, so a
# backslash immediately before a candidate path is never legitimate).
# The `~` exclusion (fix round, second pass, NIT) closes the same class of
# hole as the backslash one: `~/dev/three.py:4` used to match starting
# right after the `~`, capturing `/dev/three.py:4` -- which then read as
# an ABSOLUTE path and produced an "is absolute" ERROR naming
# `/dev/three.py`, a path nobody actually wrote. `~` immediately before a
# candidate path is now excluded the same way `\` already is.
# KNOWN MISS, not fixed here (no specific repair requested; the citation
# format this schema documents assumes no spaces in a path): a path
# containing a literal space ("my t.py:123") still matches only its
# suffix after the space ("t.py:123"), the same "silently take a
# basename" shape as the backslash case, for the structural reason that
# nothing marks where such a path starts. Only the FIRST line number of a
# "path:123-456" or "path:123,456" citation is captured -- a range's or
# list's remaining numbers, and an "L"-prefixed line number
# ("file.py:L123"), are known misses (see module comment above). A
# Windows drive-letter path ("C:/x/three.py:1") is SILENTLY not a
# citation at all, the same class as the backslash miss: the colon right
# after "C" is excluded from the lookbehind the same way any other bare
# colon is (this store's own "field: value" YAML convention), so nothing
# ever starts matching partway through it either. A "version 0.2.0:1" or
# "v0.2.0:3"-shaped token (a version number, not a path, followed by a
# colon and a small integer) reads as a file:line citation -- ".2.0"
# satisfies the dotted-extension requirement -- and resolves to a WARNING
# (not found), never an ERROR, unless --strict-citations is set.
_FILE_LINE_CITE_RE = re.compile(
    r"(?<![\w./:\\~-])([A-Za-z0-9_.\-/]+\.[A-Za-z0-9]{1,8}):(\d+)(?:[-,]\d+)?"
)


@functools.lru_cache(maxsize=None)
def _is_git_repo(root: Path) -> bool:
    """Cached (real-store cost: ~230 link/record citation checks would
    otherwise each re-spawn `git rev-parse` for the same one or two code
    roots): a --code-root that is not a git repository at all (or any
    ancestor of it) contributes nothing to commit-hash resolution -- callers
    skip it rather than erroring, so a plain filesystem checkout (the shape
    every existing code_roots-bearing test in this suite already uses) keeps
    the commit-citation check silently off while file:line citations, which
    need only the filesystem, are unaffected. A root that does not exist on
    disk at all (a typo'd --code-root) is also just "not a git repo" here,
    never a crash: `_run_git` would otherwise raise GitError from
    subprocess.run's own FileNotFoundError on a missing cwd, which nothing
    in the ordinary schema-lint path catches (only --against-ref's dispatch
    does) -- every other --code-root consumer (lint_concept's path.exists(),
    lint_markers' walk) already tolerates a bad root by reporting "not
    found," so this one does too rather than turning a typo into a
    traceback."""
    if not root.is_dir():
        return False
    try:
        proc = _run_git(["-C", str(root), "rev-parse", "--git-dir"], root)
    except GitError:
        return False
    return proc.returncode == 0


def _commit_resolves(root: Path, commit_hash: str) -> bool:
    """The one mechanism the task names explicitly: `git -C root cat-file -e
    <hash>^{commit}`. Kept as its own one-line function (never folded into
    `_commit_subject`, which would also need a second git call anyway) so a
    test can stub exactly this call and nothing else (acceptance 7)."""
    proc = _run_git(["-C", str(root), "cat-file", "-e", f"{commit_hash}^{{commit}}"], root)
    return proc.returncode == 0


def _commit_subject(root: Path, commit_hash: str) -> str | None:
    """`git log -1 --format=%s <hash>` -- None if the hash does not resolve
    in this particular root (a caller that already knows it resolves, e.g.
    via _commit_resolves against a DIFFERENT root in a multi-root store,
    should not read None here as "does not exist anywhere.")"""
    proc = _run_git(["-C", str(root), "log", "-1", "--format=%s", f"{commit_hash}^{{commit}}"], root)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace").rstrip("\n")


def _extract_commit_citations(text: str) -> list[dict]:
    """Every commit citation _COMMIT_CITE_RE finds in one string, as
    {"hash": str, "subject": str | None} -- "subject" is the same-line
    quoted text immediately following a commit/merge-triggered hash, when
    present, else None. An at/as-triggered or backtick-wrapped hash is
    never subject-eligible (see module comment) even when a quote happens
    to follow it in the source text."""
    out = []
    for m in _COMMIT_CITE_RE.finditer(text):
        h = m.group("hash_ctx_subj") or m.group("hash_ctx_bare") or m.group("hash_bt")
        subject = m.group("subject") if m.group("hash_ctx_subj") else None
        out.append({"hash": h, "subject": subject})
    return out


def _extract_file_line_citations(text: str) -> list[tuple[str, int]]:
    """Every "path:line" _FILE_LINE_CITE_RE finds in one string, as
    (path, line_number)."""
    return [(m.group(1), int(m.group(2))) for m in _FILE_LINE_CITE_RE.finditer(text)]


def _normalize_subject(s: str) -> str:
    """Whitespace-normalized comparison, chosen (docs/SCHEMA.md sec7
    addendum) as the safe default over an exact byte compare: a citation
    typed by hand into YAML is expected to preserve the commit subject's
    WORDS, not necessarily its exact run of internal whitespace, and a
    stricter compare would flag a cosmetic re-wrap as a fabrication -- the
    one thing this check must never do (INC-0124's own lesson: a guard for
    fabrication must not itself cry wolf on a harmless reformat)."""
    return " ".join(s.split())


# ---------------------------------------------------------------------------
# Declined rulings checked on the write path (0.6.0; docs/internal/
# DESIGN-declined-check.md). ONE comparator, used by the filing command
# (memidx.py) and by the commit-time rule below: what a topic REJECTED, whether
# the rejection is still operative, and whether a new link re-adopts it
# verbatim. The only normaliser is _normalize_subject (whitespace collapse);
# no substring, case-fold or semantic test (a paraphrase is shown, never
# flagged).
# ---------------------------------------------------------------------------

def _link_number(link_id) -> int | None:
    """The integer of a link id like "L12", or None."""
    m = re.fullmatch(r"L(\d+)", str(link_id or "").strip())
    return int(m.group(1)) if m else None


def rejection_rows(links: list) -> list[dict]:
    """Every rejection a topic records: a link with `kind: declined` or
    `status: declined` (its ruling.text is the rejected sentence) and each
    `alternatives[].option` of any link. Each row: link, num, alt (index or
    None), sentence, reason, restored_by (id of the EARLIEST later link that
    is `status: active` and `reverses` the row's link, else None) and
    restored_num. An alternative cannot be named alone, so reversing its link
    restores all of that link's alternatives. Superseding is not restoring."""
    links = [l for l in links if isinstance(l, dict)]
    rows: list[dict] = []
    for l in links:
        lid, num = str(l.get("link")), _link_number(l.get("link"))
        if num is None:
            continue
        ruling = l.get("ruling") if isinstance(l.get("ruling"), dict) else {}
        if (l.get("kind") == "declined" or l.get("status") == "declined") and ruling.get("text"):
            rat = l.get("rationale") if isinstance(l.get("rationale"), dict) else {}
            rows.append({"link": lid, "num": num, "alt": None, "sentence": str(ruling["text"]),
                         "reason": str(rat.get("text") or "")})
        alts = l.get("alternatives")
        for i, alt in enumerate(alts if isinstance(alts, list) else []):
            if isinstance(alt, dict) and alt.get("option"):
                rows.append({"link": lid, "num": num, "alt": i, "sentence": str(alt["option"]),
                             "reason": str(alt.get("rejected_because") or "")})
    for row in rows:
        row["restored_by"], row["restored_num"] = None, None
        for l in links:
            n = _link_number(l.get("link"))
            if (n is not None and n > row["num"] and l.get("status") == "active"
                    and str(l.get("reverses") or "") == row["link"]
                    and (row["restored_num"] is None or n < row["restored_num"])):
                row["restored_by"], row["restored_num"] = str(l.get("link")), n
    return rows


def rejection_is_operative(row: dict, as_of: int | None = None) -> bool:
    """Operative unless restored; with `as_of` (a link number) a restoration
    LATER than that link does not count -- the state when that link was written."""
    if row["restored_num"] is None:
        return True
    return as_of is not None and row["restored_num"] > as_of


def readoption_matches(link: dict, rows: list[dict], as_of: int | None = None,
                       own_topic: bool = True) -> list[dict]:
    """The operative rows `link` re-adopts verbatim (DESIGN section 3):
    kind `adopted` (or `restored` without `reverses`), status `active` or
    `provisional`, no `reverses`, and the whitespace-normalised ruling text
    equal to a rejected sentence. `own_topic`: the rows come from the link's
    own topic, so only rejections at a LOWER link number than the link count
    (a link never re-adopts itself)."""
    if link.get("reverses") or link.get("kind") not in ("adopted", "restored"):
        return []
    if link.get("status") not in ("active", "provisional"):
        return []
    ruling = link.get("ruling") if isinstance(link.get("ruling"), dict) else {}
    text = ruling.get("text")
    if not text:
        return []
    want = _normalize_subject(str(text))
    num = _link_number(link.get("link"))
    out = []
    for row in rows:
        if own_topic and num is not None and row["num"] >= num:
            continue
        if not rejection_is_operative(row, as_of if own_topic else None):
            continue
        if _normalize_subject(row["sentence"]) == want:
            out.append(row)
    return out


def rejection_ref(topic_id: str, row: dict) -> str:
    """`<TOP> <Ln>` or `<TOP> <Ln> alternative <i>` (1-based), as printed."""
    return f"{topic_id} {row['link']}" + ("" if row["alt"] is None else f" alternative {row['alt'] + 1}")


def readoption_message(prefix: str, topic_id: str, row: dict) -> str:
    return (f"{prefix}: re-adopts a rejected option recorded at {rejection_ref(topic_id, row)} "
            "without reverses")


def _topic_rejection_rows(fm: dict) -> list:
    links = fm.get("links")
    if not isinstance(links, list):
        return []
    return [(str(fm.get("id") or ""), r) for r in rejection_rows(links)]


def _rejections_of_parsed(result, stem: str) -> list:
    """`(topic_id, row)` for one parsed record (nothing unless it is a valid topic)."""
    if not result.valid or not _is_topic_frontmatter(result.frontmatter):
        return []
    fm = dict(result.frontmatter)
    fm["id"] = fm.get("id") or stem
    return _topic_rejection_rows(fm)


def _staged_md_blobs(toplevel: Path, root_prefix: str):
    """`(path_in_index, blob)` for every regular `.md` file the INDEX holds
    under the store (symlinks, dot-directories and node_modules skipped, as
    walk_markdown does for the working tree). Unmerged stages collapse to one."""
    proc = _run_git(["ls-files", "-s", "-z", "--", root_prefix.rstrip("/") or "."], toplevel)
    if proc.returncode != 0:
        return
    seen: set = set()
    for rec in proc.stdout.split(b"\x00"):
        meta, _tab, raw = rec.partition(b"\t")
        if not raw or meta.split(b" ")[0] == b"120000":
            continue
        rel = raw.decode("utf-8", "surrogateescape")
        parts = rel[len(root_prefix):].split("/") if root_prefix and rel.startswith(root_prefix) else rel.split("/")
        if rel in seen or not rel.endswith(".md") or any(x.startswith(".") or x == "node_modules" for x in parts):
            continue
        seen.add(rel)
        yield rel, _git_show(toplevel, f":{rel}")


def _store_rejections(root: Path, staged: tuple | None = None) -> list:
    """`(topic_id, row)` for every parseable topic of the store. One reader
    for both sources, `_parse_git_blob` (CRLF-tolerant, the filing flow's
    parse): the working tree by default, or -- `staged=(toplevel, root_prefix)`
    -- the INDEX (the pre-commit hook's contract), never the working tree. A
    file that does not parse contributes nothing."""
    out: list = []
    if staged is not None:
        for rel, blob in _staged_md_blobs(*staged):
            out.extend(_rejections_of_parsed(_parse_git_blob(blob, rel), Path(rel).stem))
        return out
    for f in sorted(walk_markdown(root)):
        out.extend(_rejections_of_parsed(_parse_git_blob(_read_worktree(f), f), f.stem))
    return out


def _citation_errors(
    prefix: str, source, evidence, code_roots: list[Path], strict: bool = False
) -> tuple[list[str], list[str]]:
    """The shared check behind lint_topic's per-link pass and lint_record's
    standalone-record pass: scans `source` (a single free-text string, e.g.
    ruling.source) and `evidence` (a list of free-text strings) for commit
    and file:line citations and verifies each against `code_roots`. `prefix`
    is everything the caller wants before the citation's own description
    (already includes the file path and, for a link, its id). Returns
    (errors, warnings) -- one string per mismatch, naming the field/index,
    the citation, and what went wrong.

    UNVERIFIABLE vs SUBSTANTIATED (coordinator ruling, 2026-09-19, after this
    checker's own real-store run surfaced a topic that legitimately cites
    another project's installer files, and INC-0124 citing an IceKEY commit):
    with a single --code-root, "does not resolve" cannot be told apart from
    "lives in a different repository" -- an ERROR for that case is a false
    accusation the checker cannot back up. So a citation that resolves to
    NOTHING under any configured root (a hash cat-file cannot find; a file
    that is not found at all) is a WARNING by default, worded "unverifiable,
    not necessarily wrong" -- and promotes to an ERROR only when `strict` is
    set (--strict-citations, for a store whose records are known to cite
    only the wired repo). A SUBSTANTIATED mismatch -- the hash resolves but
    the quoted subject differs; the file exists but has fewer lines than
    cited -- stays an ERROR unconditionally: the checker verified something
    concrete and it was wrong, which is exactly the fabrication class
    INC-0124 exists to catch, not a stylistic nit. An absolute-path citation
    is also always an ERROR, strict or not: it violates the "relative to a
    code root" citation shape categorically, independent of which repository
    anything lives in, so there is nothing unverifiable about it.

    Skips the commit-hash half of the check entirely when NONE of
    code_roots is a git repository (the file:line half still runs -- it
    needs only the filesystem). When several code_roots are given, a hash
    is accepted if it resolves under ANY git root (content-addressed, so
    "found in two roots" is not the ambiguity code_refs/implemented_by
    guard against for a plain path) and a file:line citation is accepted if
    it exists under ANY root.

    Roots are `.resolve()`d up front, exactly like lint_concept's own
    `resolved_roots` -- caught on macOS CI (Grok/first-CI-run finding): a
    root under a path with a symlinked component (macOS's own
    `/var` -> `/private/var`, which is where `tempfile.mkdtemp()` lands)
    made `full.relative_to(root)` raise ValueError even for a file that
    plainly exists under it, because `full` was resolved (following the
    symlink) while `root` was not -- a real file:line citation was then
    reported as "not found" purely from that mismatch, not from the file
    actually being absent."""
    errors: list[str] = []
    warnings: list[str] = []

    def _unresolved(msg: str) -> None:
        (errors if strict else warnings).append(msg)

    code_roots = [r.resolve() for r in code_roots]
    git_roots = [r for r in code_roots if _is_git_repo(r)]
    roots_desc = ", ".join(str(r) for r in code_roots)

    fields: list[tuple[str, str]] = []
    if isinstance(source, str) and source.strip():
        fields.append(("source", source))
    if isinstance(evidence, list):
        for i, item in enumerate(evidence):
            if isinstance(item, str) and item.strip():
                fields.append((f"evidence[{i}]", item))

    for field_name, text in fields:
        if git_roots:
            for cite in _extract_commit_citations(text):
                h = cite["hash"]
                hit_root = None
                for root in git_roots:
                    if _commit_resolves(root, h):
                        hit_root = root
                        break
                if hit_root is None:
                    _unresolved(
                        f"{prefix}: {field_name} cites commit {h!r} -- not found under any "
                        f"configured code root tried ({', '.join(str(r) for r in git_roots)}); "
                        f"unverifiable, not necessarily wrong"
                    )
                    continue
                if cite["subject"] is not None:
                    actual = _commit_subject(hit_root, h)
                    if actual is None or _normalize_subject(actual) != _normalize_subject(cite["subject"]):
                        errors.append(
                            f"{prefix}: {field_name} cites commit {h!r} with subject "
                            f"{cite['subject']!r} -- actual subject is {actual!r}"
                        )
        for ref_path, line_no in _extract_file_line_citations(text):
            if Path(ref_path).is_absolute():
                errors.append(
                    f"{prefix}: {field_name} cites {ref_path!r}:{line_no} -- path is absolute, "
                    f"must be relative to a code root ({roots_desc})"
                )
                continue
            hit = None
            for root in code_roots:
                full = (root / ref_path).resolve()
                try:
                    full.relative_to(root)
                except ValueError:
                    continue
                if full.exists() and full.is_file():
                    hit = full
                    break
            if hit is None:
                _unresolved(
                    f"{prefix}: {field_name} cites {ref_path!r}:{line_no} -- not found under any "
                    f"configured code root tried ({roots_desc}); unverifiable, not necessarily wrong"
                )
                continue
            try:
                with hit.open(encoding="utf-8", errors="ignore") as fh:
                    line_count = sum(1 for _ in fh)
            except OSError:
                line_count = 0
            if line_count < line_no:
                errors.append(
                    f"{prefix}: {field_name} cites {ref_path!r}:{line_no} -- file has only "
                    f"{line_count} line(s)"
                )
    return errors, warnings


def lint_record(
    path: Path,
    fm: dict,
    code_roots: list[Path] | None = None,
    strict_citations: bool = False,
) -> tuple[list[str], list[str]]:
    """Enum-validate a standalone (non-topic) record's top-level status/authority,
    plus (SCHEMA sec7/sec9 addendum, INC-0124) verify any commit/file:line
    citations in its top-level source:/evidence: fields -- see
    _citation_errors."""
    errors: list[str] = []
    warnings: list[str] = []
    status = fm.get("status")
    if status is not None and status not in STATUSES:
        errors.append(f"{path}: unknown status {status!r} (must be one of {sorted(STATUSES)})")
    authority = fm.get("authority")
    if authority is not None and authority not in AUTHORITIES:
        errors.append(f"{path}: unknown authority {authority!r} (must be one of {sorted(AUTHORITIES)})")
    if code_roots:
        cid = fm.get("id") or path.stem
        cite_errors, cite_warnings = _citation_errors(
            f"{path}: {cid}", fm.get("source"), fm.get("evidence"), code_roots, strict=strict_citations
        )
        errors.extend(cite_errors)
        warnings.extend(cite_warnings)
    return errors, warnings


def lint_file(
    path: Path,
    code_roots: list[Path] | None = None,
    known_topic_ids: set[str] | None = None,
    strict_citations: bool = False,
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
            errors, more_warnings = lint_topic(path, fm, code_roots, strict_citations)
        else:
            errors, more_warnings = lint_record(path, fm, code_roots, strict_citations)
    return errors, warnings + more_warnings


# INC-0127: thirteen records (five incidents, two topics, six
# investigations) were committed with no opening `---` -- some also with
# no closing one. `parse_record_text` treats anything not starting with
# `---` as a plain note with empty frontmatter (a deliberate, legitimate
# shape for `sources/`, `inbox/`, and the store README): valid=True,
# no diagnostics, so lint_file's normal path never sees a problem, the
# append-only guard's `_is_topic_like` gate skips it (no `links`, no
# `type: topic`), and memidx never types it as its real kind. Nothing
# anywhere printed a line. Directory membership -- never raw-text
# canonicity guessing -- decides this: a file physically filed under
# `topics/`, `incidents/`, or `investigations/` is asserting its kind by
# LOCATION, and either fence problem there is an ERROR naming the file,
# independent of whether its content also happens to look canonical.
_FENCE_REQUIRED_DIRS = frozenset({"topics", "incidents", "investigations"})


def _fence_error(root: Path, path: Path) -> str | None:
    # CI gate finding (macOS): `walk_markdown` resolves `root` before
    # yielding paths under it (a symlinked --root walks the real tree), so
    # `path` always comes back resolved -- e.g. `/private/var/folders/...`
    # on macOS, where `/var` is itself a symlink to `/private/var`. Callers
    # of `_fence_error` (via `lint_root`) may still pass the UNRESOLVED
    # `root` a caller gave them, so `path.relative_to(root)` silently
    # raised ValueError on every macOS run -- caught, fell back to
    # `path.parts`, whose first component is never one of the required
    # directory names, so this check no-op'd everywhere on that platform.
    # Resolving `root` here, the same way `walk_markdown` already resolved
    # it, restores the match without changing `lint_root`'s own signature.
    try:
        rel_parts = path.relative_to(root.resolve()).parts
    except ValueError:
        rel_parts = path.parts
    if not rel_parts or rel_parts[0] not in _FENCE_REQUIRED_DIRS:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # Unreadable/non-UTF-8 is already reported by parse_record's own
        # diagnostic (lint_file's `if not result.valid` branch) -- never a
        # second error for the same file here.
        return None
    if not text.startswith("---"):
        return (
            f"{path}: missing opening frontmatter fence (the file's first line "
            f"must be exactly \"---\") -- a record under {rel_parts[0]}/ that is "
            "not fenced silently parses as an empty note: no schema check, no "
            "append-only protection, no correct type in the index (INC-0127)"
        )
    for line in text.splitlines()[1:]:
        if line.rstrip() == "---":
            return None
    return (
        f"{path}: frontmatter has no closing fence (a lone \"---\" line to end "
        f"the block) -- a record under {rel_parts[0]}/ with an unterminated "
        "block is not reliably parsed (INC-0127)"
    )


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


def _standing_cap_errors(root: Path) -> list[str]:
    """TOP-0132 L1/L2's hard, VISIBLE, store-wide cap: at most
    STANDING_CAP_LINKS pointers, or STANDING_CAP_BYTES bytes of the
    COMPLETE digest (`standing_digest_text`'s own rendering -- the header,
    every line, and the newlines joining them, UTF-8-encoded -- exactly
    what `memidx.py standing` prints and hashes), whichever is reached
    first, counted in the SAME order the projection uses
    (`standing_sort_key`: topic id, then link id). Corpus-wide, like
    `_duplicate_claim_errors`, so it walks the store once on its own rather
    than threading a cross-topic accumulator through `lint_topic`'s
    per-file pass.

    Fix-round MINOR (both reviewers): bytes, not characters of the lines
    alone -- a character count excluded the header entirely and
    undercounted any multi-byte (e.g. Cyrillic) text by roughly its
    encoded-width factor, letting a set through that `memidx.py standing`
    would then deliver at a size well past what was actually measured.

    Only a pointer that ALSO clears the per-link eligibility gate
    `lint_topic` checks (exists in its topic, active, CONSTRAINT_AUTHORITIES)
    is counted -- an ineligible pointer is already ITS OWN error from that
    check and would never actually reach the projection, so counting it
    here too would make the cap message name topics for content that
    `memidx.py standing` would never emit in the first place."""
    entries = []
    for f in sorted(walk_markdown(root)):
        result = parse_record(f)
        if not result.valid:
            continue
        fm = result.frontmatter
        if not _is_topic_frontmatter(fm):
            continue
        standing_ids = fm.get("standing")
        if not isinstance(standing_ids, list) or not standing_ids:
            continue
        topic_id = str(fm.get("id") or f.stem)
        links_by_id = {str(l.get("link")): l for l in (fm.get("links") or []) if l.get("link")}
        for lid in standing_ids:
            lid_str = str(lid)
            link = links_by_id.get(lid_str)
            if link is None or link.get("status") != "active":
                continue
            ruling = link.get("ruling") or {}
            auth = ruling.get("authority")
            if auth not in CONSTRAINT_AUTHORITIES:
                continue
            entries.append((topic_id, lid_str, auth, str(ruling.get("text") or "")))

    entries.sort(key=standing_sort_key)
    rendered = [standing_line(*e) for e in entries]
    total_links = len(rendered)
    total_bytes = len(standing_digest_text(rendered).encode("utf-8"))
    if total_links <= STANDING_CAP_LINKS and total_bytes <= STANDING_CAP_BYTES:
        return []

    over_topics = set()
    # Mirrors standing_digest_text's own concatenation: the header's bytes
    # first, then each line preceded by its own "\n" -- so cum_bytes after
    # entry i equals len(standing_digest_text(rendered[:i+1]).encode()).
    cum_bytes = len(_std_header_bytes())
    for i, (entry, line) in enumerate(zip(entries, rendered)):
        cum_bytes += len(("\n" + line).encode("utf-8"))
        if i >= STANDING_CAP_LINKS or cum_bytes > STANDING_CAP_BYTES:
            over_topics.add(entry[0])
    listing = ", ".join(sorted(over_topics))
    return [
        f"standing set exceeds the store-wide cap ({total_links} links, {total_bytes} bytes "
        f"of the complete digest; limit {STANDING_CAP_LINKS} links / {STANDING_CAP_BYTES} bytes): "
        f"topics over the line: {listing}"
    ]


def _std_header_bytes() -> bytes:
    """`standing_digest_text([])`'s own bytes -- the header alone, no
    lines -- used only to seed the cumulative byte count above so it
    matches `standing_digest_text`'s real concatenation exactly (never a
    second, independent header-length computation)."""
    return standing_digest_text([]).encode("utf-8")


def lint_root(
    root: Path, code_roots: list[Path] | None = None, strict_citations: bool = False
) -> tuple[list[str], list[str]]:
    """One pre-pass walk collects everything id-shaped (known ids for concept
    validation, explicit-id owners for the duplicate check, stem fallbacks for
    the collision warning) so the duplicate-id check costs no walk of its own
    (round-3 reviewer finding 8)."""
    known_topic_ids: set[str] = set()
    id_owners: dict[str, list[Path]] = {}
    stem_owners: dict[str, list[Path]] = {}
    fence_errors: list[str] = []
    topics: list = []  # (file, topic id, links, rejection rows) for the cross-topic declined warnings
    for f in sorted(walk_markdown(root)):
        # INC-0127: independent of parse validity -- a fence problem is
        # exactly what leaves parse_record reporting "valid, empty, no
        # diagnostics" in the first place, so it must not be gated behind
        # the `if not result.valid: continue` below.
        fence_error = _fence_error(root, f)
        if fence_error is not None:
            fence_errors.append(fence_error)
        result = parse_record(f)
        if not result.valid:
            continue
        fm = result.frontmatter
        is_topic = _is_topic_frontmatter(fm)
        if is_topic:
            tid = fm.get("id") or f.stem
            known_topic_ids.add(str(tid))
            if isinstance(fm.get("links"), list):
                topics.append((f, str(tid), fm["links"], rejection_rows(fm["links"])))
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

    all_errors: list[str] = list(fence_errors)
    all_warnings: list[str] = []
    for f in sorted(walk_markdown(root)):
        errors, warnings = lint_file(
            f, code_roots, known_topic_ids=known_topic_ids, strict_citations=strict_citations
        )
        all_errors.extend(errors)
        all_warnings.extend(warnings)
    # Declined-rulings rule, cross-topic half (DESIGN-declined-check section 1):
    # lint_topic judged each link against its own topic; here against every
    # OTHER topic's operative rejections. Warnings: history is grandfathered
    # (the error form is --against-ref, for links new relative to REF).
    for i, (f, tid, links, _rows) in enumerate(topics):
        others = [r for j, t in enumerate(topics) if j != i and t[1] != tid for r in t[3]]
        owners = {id(r): t[1] for j, t in enumerate(topics) if j != i and t[1] != tid for r in t[3]}
        for link in links:
            if not isinstance(link, dict):
                continue
            where = f"{f}:{link.get('link', '?')}"
            for row in readoption_matches(link, others, own_topic=False):
                all_warnings.append(
                    f"{readoption_message(where, owners[id(row)], row)} "
                    "[grandfathered: an error for new links under --against-ref]")
    all_errors.extend(_duplicate_claim_errors(root))
    all_errors.extend(_standing_cap_errors(root))
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


_MD_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_BACKTICK_RUN_RE = re.compile(r"`+")


def _blank_code_spans(line: str) -> str:
    """Return `line` with inline code spans blanked (same length, spaces).
    CommonMark rule, scanning backtick RUNS left to right: a run of length N
    opens a span that closes at the next run of exactly N; runs of other
    lengths are content. An opener with no matching closer is literal text
    and scanning resumes after it. Escapes and multi-line spans: not modelled."""
    runs = list(_BACKTICK_RUN_RE.finditer(line))
    out = list(line)
    i = 0
    while i < len(runs):
        n = len(runs[i].group(0))
        j = next((k for k in range(i + 1, len(runs))
                  if len(runs[k].group(0)) == n), None)
        if j is None:
            i += 1
            continue
        start, end = runs[i].start(), runs[j].end()
        out[start:end] = " " * (end - start)
        i = j + 1
    return "".join(out)


def _markdown_prose_lines(text: str):
    """Yield (lineno, line) for the prose of a Markdown file: lines inside a
    fenced block (``` or ~~~, any info string; closed by a fence of the same
    character at least as long, an unterminated one runs to EOF) are skipped,
    and inline code spans are blanked so a marker quoted as an example in a
    document is never read as a real one (a span opens and closes on a whole
    backtick run of the same length, as in CommonMark). Known limits: escaped
    backticks and spans crossing a line break are not modelled. Line numbers
    stay those of the file."""
    fence_char = ""
    fence_len = 0
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = _MD_FENCE_RE.match(line)
        if fence_char:
            if m and m.group(1)[0] == fence_char and len(m.group(1)) >= fence_len \
                    and not m.group(2).strip():
                fence_char, fence_len = "", 0
            continue
        if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            fence_char, fence_len = m.group(1)[0], len(m.group(1))
            continue
        yield lineno, _blank_code_spans(line)


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
                # A Markdown file documents markers in fenced blocks and
                # code spans as EXAMPLES (docs/SCHEMA.md); only its prose
                # is scanned (_markdown_prose_lines).
                if full.suffix.lower() in (".md", ".markdown"):
                    scan_lines = _markdown_prose_lines(text)
                else:
                    scan_lines = enumerate(text.splitlines(), start=1)
                for lineno, line in scan_lines:
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
    # CRLF-tolerant parse: the parser reads LF, the raw-span walker (below)
    # keeps the original bytes (TOP-0137 L12, gate round 2 item 1).
    return parse_record_raw(text, label)


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


# ---------------------------------------------------------------------------
# INC-0124: `_link_diff_errors` above compares PARSED link fields, so a file
# regenerated through a YAML dumper (same links, different bytes -- quoting
# style, key order, line wrapping) sails through untouched: every field is
# equal, so no branch above ever fires. TOP-0122 L1 rule 3 / the 0.3.0 plan
# item ("The append-only guard refuses a rewrite, not only a deletion")
# closes that: when a link's PARSED fields survive unchanged, its RAW BYTES
# -- the exact text span from its `  - link: Ln` line through the last line
# belonging to that list item -- must also survive unchanged, or the commit
# is refused as a reformat, not an append.
#
# The extraction below works on the file's LINES, never on a re-serialized
# form of the parsed dict (that would just reintroduce the same bug one
# layer down -- confirmed empirically: `yaml.safe_dump` renders sequences
# INDENTLESS and keys `sort_keys`-first, so a naive `  - link:`-anchored
# regex scan returns nothing on exactly the file this check exists to
# catch). Instead it locates each link's span STRUCTURALLY, by indentation,
# and identifies WHICH span belongs to which link id by ZIPPING the
# structural item order against the already-parsed `links` list in the same
# file order -- YAML never reorders a list, so the Nth structural item is
# the Nth parsed link, regardless of how that item's own lines are styled
# or ordered internally. `len(items) != len(parsed_links)` is the one
# consistency check available without re-deriving ids from a dumper of our
# own, and it is treated as "cannot safely say", not "unchanged": see
# `_link_raw_spans`'s docstring and check_append_only's fail-closed handling
# of a `None` return for what happens then.
# ---------------------------------------------------------------------------


def _leading_spaces(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _link_raw_spans(
    text: str, parsed_links: list, key: str = "links", ident=None
) -> dict[str, str] | None:
    """Maps link id -> the EXACT raw text (original line endings, no
    re-serialization) of that link's list-item span in `text`, a full
    record file's decoded content -- from its `- link: ...`-bearing line
    through the last line belonging to that item, i.e. every following
    line indented no shallower than the item marker itself. `parsed_links`
    is the SAME file's already-parsed `links` list (file order); spans are
    assigned to ids POSITIONALLY (see module comment above), never by
    regexing an id back out of the raw text -- immune to quoting, key
    order, and (YAML always preserves list order) reordering.

    A trailing space after `links:` (`.rstrip()`, not `.rstrip("\\n")`) and
    a `#`-comment line at any indent (attributed to whichever span is
    already open, never mistaken for the first content line, an item
    marker, or a boundary) are both tolerated -- PR #22 gate finding
    (MAJOR, both reviewers): either used to bail the WHOLE FILE to a NOTE,
    permanently turning the byte layer off for every link in it, on a
    perfectly valid YAML shape.

    `key` (default "links") names the top-level frontmatter key whose list
    is walked and `ident` (default: the item's `link` field) maps one
    parsed item to its identity string, or None when it has none -- the
    PR 2 `anchors:` freeze reuses this walker with key="anchors" and an
    identity over (ref, link, captured, captured_at) instead of a second
    copy of the structural scan. The default arguments keep the old
    behaviour byte-for-byte.

    Returns None -- "cannot safely say", never "unchanged" -- whenever the
    structure will not support that positional zip with confidence:
      * no top-level `links:` key on its own line (frontmatter missing,
        or `links` written as an inline flow list -- no real store file
        does this, but this function must not guess if one someday does)
      * the first real (non-blank, non-comment) line under it is not a
        list-item marker (`- `) at some consistent indent
      * the number of structurally-found items does not exactly match
        `len(parsed_links)` -- e.g. a REF-side duplicate id that
        `_recovered_old_links` already resolved to one entry still shows
        up as two structural items; rather than guess which structural
        item the surviving parsed entry corresponds to, this bails and
        the caller falls back to parsed-field comparison alone for that
        file (append-only is still enforced, just not the byte layer) --
        this is the one bail-out this fix round did not close; the caller
        turns it into a NOTE (never silent -- see check_append_only)
      * any parsed link's own `link` id is missing/unhashable, or two
        parsed links resolve to the same id string (would silently drop
        a span otherwise)

    Known limits (TOP-0129 L1 -- a guard is a claim until demonstrated,
    not a result): a YAML block scalar (`|`/`>`) whose continuation lines
    happen to dedent to the item marker's own column would be mis-split
    (no store file uses block scalars today -- confirmed by a corpus
    grep, not assumed); a blank line immediately after an item attaches
    to that item's OWN span, not to whatever follows it, so inserting a
    blank line inside recorded history (with nothing else touched) is
    correctly flagged as a reformat, not silently ignored.
    """
    if not text.startswith("---"):
        return None
    lines = text.splitlines(keepends=True)
    if not lines or not lines[0].startswith("---"):
        return None
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            end_idx = i
            break
    if end_idx is None:
        return None
    fm_lines = lines[1:end_idx]

    links_idx = None
    for i, line in enumerate(fm_lines):
        # Grok/Opus gate finding (MAJOR): `.rstrip("\n")` alone missed
        # `links: ` (a trailing space before the newline, a real shape a
        # hand edit or an editor's trim-on-save can leave) -- a full
        # `.rstrip()` catches trailing whitespace too, and still refuses a
        # flow-style `links: [...]` (that line does not become bare
        # "links:" after stripping), which stays a documented bail-out.
        if line.rstrip() == f"{key}:":
            links_idx = i
            break
    if links_idx is None:
        return None

    # A comment-only line (any indent) is never structure -- it belongs to
    # whichever span is already open (or, before the first item, to none
    # at all) and must never be mistaken for the first real content line,
    # an item marker, or a top-level-key boundary. Grok/Opus gate finding
    # (MAJOR): a `# recorded history`-style comment directly under
    # `links:`, before the first item, used to be read AS that first
    # line -- its own indent then "detected" a marker of `"# "`, which
    # never equals `"- "`, so the whole file bailed to a NOTE with the
    # byte layer silently off for every link in it.
    def _is_comment_only(line: str) -> bool:
        return line.lstrip().startswith("#")

    # Indent of the item marker itself: PyYAML's default dump renders
    # sequences indentless (marker at the SAME column as `links:`, column
    # 0); the store's own hand/agent-written style indents by 2. Detect it
    # from whatever the first real (non-blank, non-comment) line under
    # `links:` actually is, rather than assuming either convention.
    first = None
    for i in range(links_idx + 1, len(fm_lines)):
        line = fm_lines[i]
        if line.strip("\r\n") == "" or _is_comment_only(line):
            continue
        first = i
        break
    if first is None:
        return None
    indent = _leading_spaces(fm_lines[first])
    marker = fm_lines[first][indent:indent + 2]
    if marker != "- ":
        return None

    item_starts: list[int] = []
    block_end = len(fm_lines)
    for i in range(links_idx + 1, len(fm_lines)):
        line = fm_lines[i]
        if line.strip("\r\n") == "" or _is_comment_only(line):
            continue
        ls = _leading_spaces(line)
        if ls < indent:
            block_end = i
            break
        if ls == indent:
            if line[indent:indent + 2] == "- ":
                item_starts.append(i)
                continue
            block_end = i
            break
        # ls > indent: a nested field (or a nested list's own "- ") that
        # belongs to whichever item is currently open -- never a boundary.

    if not item_starts:
        return None

    spans_by_index = []
    for idx, start in enumerate(item_starts):
        stop = item_starts[idx + 1] if idx + 1 < len(item_starts) else block_end
        spans_by_index.append("".join(fm_lines[start:stop]))

    if len(spans_by_index) != len(parsed_links):
        return None

    if ident is None:
        def ident(link):
            lid = link.get("link")
            if lid is None or isinstance(lid, (dict, list)):
                return None
            return str(lid)

    spans: dict[str, str] = {}
    for link, span in zip(parsed_links, spans_by_index):
        if not isinstance(link, dict):
            return None
        lid_key = ident(link)
        if lid_key is None:
            return None
        if lid_key in spans:
            return None
        spans[lid_key] = span
    return spans


def _link_bytes_changed(old_span: str, new_span: str) -> bool:
    """The one comparison INC-0124 found missing -- isolated in its own
    function so a mutation test (TOP-0129 L1: prove the guard, don't just
    assert it) can disable exactly this and nothing else, then confirm the
    tests that depend on it fail for the right reason."""
    return old_span != new_span


def _read_added_blob(toplevel: Path, root: Path, relpath: str, path_in_root: str, staged: bool):
    """Parse the NEW side of an added file (index when staged, else the
    working tree); None when unreadable."""
    blob = _git_show(toplevel, f":{relpath}") if staged else _read_worktree(root / path_in_root)
    if blob is None:
        return None
    return _parse_git_blob(blob, path_in_root)


def _added_links_errors(full_path: Path, links: list, old_ids: set,
                        fm: dict | None = None, ctx: dict | None = None) -> list[str]:
    """Grandfathered-check errors for every link whose id is not in
    `old_ids` (the ids recorded at REF): the two kind/question checks and the
    declined-rulings rule (DESIGN-declined-check). The latter judges a new
    link against its own topic's rejections; when `old_ids` is empty (a
    brand-new topic, every link new) also against every OTHER topic in the
    store, read via `ctx` ({"root": store, "cache": {}, "staged": (toplevel,
    prefix) under --staged}): from the INDEX when staged, else the working tree."""
    errs: list[str] = []
    topic_id = str((fm or {}).get("id") or Path(str(full_path)).stem)
    own_rows = rejection_rows(links)
    other: list = []
    if not old_ids and ctx is not None:
        if "store" not in ctx["cache"]:
            ctx["cache"]["store"] = _store_rejections(ctx["root"], ctx.get("staged"))
        other = [(tid, r) for tid, r in ctx["cache"]["store"] if tid != topic_id]
    for l in links:
        lid = l.get("link")
        if lid is None or str(lid) in old_ids:
            continue
        errs.extend(_new_link_errors(f"{full_path}:{lid}", l))
        prefix = f"{full_path}:{lid}"
        for row in readoption_matches(l, own_rows, _link_number(lid)):
            errs.append(readoption_message(prefix, topic_id, row))
        for tid, row in other:
            if readoption_matches(l, [row], own_topic=False):
                errs.append(readoption_message(prefix, tid, row))
    return errs


def _decode_or_none(blob):
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _anchor_ident(row) -> str | None:
    """Identity of an anchor row for the freeze: (ref, link, captured,
    captured_at). None when any part is missing or not a scalar."""
    if not isinstance(row, dict):
        return None
    parts = [row.get(k) for k in ("ref", "link", "captured", "captured_at")]
    if any(p is None or isinstance(p, (dict, list)) for p in parts):
        return None
    return "\x00".join(str(p) for p in parts)


_ANCHOR_FREEZE_MESSAGES = {
    "no_ident": "recorded row lacks an identity component (ref, link, captured, captured_at) "
                "and cannot be verified; refusing to skip it (append-only)",
    "gone": "row removed or changed after being recorded (append-only; an anchor is a capture, "
            "never edited -- add a new link for a new baseline)",
    "changed": "row changed after being recorded (append-only; an anchor is a capture, never edited)",
    "no_span": "row's span could not be extracted on the new side; treated as changed "
               "(append-only; a recorded anchor must stay byte-verifiable)",
    "reformatted": "row reformatted, not appended (append-only; raw text changed after being "
                   "recorded while its parsed fields did not)",
}


def _frozen_rows_errors(old_rows: list, new_rows: list, ident, name, old_spans, new_spans,
                        messages: dict) -> list[str]:
    """The shared append-only loop for a frozen row list (`anchors:`,
    `standing_marks:`): every row recorded at REF must survive with the same
    parsed fields AND the same raw bytes (`old_spans`/`new_spans` are
    `_link_raw_spans` maps keyed by `ident(row)`, or None when unavailable).
    `name(row)` prefixes each message; `messages` supplies the wording."""
    errors: list[str] = []
    # multiplicity kept: a pair of identical rows at REF reduced to one is a removal
    new_by_ident: dict[str, list] = {}
    for row in new_rows:
        key = ident(row)
        if key is not None:
            new_by_ident.setdefault(key, []).append(row)
    taken: dict[str, int] = {}
    for row in old_rows:
        key = ident(row)
        label = name(row)
        if key is None:
            errors.append(f"{label}: {messages['no_ident']}")
            continue
        n = taken.get(key, 0)
        taken[key] = n + 1
        candidates = new_by_ident.get(key, [])
        new_row = candidates[n] if n < len(candidates) else None
        if new_row is None:
            errors.append(f"{label}: {messages['gone']}")
        elif new_row != row:
            errors.append(f"{label}: {messages['changed']}")
        elif old_spans is not None and key in old_spans:
            new_span = new_spans.get(key) if new_spans is not None else None
            if new_span is None:
                errors.append(f"{label}: {messages['no_span']}")
            elif _link_bytes_changed(old_spans[key], new_span):
                errors.append(f"{label}: {messages['reformatted']}")
    return errors


def _anchor_freeze_errors(full_path, old_text, old_rows: list, new_text, new_rows: list,
                          notes: list[str]) -> list[str]:
    """The append-only rule for `anchors:` rows: a row recorded at REF must
    survive with the same parsed fields AND the same raw bytes; new rows are
    free; a second row for an existing (ref, link) key is an error. Raw
    spans come from the same walker the links freeze uses (`_link_raw_spans`
    with key="anchors")."""
    errors: list[str] = []
    for ref, link in _anchor_duplicate_keys(new_rows):
        errors.append(
            f"{full_path}: anchors: more than one row for ({ref!r}, {link!r}) -- one baseline per "
            "key in this release; a new baseline is a new link"
        )
    if not old_rows:
        return errors
    old_spans = (
        _link_raw_spans(old_text, old_rows, key="anchors", ident=_anchor_ident)
        if old_text is not None else None
    )
    new_spans = (
        _link_raw_spans(new_text, new_rows, key="anchors", ident=_anchor_ident)
        if new_text is not None else None
    )
    if old_spans is None:
        notes.append(
            f"{full_path}: could not verify recorded anchors' raw text at REF "
            "(structural extraction inconclusive); anchors compared by parsed fields only"
        )
    errors.extend(_frozen_rows_errors(
        old_rows, new_rows, _anchor_ident,
        lambda row: f"{full_path}: anchor ({row.get('ref')!r}, {row.get('link')!r})",
        old_spans, new_spans, _ANCHOR_FREEZE_MESSAGES,
    ))
    return errors


# ---------------------------------------------------------------------------
# TOP-0137 L14: `standing:` / `current:` / link order under the guard. The rules
# (SCHEMA section 7, g1-g4) judge the REF blob against the staged one; nothing
# here reads the index or the code. Rows of `standing_marks:` are an audit,
# never delivered; this is the only place that makes them mandatory.
# ---------------------------------------------------------------------------


def _mark_span_ident(row) -> str | None:
    """`standing_mark_ident` as the string key `_link_raw_spans` maps by."""
    ident = standing_mark_ident(row)
    return "\x00".join(ident) if ident is not None else None


def _ref_mark_span_ident(row) -> str | None:
    """`ref_mark_ident` as the string key `_link_raw_spans` maps by."""
    ident = ref_mark_ident(row)
    return "\x00".join(ident) if ident is not None else None


# The two frozen audit blocks the guard judges with ONE implementation: the
# `standing:` pointer list (rows keyed by link id) and the `code_refs:` list
# (rows keyed by ref). `noun` words the messages, `members` reads the membership
# set the rows must account for, `rows` reads the block.
_STANDING_AUDIT = {
    "block": "standing_marks", "field": "standing", "id_field": "link", "noun": "standing mark",
    "span_ident": _mark_span_ident, "rows": standing_mark_rows,
    "members": lambda fm: set(standing_ids(fm)),
}
_REF_AUDIT = {
    "block": "ref_marks", "field": "code_refs", "id_field": "ref", "noun": "reference mark",
    "span_ident": _ref_mark_span_ident, "rows": ref_mark_rows, "members": code_ref_set,
}


def _mark_freeze_messages(audit: dict) -> dict:
    id_field, noun = audit["id_field"], audit["noun"]
    return {
        "no_ident": f"recorded row lacks an identity component ({id_field}, action, recorded_at) and "
                    "cannot be verified; refusing to skip it (append-only)",
        "gone": f"row removed after being recorded (append-only; a {noun} is never deleted)",
        "changed": f"row changed after being recorded (append-only; a {noun} is never edited)",
        "no_span": "row's span could not be extracted on the new side; treated as changed "
                   f"(append-only; a recorded {noun} must stay byte-verifiable)",
        "reformatted": "row reformatted, not appended (append-only; raw text changed after being "
                       "recorded while its parsed fields did not)",
    }


def _standing_marks_freeze_errors(full_path, old_text, old_rows: list, new_text, new_rows: list,
                                  audit: dict = _STANDING_AUDIT) -> list[str]:
    """g1: every row of the audit block (`standing_marks:` by default, `ref_marks:`
    with `audit=_REF_AUDIT`) recorded at REF survives with the same parsed
    fields and the same raw bytes, in the same relative order, and every new row
    sits ABOVE every old one (newest first). Unlike anchors, a span walker that
    cannot account for an existing OR a new block is an ERROR, not a note --
    otherwise the block's first commit would get parsed-field protection only."""
    errors: list[str] = []
    block, noun, span_ident = audit["block"], audit["noun"], audit["span_ident"]
    old_spans = (
        _link_raw_spans(old_text, old_rows, key=block, ident=span_ident)
        if old_rows and old_text is not None else None
    )
    new_spans = (
        _link_raw_spans(new_text, new_rows, key=block, ident=span_ident)
        if new_rows and new_text is not None else None
    )
    if old_rows and old_spans is None:
        errors.append(
            f"{full_path}: {block}: could not verify the recorded rows' raw text at REF "
            "(structural extraction inconclusive); refusing to treat them as unchanged"
        )
    if new_rows and new_spans is None:
        errors.append(
            f"{full_path}: {block}: could not extract the rows' raw text from the new "
            "block (structural extraction inconclusive); refusing to treat it as appended"
        )

    def name(row):
        return (f"{full_path}: {noun} ({row.get(audit['id_field'])}, {row.get('action')}, "
                f"{row.get('recorded_at')})")

    errors.extend(_frozen_rows_errors(old_rows, new_rows, span_ident, name,
                                      old_spans, new_spans, _mark_freeze_messages(audit)))
    old_keys = [k for k in (span_ident(r) for r in old_rows) if k is not None]
    old_set = set(old_keys)
    new_keys = [span_ident(r) for r in new_rows]
    survivors = [k for k in new_keys if k in old_set]
    if survivors != [k for k in old_keys if k in set(new_keys)]:
        errors.append(f"{full_path}: {block}: recorded rows reordered (append-only; "
                      "rows keep the order they were recorded in, newest first)")
    seen_old = False
    for k in new_keys:
        if k in old_set:
            seen_old = True
        elif seen_old:
            errors.append(f"{full_path}: {block}: marks not prepended (a new row must sit "
                          "above every recorded row, newest first)")
            break
    return errors


def _standing_pairing_errors(full_path, old_ids: set, new_ids: set, new_pairs: list,
                             audit: dict = _STANDING_AUDIT) -> list[str]:
    """g2: the multiset of `(id, action)` the membership change REQUIRES (of
    `standing:` by default, of `code_refs:` with `audit=_REF_AUDIT`) must equal
    the multiset of `(id, action)` of the rows added in this commit. Membership
    is a SET: reordering the same entries needs no row."""
    required = [(i, "add") for i in sorted(new_ids - old_ids)] + \
               [(i, "remove") for i in sorted(old_ids - new_ids)]
    field, block, noun = audit["field"], audit["block"], audit["noun"]
    errors: list[str] = []
    for pair in required:
        n = new_pairs.count(pair)
        if n == 0:
            errors.append(f"{full_path}: {field} changed for {pair[0]} ({pair[1]}) with no "
                          f"matching {block} row")
        elif n > 1:
            errors.append(f"{full_path}: duplicate {noun} for ({pair[0]}, {pair[1]}) in one commit")
    for pair in new_pairs:
        if pair not in required:
            errors.append(f"{full_path}: orphan {noun} ({pair[0]}, {pair[1]}): {field}: "
                          "did not change that way")
    return errors


def _link_order_errors(full_path, old_links: list, new_links: list) -> list[str]:
    """g3: the link ids recorded at REF keep their relative file order in the
    new blob (new ids go on top: a new id between old ones is refused by the older newest-first check) -- the chain head is the first
    active link in file order, so a reorder is a silent re-ruling."""
    old_order = []
    for l in old_links:
        lid = l.get("link")
        if lid is not None and str(lid) not in old_order:
            old_order.append(str(lid))
    pos = {lid: i for i, lid in enumerate(old_order)}
    kept = [str(l.get("link")) for l in new_links if l.get("link") is not None and str(l.get("link")) in pos]
    for x, y in zip(kept, kept[1:]):
        if pos[x] > pos[y]:
            return [f"{full_path}: links reordered ({x} before {y}; the chain head is "
                    "first-active-in-file-order)"]
    return []


def _head_id(links: list) -> str | None:
    head = newest_active_link(links)
    return str(head.get("link")) if head is not None and head.get("link") is not None else None


def _staged_head_errors(full_path, new_fm: dict, new_links: list) -> list[str]:
    """The half of g4 that needs no REF: a written `current:` must name the
    newest active link of the staged blob."""
    cur_new = None if new_fm.get("current") is None else str(new_fm["current"])
    head_new = _head_id(new_links)
    if cur_new is not None and cur_new != head_new:
        return [f"{full_path}: current: {cur_new!r} does not match the newest active link "
                f"of the staged file -- should be {head_new!r}"]
    return []


def _current_discipline_errors(full_path, old_fm: dict, old_links: list, new_fm: dict, new_links: list) -> list[str]:
    """g4: a written `current:` must name the newest active link of the STAGED
    blob (SCHEMA section 7, now judged on what is about to be committed), and
    it may only move when the newest active link moved -- or when it repairs a
    disagreement that already existed at REF."""
    cur_old = None if old_fm.get("current") is None else str(old_fm["current"])
    cur_new = None if new_fm.get("current") is None else str(new_fm["current"])
    head_old, head_new = _head_id(old_links), _head_id(new_links)
    errors: list[str] = _staged_head_errors(full_path, new_fm, new_links)
    if cur_new != cur_old:
        repair = cur_old != head_old and cur_new == head_new and head_new == head_old
        if head_new == head_old and not repair:
            errors.append(f"{full_path}: current changed {cur_old} -> {cur_new} without a change "
                          "of the newest active link")
    return errors


def _standing_guard_errors(full_path, old_fm: dict, old_text, new_fm: dict, new_text,
                           audit: dict = _STANDING_AUDIT) -> list[str]:
    """g1 + g2 for one topic present at both sides (the `standing:` audit by
    default, the `code_refs:` audit with `audit=_REF_AUDIT`)."""
    old_rows, new_rows = audit["rows"](old_fm), audit["rows"](new_fm)
    errors = _standing_marks_freeze_errors(full_path, old_text, old_rows, new_text, new_rows, audit)
    old_set = {k for k in (audit["span_ident"](r) for r in old_rows) if k is not None}
    new_pairs = [(str(r.get(audit["id_field"])), str(r.get("action"))) for r in new_rows
                 if audit["span_ident"](r) not in old_set]
    errors.extend(_standing_pairing_errors(
        full_path, audit["members"](old_fm), audit["members"](new_fm), new_pairs, audit))
    return errors


def _frozen_type_frontmatter(result) -> dict | None:
    """The frontmatter of a REF-side parse with its `type:` made comparable, or
    None when the recorded `type` cannot be trusted. A strict parse is trusted
    as it stands, valid or not. A lenient-fallback parse recovers `type` as raw
    text (`topic # comment` stays whole), so a trailing ` #...` comment is
    stripped and the value is trusted only when what remains is one of the
    engine's own record types (CANONICAL_TYPES, the set `_record_kind` trusts)."""
    fm = result.frontmatter
    if not isinstance(fm, dict):
        return None
    if not result.fallback:
        return fm
    raw = fm.get("type")
    if not isinstance(raw, str):
        return None
    bare = re.sub(r"\s+#.*$", "", raw).strip()
    return dict(fm, type=bare) if bare in CANONICAL_TYPES else None


def _type_change_errors(full_path, old_fm: dict, new_fm: dict) -> list[str]:
    """A topic's `type:` recorded at REF is frozen: it decides which index type
    the record carries, and `for-path` and the standing digest read only
    `type: topic`. A record with no `type` at REF recorded nothing to freeze
    (the caller also skips a REF side whose `type` only the lenient fallback
    recovered and that is not a known record type). A new side with no `type`
    (a missing key, a null, an empty or non-mapping frontmatter) has removed it:
    an error, `type removed`."""
    old_type = old_fm.get("type")
    if old_type is None:
        return []
    new_type = new_fm.get("type") if isinstance(new_fm, dict) else None
    if new_type is None:
        return [f"{full_path}: type removed (frozen {old_type!r}): type changed {old_type!r} -> None "
                "after being recorded (append-only; `type` decides what the record is delivered as)"]
    if str(new_type) == str(old_type):
        return []
    return [f"{full_path}: type changed {old_type!r} -> {new_type!r} after being recorded "
            "(append-only; `type` decides what the record is delivered as)"]


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

    ctx = {"root": root, "cache": {}, "staged": (toplevel, prefix) if staged else None}
    errors: list[str] = []
    notes: list[str] = []
    changed = 0
    for status, relpath in entries:
        path_in_root = relpath[len(prefix):] if prefix and relpath.startswith(prefix) else relpath
        full_path = root / path_in_root
        if status == "T":
            # Type change (symlink/gitlink <-> regular file): the path exists
            # on both sides, so it is judged exactly like a modification.
            status = "M"

        if status == "A":
            # Nothing existed at REF for this path -- no history to
            # protect; a brand-new topic (or a brand-new anything) is free
            # of append-only rules -- but every one of its links is NEW, so
            # the two grandfathered link checks apply to all of them.
            added = _read_added_blob(toplevel, root, relpath, path_in_root, staged)
            if added is not None and not _is_topic_like(added):
                # Re-gate (Codex BLOCK on 2cbff87): a non-topic-typed file
                # that still carries a recoverable `links:` list is topic
                # data to memidx; judge its links whatever its `type` says.
                errors.extend(_added_links_errors(
                    full_path, _recovered_old_links(added.frontmatter) or [], set(),
                    fm=added.frontmatter, ctx=ctx))
            if added is not None and _is_topic_like(added):
                if not added.valid:
                    # A new topic that fails parse/shape validation must not
                    # be skipped (Codex BLOCK on d117f5e): refuse it, and
                    # still judge whatever links could be recovered.
                    for field, message in added.diagnostics:
                        errors.append(f"{full_path}: {field}: {message} (added file)")
                    links = _recovered_old_links(added.frontmatter) or []
                else:
                    links = added.frontmatter.get("links") or []
                    # g2 for a brand-new topic: every id in `standing:` needs its `add`
                    # row, and rows with an empty `standing:` are orphans
                    added_rows = standing_mark_rows(added.frontmatter)
                    errors.extend(_standing_pairing_errors(
                        full_path, set(), set(standing_ids(added.frontmatter)),
                        [(str(r.get("link")), str(r.get("action"))) for r in added_rows]))
                    # ... and likewise every code_refs entry of a brand-new topic needs its
                    # `add` row in ref_marks (an existing topic's membership is grandfathered)
                    errors.extend(_standing_pairing_errors(
                        full_path, set(), code_ref_set(added.frontmatter),
                        [(str(r.get("ref")), str(r.get("action")))
                         for r in ref_mark_rows(added.frontmatter)], _REF_AUDIT))
                    # ... and the plain-mode rules on the STAGED blob (the hook lints only the
                    # worktree copy, INC-0127): rows, pointers, `current:` == head
                    errors.extend(_staged_standing_errors(full_path, added.frontmatter, links))
                    errors.extend(_staged_head_errors(full_path, added.frontmatter, links))
                errors.extend(_added_links_errors(
                    full_path, links, set(), fm=added.frontmatter, ctx=ctx))
            continue

        old_blob = _git_show(toplevel, f"{ref}:{relpath}")
        old_result = _parse_git_blob(old_blob, f"{relpath} (at {ref})")
        if not _is_topic_like(old_result):
            # Re-gate (Grok): a path that was not topic-like at REF (an
            # incident, an empty note) may be promoted/edited into one
            # carrying links; every recovered link whose id REF did not
            # already record is new and judged by the grandfathered checks.
            if status == "M":
                nb = _git_show(toplevel, f":{relpath}") if staged else _read_worktree(root / path_in_root)
                if nb is not None:
                    ref_ids = {
                        str(l.get("link"))
                        for l in (_recovered_old_links(old_result.frontmatter) or [])
                        if l.get("link") is not None
                    }
                    errors.extend(_added_links_errors(
                        full_path,
                        _recovered_old_links(_parse_git_blob(nb, path_in_root).frontmatter) or [],
                        ref_ids, fm=_parse_git_blob(nb, path_in_root).frontmatter, ctx=ctx))
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

        # `type` is frozen for a topic-like file that existed at REF; judged here, before
        # any branch below can `continue` past it (a repair, an invalid new side). A REF side
        # that parsed strictly keeps its `type` frozen whether or not the record was
        # schema-valid; a fallback-parsed one only when the recovered value, minus a trailing
        # ` #...` comment, is a known record type. The NEW side is compared exactly as parsed (no comment
        # strip: a quoted `"topic # x"` is a different value from `topic`).
        old_type_fm = _frozen_type_frontmatter(old_result)
        if old_type_fm is not None:
            errors.extend(_type_change_errors(full_path, old_type_fm, new_result.frontmatter))

        repair_note = None
        if not old_result.valid:
            old_links = _recovered_old_links(old_result.frontmatter)
            if old_links is None and (old_result.fallback or not old_result.frontmatter):
                # Shapes (ii)/(iii) of TOP-0137 L14 F1: the lenient fallback parse (or no
                # mapping at all) -- `standing` is not among the fallback fields, so the
                # old membership is unknown and cannot be paired.
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
                    notes.append(
                        f"{full_path}: standing membership at {ref} unrecoverable "
                        "(fallback parse); pairing not checked"
                    )
                    # Re-gate (Grok BLOCK on 90f3960): nothing to freeze,
                    # but every link in the repaired blob is NEW -- judge them.
                    # old_links is None here: REF recorded no recoverable
                    # link ids, so every link in the new blob is new.
                    errors.extend(_added_links_errors(
                        full_path, new_result.frontmatter.get("links") or [], set(),
                        fm=new_result.frontmatter, ctx=ctx))
                    # Anchor rows recorded at REF stay frozen on this branch too:
                    # a repair of the links is not a licence to rewrite a capture.
                    errors.extend(_anchor_freeze_errors(
                        full_path, _decode_or_none(old_blob), anchor_rows(old_result.frontmatter),
                        _decode_or_none(new_blob), anchor_rows(new_result.frontmatter), notes,
                    ))
                    # ... and so do the standing marks (g1), and every row and pointer of the
                    # new blob meets the plain-mode rules (pairing cannot run: see the note)
                    errors.extend(_standing_marks_freeze_errors(
                        full_path, _decode_or_none(old_blob), standing_mark_rows(old_result.frontmatter),
                        _decode_or_none(new_blob), standing_mark_rows(new_result.frontmatter),
                    ))
                    errors.extend(_standing_marks_freeze_errors(
                        full_path, _decode_or_none(old_blob), ref_mark_rows(old_result.frontmatter),
                        _decode_or_none(new_blob), ref_mark_rows(new_result.frontmatter), _REF_AUDIT,
                    ))
                    errors.extend(_staged_standing_errors(
                        full_path, new_result.frontmatter, new_result.frontmatter.get("links") or []))
                    errors.extend(_staged_head_errors(
                        full_path, new_result.frontmatter, new_result.frontmatter.get("links") or []))
                    continue
                # Both sides unparseable: still fail closed (test_j: malformed
                # -> malformed is still refused), reported from the OLD side's
                # diagnostics, same as before this fix.
                for field, message in old_result.diagnostics:
                    errors.append(f"{full_path}: {field}: {message} (at {ref})")
                continue
            if old_links is None:
                # Shape (i): a parsed mapping whose links are absent/empty/id-less. Its
                # `standing`, marks and `current` are on the dict: full g1-g4, no old links.
                old_links = []
                old_reasons = "; ".join(message for _field, message in old_result.diagnostics)
                repair_note = (
                    f"{full_path}: the blob at {ref} could not be safely parsed ({old_reasons}) and "
                    "recorded no usable links; its standing, standing_marks and current are still "
                    "compared against the current blob"
                )
            else:
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

        # INC-0124 / TOP-0122 L1 rule 3: locate each surviving link's raw
        # text span on both sides so a parsed-field-identical link can
        # still be caught if it was reformatted rather than left alone.
        # Decode is expected to succeed here -- both blobs already passed
        # through _parse_git_blob successfully to reach this point -- but
        # stays defensive rather than assuming that invariant forever.
        try:
            old_text = old_blob.decode("utf-8")
        except UnicodeDecodeError:
            old_text = None
        try:
            new_text = new_blob.decode("utf-8")
        except UnicodeDecodeError:
            new_text = None
        new_links_full = new_result.frontmatter.get("links") or []
        old_spans = _link_raw_spans(old_text, old_links) if old_text is not None else None
        new_spans = _link_raw_spans(new_text, new_links_full) if new_text is not None else None
        if old_links and old_spans is None:
            notes.append(
                f"{full_path}: could not verify recorded links' raw text at {ref} "
                "(structural extraction inconclusive -- see _link_raw_spans); "
                "append-only still enforced via parsed-field comparison only"
            )

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
            for l in new_links_full
            if l.get("link") is not None
        }

        # Grok/Opus gate finding (MINOR, both, pre-existing): newest-first
        # order was never enforced -- a new link inserted BETWEEN two
        # recorded ones (rather than above all of them) left every old
        # link's own bytes untouched, so nothing above ever saw it. This
        # catches only that shape (a new id following an old one in the
        # new file's own order); it does not police reordering AMONG old
        # ids themselves, which is a separate, unasked-for check.
        old_ids = {str(l.get("link")) for l in old_links if l.get("link") is not None}
        seen_old_id = False
        errors.extend(_added_links_errors(
            full_path, new_links_full, old_ids, fm=new_result.frontmatter, ctx=ctx))
        for l in new_links_full:
            nid = l.get("link")
            if nid is None:
                continue
            nid = str(nid)
            if nid in old_ids:
                seen_old_id = True
            elif seen_old_id:
                errors.append(
                    f"{full_path}:{nid}: link inserted out of order "
                    "(append-only; a new link must be added above every "
                    "previously recorded link, newest-first)"
                )

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
            elif old_spans is not None and lid in old_spans:
                # Parsed fields are IDENTICAL -- the branch above never
                # fired -- but the raw bytes may still differ (INC-0124:
                # a whole-file YAML regeneration preserves every parsed
                # field while changing quoting/wrapping/key order). A
                # missing new-side span (structural extraction failed on
                # the new blob, or this id somehow has none) is treated
                # the SAME as a proven byte change -- fail closed, per
                # the plan item's own framing ("a legitimate reformat of
                # history is refused too -- that is the point") -- rather
                # than silently trusting a blob our own extractor could
                # not account for. Opus gate finding (MINOR): this is a
                # DIFFERENT claim from an actual proven byte diff, so it
                # gets its own, honest message -- the old text ("raw text
                # changed") asserted something this branch never checked.
                new_span = new_spans.get(lid) if new_spans is not None else None
                if new_span is None:
                    errors.append(
                        f"{full_path}:{lid}: link's span could not be extracted on "
                        "the new side; treated as changed (append-only; a link "
                        "recorded at REF must stay byte-verifiable, and a structural "
                        "extraction failure on the new blob is judged the same as a "
                        "proven change rather than trusted)"
                    )
                elif _link_bytes_changed(old_spans[lid], new_span):
                    errors.append(
                        f"{full_path}:{lid}: link reformatted, not appended "
                        "(append-only; raw text changed after being recorded "
                        "while its parsed fields did not -- add a new link "
                        "instead of regenerating the file)"
                    )

        errors.extend(_link_order_errors(full_path, old_links, new_links_full))

        errors.extend(_anchor_freeze_errors(
            full_path, old_text, anchor_rows(old_result.frontmatter),
            new_text, anchor_rows(new_result.frontmatter), notes,
        ))

        # TOP-0137 L14: `standing:` changes need their marks (g1, g2) and `current:`
        # moves only with the newest active link (g4)
        errors.extend(_standing_guard_errors(
            full_path, old_result.frontmatter, old_text, new_result.frontmatter, new_text))
        # ... and `code_refs:` membership changes need their `ref_marks:` rows the same way
        errors.extend(_standing_guard_errors(
            full_path, old_result.frontmatter, old_text, new_result.frontmatter, new_text, _REF_AUDIT))
        errors.extend(_current_discipline_errors(
            full_path, old_result.frontmatter, old_links, new_result.frontmatter, new_links_full))
        errors.extend(_staged_standing_errors(full_path, new_result.frontmatter, new_links_full))

    return errors, changed, notes


def _extract_strict_citations_flag(argv: list[str]) -> tuple[list[str], bool]:
    """Pulls a bare `--strict-citations` flag out of argv before the
    remainder reaches parse_argv unchanged -- parse_argv's own 3-tuple
    contract stays exactly as it was (mirroring how _extract_against_ref_flags
    already preprocesses its own flags without touching parse_argv). A
    SEPARATE function, not folded into _extract_against_ref_flags, so the
    two preprocessing passes merge cleanly with any other branch touching
    that one. Meaningless without --code-root (the whole citation check is
    skipped then, silently, same as every other --code-root-gated check).
    Meaningless under --against-ref too (append-only mode never uses a code
    root either) -- but there main() REJECTS it explicitly (fix round,
    NIT: it used to be silently stripped and ignored, inconsistent with
    --code-root's own explicit rejection in that same mode for the
    identical reason), so this function itself never special-cases
    --against-ref -- it only extracts the flag's presence, leaving the
    accept/reject decision to main(), which already owns that branch."""
    rest: list[str] = []
    strict = False
    for a in argv:
        if a == "--strict-citations":
            strict = True
        else:
            rest.append(a)
    return rest, strict


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


USAGE = """usage: memlint.py ROOT [--code-root PATH ...] [--strict-citations]
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
                     (a warning if none is found yet). Also verifies cited
                     commits: a link's ruling.source/evidence, or a
                     standalone record's top-level source:/evidence:, that
                     names a commit (via "commit "/"merge "/"at "/" as " or
                     a backtick-quoted hash) or a path:line is checked
                     against the given code roots. A citation that
                     resolves to NOTHING (a hash cat-file cannot find; a
                     file not found at all) is a WARNING by default,
                     worded "unverifiable, not necessarily wrong" -- a
                     single code root cannot tell "fabricated" apart from
                     "cites a different repository." A SUBSTANTIATED
                     mismatch -- the hash resolves but a quoted subject
                     after it does not match that commit's actual subject;
                     a cited file exists but has fewer lines than cited --
                     is always a lint ERROR naming the record/link and the
                     mismatch, strict or not. Omit --code-root entirely
                     and all of these checks are skipped; every other rule
                     still runs.
  --strict-citations promotes an unresolved citation (the WARNING case
                     above) to a lint ERROR too -- for a store whose
                     records are known to cite only the wired repo, where
                     "not found" really does mean wrong. Has no effect
                     without --code-root (nothing to promote). Rejected
                     together with --against-ref, same as --code-root
                     (append-only mode never uses a code root either).
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
                     or renamed, is an error. New links (above every recorded
                     one), and changes to title/tags/the body text
                     outside a link, are free; a `code_refs` change needs its
                     `ref_marks:` rows, a recorded topic's `type` is frozen, a
                     `standing:` change needs its `standing_marks:` rows, link order is frozen, and `current`
                     moves only with the newest active link. A REF that never parsed is repaired
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
    # Grok/Opus gate finding (MAJOR): a NOTE ("could not verify byte-
    # identity here") used to print to stdout unconditionally and the
    # summary line carried no count of it -- on a clean (rc=0) run,
    # hooks/pre-commit-append-only.sh never echoes $OUT at all, so the one
    # signal that the byte layer was inconclusive for a file reached
    # nobody. Notes now go to stderr (never treated as an error -- exit
    # stays 0 for a NOTE, per both reviewers) and the summary line names
    # how many fired, so a caller that greps the summary (the hook does)
    # can see it even without capturing stderr separately.
    for n in notes:
        print(f"NOTE: {n}", file=sys.stderr)
    for e in errors:
        print(f"ERROR: {e}")
    print(f"memlint: append-only against {ref}: changed={changed} errors={len(errors)} notes={len(notes)}")
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
    rest, strict_citations = _extract_strict_citations_flag(rest)
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
        if strict_citations:
            # Fix round (NIT): --strict-citations used to be silently
            # stripped and ignored here, inconsistent with --code-root's
            # own explicit rejection one branch up for exactly the same
            # reason -- append-only mode never uses a code root, so a
            # citation-strictness flag that only means anything WITH a
            # code root has nothing to attach to either. Same message
            # shape as the --code-root rejection, so a reader sees the
            # two flags are refused the same way, not two different ways.
            print(
                "--strict-citations is rejected together with --against-ref "
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
    try:
        errors, warnings = lint_root(root, code_roots, strict_citations)
    except Exception as exc:  # never a bare traceback -- same contract
        # _run_append_only already holds (spec test (j)): a real ERROR is
        # a printed diagnostic and exit 1, never an uncaught exception.
        # Re-gate finding (MAJOR): this does not by itself close the
        # `--against-ref` mode's own hole (a broken `memlint import` --
        # PyYAML missing from the venv -- fails before `main` is ever
        # reached, so no try/except inside it can catch that), but it
        # keeps THIS mode's own rc contract honest for every failure that
        # happens once execution is inside `main` -- and it is exactly
        # this exit-2 shape that hooks/pre-commit-append-only.sh's
        # summary-line marker check (rather than trusting RC alone) is
        # built to tell apart from a real finding either way.
        print(f"memlint: unexpected failure during schema lint: {exc}", file=sys.stderr)
        return 2
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
