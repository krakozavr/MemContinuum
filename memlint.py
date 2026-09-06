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

from memidx import (
    AUTHORITIES,
    EDGE_RELS,
    INVARIANT_KINDS,
    KINDS,
    STATUSES,
    ParseResult,
    code_ref_is_named,
    fragment_declaration_status,
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
        is_topic = bool(fm.get("links")) or fm.get("type") == "topic"
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
    decides it: `links` present, or an explicit `type: topic`."""
    if not result.valid:
        return True
    fm = result.frontmatter
    return bool(fm.get("links")) or fm.get("type") == "topic"


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
                errors.append(
                    f"{full_path}:{lid}: link changed after being recorded "
                    "(append-only; add a new link instead)"
                )

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
                     error (one reference must name one file). Omit it
                     entirely and those checks are skipped; every other rule
                     still runs.
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


def _extract_against_ref_flags(argv: list[str]) -> tuple[list[str], str | None, bool]:
    """Pulls --against-ref REF and --staged out of argv before the
    remainder reaches parse_argv unchanged -- parse_argv's own 3-tuple
    contract (and the tests that call it directly) stays exactly as it
    was; this is a preprocessing pass, not a parse_argv change."""
    rest: list[str] = []
    against_ref = None
    staged = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--against-ref":
            i += 1
            if i < len(argv):
                against_ref = argv[i]
        elif a == "--staged":
            staged = True
        else:
            rest.append(a)
        i += 1
    return rest, against_ref, staged


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
    rest, against_ref, staged = _extract_against_ref_flags(argv)
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
