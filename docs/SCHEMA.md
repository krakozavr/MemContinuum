# Record schema — topics, links, per-field authority

This is the schema `memidx.py` indexes and `memlint.py` validates. It is
project-agnostic: every example below describes a fictional note-taking app
("Notecatcher") purely to make the shape concrete. Nothing here is tied to
any particular project's real history.

## 1. The unit is the TOPIC; rulings are LINKS under it

```
topics/<area>/<topic-slug>.md          one file per topic = one chain
```

A topic file is **append-only**. Its body is the ordered list of links, newest
first. Editing a past link is forbidden; a change of mind is a new link. "The
current decision" is the newest link with `status: active` — never a separate
field that can drift out of sync with the chain.

## 2. Topic frontmatter

```yaml
type: topic
id: TOP-42
title: Hidden files in the processed count
area: processing/status
project: notecatcher
current: L4                 # newest active link id — hand-set; memlint errors if it does not match the newest active link
code_refs:                  # the decision→code link — three forms, freely mixed
  - src/core/scan/                    # a repo-relative PATH PREFIX — matches every file under it
  - src/app/summary/*.py              # an fnmatch GLOB
  - src/core/scan/scan_plan.py#hidden_count   # PATH#SYMBOL — a qualified symbol name as the chunkers report it
tags: []
```

A prefix and a glob keep serving retrieval exactly as before — `for-path`/`unmapped` match either
against a file path, unchanged (§8.4). Only `path#symbol` names an actual symbol, so only
`path#symbol` refs take part in marker verification (§8.3): a marker can never be checked against
a ref that names no symbol.

## 3. Link (one ruling) — fields, and authority PER FIELD

```yaml
- link: L4
  date: 2024-04-15
  status: active            # active | provisional | superseded | historical | declined
  kind: adopted              # adopted | declined | reversed | amended | restored
  reverses: L3                # pointer, when kind is reversed/restored/amended
  reason_for_change: new-evidence  # new-evidence | changed-context | changed-mind (required if reverses:)
  ruling:
    text: "…the decision, in one sentence…"
    authority: owner-verbatim        # owner-verbatim | owner-ratified | agent-inference | reviewer-finding | code-derived
    source: "session 2024-04-15 / BACKLOG.md#12 / commit a1b2c3d"
  rationale:
    text: "why — as best understood"
    authority: agent-inference       # usually inference, even when the ruling itself is verbatim
  alternatives:
    - {option: "…", rejected_because: "…", authority: agent-inference}
  evidence: [commit a1b2c3d, tests/test_hidden_count.py, "render notes/summary-card.png"]
  revisit_if:
    - "the count and the appendix stop being shown together"
    - "a user reports the appendix as noise"
  recorded_by: agent
  recorded_at: 2024-04-15
```

### Authority values — exactly five, and what each may do

| authority | meaning | may be cited as |
|---|---|---|
| `owner-verbatim` | the project owner's own words, quoted; `source` points at where | **CONSTRAINT** (with status active) |
| `owner-ratified` | agent-drafted text the owner was shown and affirmed; `text` = what they saw; `source` = the sitting | **CONSTRAINT**; produced only by the promotion procedure (§5) |
| `agent-inference` | an agent's reading of the situation | CONTEXT; with an invariant and validated evidence it is a HOLD (reported; enforced only under `--strict-holds`) |
| `reviewer-finding` | an independent reviewer's finding, with its evidence | HOLD if evidence-bearing, else CONTEXT |
| `code-derived` | true because code/tests say so; WHY is unknown | HOLD if it is an incident with a repro, else CONTEXT |

There is no `paraphrase`. `owner-ratified` exists because banning ratification
would mean an inference never gets promoted to a constraint at all; it stays
distinct from `owner-verbatim` so it can never be confused with the owner's
own words.

### Status values — exactly five

| status | means | rule |
|---|---|---|
| `active` | currently applicable (not "binding" — an incident can be active without binding anything) | — |
| `provisional` | recorded, not yet owner-confirmed | CONTEXT/HOLD only |
| `superseded` | replaced by a NAMED later link | `superseded_by` required; empty = linter error |
| `historical` | no longer applicable, nothing replaced it | no successor |
| `declined` | considered and not adopted | reopening = a new link with `reverses:` |

Partial supersession (a rule replaced in one scope only, e.g. one platform)
is handled by **splitting the link into scoped claims before promotion**
(see the `applies_to`/`preserves` edges in §8), never by a sixth status.

## 4. The citation rule — three tiers

> **CONSTRAINT** — may refuse, block, or reverse work: a link whose
> `ruling.authority` is `owner-verbatim` or `owner-ratified` AND
> `status: active`. Code and tests are a separate constraint channel and need
> no record here.
>
> **HOLD** — must pause: an incident/constraint with reproducible evidence
> (repro, commit, failing test), at `reviewer-finding`, `code-derived`, or
> `agent-inference` authority — never `owner-verbatim`/`owner-ratified`,
> which are already CONSTRAINT, and never a HOLD-eligible authority with no
> validated evidence, which stays CONTEXT. A HOLD forces **live
> revalidation against current source** — a confirmed current-source safety
> violation may block on its own authority; green tests are not dispositive
> unless they actually discriminate against the failure. `memidx.py drift`
> makes this executable against a checkable `invariant:` (§8.3): a
> CONSTRAINT-tier violation always fails the run; a HOLD violation is
> reported but only fails the run under `--strict-holds`; a `provisional`
> link's invariant is never enforced, only reported for revalidation.
>
> **CONTEXT** — informs only. `agent-inference` may never by itself override
> a fix that code and tests already accept.

**Conflict resolution.** Two `status: active` links can genuinely conflict — the schema allows
several active rulings at once (one topic's own chain, or across topics), it does not guarantee
they agree. When they do, the tier above decides: the higher-tier link prevails, and the reader
names both links and says so, rather than silently picking one. Equal tier does not resolve
itself: two conflicting rulings at `agent-inference` (or any other equal, non-owner tier) are
resolved by the orchestrator, who writes a new link reversing one of them (`kind: reversed`,
`reverses: <the losing link>`, per §7's rule that its target must no longer be active); two
conflicting `owner-verbatim`/`owner-ratified` links go back to the owner, and the owner's answer
is recorded as a new `owner-verbatim` link (`TOP-xxxx Ln`), never inferred on their behalf.

## 5. Promotion — how an inference becomes a ruling

1. The record exists as `agent-inference` / `provisional`.
2. The owner is shown the **exact text**; they affirm *that text* (or supply
   their own).
3. A **new link** is appended: `authority: owner-ratified` (or
   `owner-verbatim` if it is their own words), `text` = what they saw,
   `source` = the sitting + date. The old link gets `promoted_by: L<n>`.
4. **Bulk approval never promotes.** Keep/drop/merge passes change no
   authority. A promotion sitting shows a small number of statements at a
   time; "yes to all" is not a valid promotion.
5. Only the owner promotes. Reviewers propose in an inbox; an agent writes;
   nobody else.

## 6. The compressed chain view (what `memidx.py chain` shows)

One line per link, newest first:
```
TOP-42 Hidden files in the processed count — current: L4 (active, agent-inference)
  L4 2024-04-15 restored  ← reverses L3 (new-evidence: the earlier deletion was blind to the rationale)
  L3 2024-04-01 amended   half of the pair was deleted (reviewer-finding)
  L2 2024-03-20 adopted   "…the ruling text…" (owner-verbatim) because [rationale, inference]
  L1 2024-03-02 declined  because it inflates the number the user sees (agent-inference)
```
Ten links = ten lines. The argument stays in `evidence`, pointed to, not
copied inline.

## 7. Linter rules

`memlint.py` enforces:

- `owner-verbatim` or `owner-ratified` without `ruling.text` and `source` → error
- an `owner-verbatim` `ruling.text` ending in `?` (after trimming quotes/whitespace) → error, a
  question is not a ruling (`owner-ratified` is the orchestrator's own paraphrase, not covered)
- `status: superseded` without `superseded_by` → error
- `reverses:` without `reason_for_change` → error
- a `kind: reversed` link whose `reverses:` target's `status` is still `active` or `provisional` →
  error, naming the target and its status (`kind: amended` leaves its predecessor active on
  purpose — not covered)
- `current` not equal to the newest link with `status: active` → error (names the correct value)
- a topic in area `processing/*` or `deletion/*` with no `code_refs` → warning
- any `status` / `authority` / `kind` value outside the five/five/five enumerated above → error
- frontmatter that does not parse (unreadable, not UTF-8, unterminated, malformed YAML on a
  canonical record), or a typed field in the wrong shape (`links` not a list of mappings, a link
  missing its `link` id, `ruling`/`rationale`/`invariant` not a mapping, a list field carrying a
  non-scalar) → error naming the file/field; the same on a note (no schema id/links/type) → warning

**A link edited after being recorded is caught too**, in a second, independent
check: `memlint.py --against-ref REF [--staged] ROOT` compares every topic
file's links now against what they were at `REF`. A link present at `REF`
has its BODY frozen — `ruling`, `rationale`, `alternatives`, `evidence`,
`revisit_if`, `edges`, `assumptions`, `invariant`, `date`, `kind`, `reverses`,
`reason_for_change`, `recorded_by`, `recorded_at` may never change; any diff
there is an error naming the field. Exactly three fields are lifecycle
fields, allowed to move **forward only, once**: `status` may move from
`active` or `provisional` to `superseded`, `historical`, or `declined` —
never back to `active`/`provisional`, never between the three terminal
values (so a provisional record is *promoted* by a new link, per §5, never
by editing this field to `active`) — `superseded_by` may be *added* in that
same move (never changed afterwards, never present unless `status` is
`superseded`) — and `promoted_by` may be *added* once, with no such status
coupling: §5 step 3's promotion procedure appends a new link and adds
`promoted_by: L<n>` to the OLD link it promotes, whatever that old link's
own status; it is immutable once set, exactly like `superseded_by`. A
lifecycle move must be the only change on the link; bundled with any body
edit, both get their own error. A link removed, or a topic file deleted or
renamed, is an error naming the path. New links, and changes to `current`,
`title`, `tags`, `code_refs`, or the body text, are free. A store's own git
`pre-commit` hook (`hooks/pre-commit-append-only.sh`, wired by
`scripts/repo-init.sh` the same way `post-commit-reindex.sh` is) runs this on
every commit and blocks the ones that fail it; the same check can run again in
CI against a wider range, for a guarantee `--no-verify` cannot bypass. See
`hooks/install-hooks.md` for how it is wired and `docs/INTERNALS.md`'s memlint
section for the full rule table.

---

## 8. Extensions — typed edges, assumptions, invariants, concepts

Everything below extends the shapes above without changing anything already
described: plain topics/records work exactly as before.

### 8.1 Links gain typed cross-references (the chain becomes a DAG)

```yaml
  edges:                      # optional, any link; targets are topic ids, link ids, incident/investigation ids
    - {rel: supersedes,    to: TOP-42/L2}
    - {rel: preserves,     to: TOP-42/L2#constraint-a}   # partial supersession: what survives
    - {rel: abandons,      to: assumption:A1}              # what stops being assumed
    - {rel: challenged_by, to: INC-900}
    - {rel: led_to,        to: TOP-91/L1}
    - {rel: applies_to,    to: subsystem:import}           # scope of a partial replacement
```
A claim superseded in one scope only is SPLIT into scoped claims; `applies_to`
+ `preserves` record the split. `reverses` and `reason_for_change` remain
mandatory for reversals — edges add detail, never replace them.
`memlint.py` rejects any edge whose `rel` is not one of these seven.

### 8.2 Assumptions become explicit (the positive form of `revisit_if`)

```yaml
  assumptions:
    - {id: A1, text: "notes are read far more often than they are re-indexed", status: holds}
    - {id: A2, text: "no user has more than 100k notes",                       status: broken, since: 2024-05-01}
  revisit_if: [...]           # unchanged — the negative form
```
"What must change for the declined option to become reasonable again?" is
exactly the assumptions its rejection rested on. A later link that breaks an
assumption cites it with an `abandons` edge.

### 8.3 Constraints can carry a checkable invariant (decision/code drift gate)

```yaml
  invariant:
    kind: no-bypass | single-definition | must-call | pattern-absent
    pattern: "unlink\\(|os\\.remove"        # what the check greps for
    allowed: ["src/core/storage/purge.py"]  # canonical sites
    checked_by: "tests/test_purge_gate.py"  # when a guard test exists
```
A CONSTRAINT-tier link with an `invariant` is a tripwire, not prose:
`memidx.py drift` runs every invariant against the code tree and reports
"implementation has drifted from active decision `<id>`".

**A CONSTRAINT or HOLD link may also be mirrored at its bound symbol**, a comment carrying the
decision it implements:

```python
# decision: TOP-42 L4
def hidden_count(entries):
    ...
```

Syntax is language-agnostic: any comment LINE containing `decision: TOP-xxxx Ln` — on the
symbol's own definition line, or within the three lines immediately above it — is a marker,
whatever the language's comment leader (`#`, `//`, `--`, …); the check is a plain text match, not a
parse of the comment itself. The symbol's definition line is found through the chunker registry
(`chunkers.get_chunker(lang).chunk_file`), so the same rule serves every wired language; a file
whose language has no chunker, or whose backend cannot run here, is skipped with a warning rather
than treated as carrying no marker. Markers verify at functions, methods, and computed
vars/properties — whatever the chunker itself reports a definition line for; a container type
(a class, struct, enum, …) has no such line of its own and is uncheckable.

`memlint.py --code-root DIR` checks the pair both ways. Only a `path#symbol` code_refs entry
takes part — a glob or a bare path names no symbol, so a marker under one is an error, not a
skip. Marker → store: every marker under a code root must point at a topic and link that exist,
that link must be `active` and CONSTRAINT or HOLD, and that topic's `code_refs` must name the
marked file with the matching `path#symbol` (a prefix or glob that merely happens to match the
same FILE does not count — that is the error `path#symbol` exists to prevent) — else an error
naming the file, line, and reason. Store → code: every active CONSTRAINT/HOLD link whose topic
has a `path#symbol` ref must find the marker at that symbol — else a warning (existing stores
carry none yet). A symbol the chunker reports no declaration for splits into two cases: the
symbol's own NAME genuinely absent from the file's text is a dangling ref, an error instead of a
missing-marker warning; the name IS present but the chunker simply
never emits a chunk for it (a Swift protocol requirement — signature only, no body — or a
container type the chunker layer does not report a declaration line for) is a warning that the
symbol cannot be verified by the chunker, and the marker check is skipped for it rather than
either erroring or asserting a false absence.

### 8.4 Concept records (`type: concept`) — the code graph's authored layer

```yaml
type: concept
id: CON-007
title: Tag Suggestion
owner_boundary: "src/core/tags — everything that decides which tags to suggest for a note"
implemented_by: [src/core/tags/suggest.py#rank_candidates]
tested_by: [tests/test_tag_suggestion.py]
governed_by: [TOP-31, TOP-42]
involved_in: [INC-4]
```
Answers "which existing entity already owns X, and where are its
boundaries?" — a question symbol search alone cannot answer. `for-path`
returns the concept(s) a file belongs to, along with their topic chains.
`memlint.py --code-root DIR` (repeatable, one project can have several code
roots) errors when an `implemented_by`/`tested_by` path no longer exists
under any root given, or exists under more than one, and warns when a
concept has no `tested_by` at all (promotion needs at least one).

### 8.5 Reading direction from code: "why is this code strange?"

`memidx.py why <symbol-or-path>` resolves a path (or symbol, via
`--code-root`) to its concept(s), then prints each concept's `governed_by`
topic chains, newest first — **including `declined` links**, so a reviewer
sees the rejected alternative before "cleaning up" code that implements it.

## 9. Incidents and investigations — field shape

`incidents/<slug>.md` and `investigations/<slug>.md` are standalone records —
`is_topic` is false (no `links:`, `type` is not `topic`) — so they skip the
per-field authority machinery of §3 entirely: one flat frontmatter block, one
claim, closed once written. This section names the fields real records
actually carry, not a new required shape:

```yaml
type: incident                    # or: investigation
id: INC-9                         # referenced from a link's edges{} (§8.1) and a concept's involved_in (§8.4)
title: Appendix count double-counted hidden files after a rename
area: processing
date: '2024-05-02'                # or omitted/null when genuinely unknown
status: active                    # active | provisional | superseded | historical | declined
authority: agent-inference        # owner-verbatim | owner-ratified | agent-inference | reviewer-finding | code-derived
source: session 2024-05-02 debugging log
evidence:
  - "test_hidden_count.py failure before the fix, commit a1b2c3d"
code_refs:
  - src/core/scan/scan_plan.py
```

- `status` and `authority` are the only two fields `memlint.py`'s standalone-
  record check enum-validates (the same five/five values §3 defines for a
  link) — an unrecognized value on either is an error. `type`, `id`, `title`,
  `area`, `date`, `source` are free-form: nothing in the linter enum-checks
  them. `id`, when present, still participates in the project-wide duplicate-
  id collision warning every structured record gets (§7), and is what an
  `edges{}`/`involved_in` reference (§8.1, §8.4) actually points at.
- `evidence` here is prose only. A topic *link's* `evidence` (§3) is parsed
  into the `links` table and read by `memidx.py drift`'s HOLD classification
  (§4); a standalone record's own top-level `evidence` is never parsed into
  that table and never consulted by `drift` — it supports the claim for a
  human reader, nothing more.
- `code_refs` is stored the same way a topic's is, but only a *topic's*
  `code_refs` are ever matched against a file path: `for-path`/`unmapped`
  query `records WHERE type='topic'` alone, so an incident's `code_refs` is
  descriptive context, not something `for-path` will ever surface for an
  edited file. Unlike a topic's `code_refs` (§2, §7), memlint does not reject
  an empty or fragment-only entry here — that check runs only for topics.
- `date: null` is real (seen in the wild when an incident's timing was never
  pinned down) — treat it as optional, not required-but-sometimes-empty.
