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
id: TOP-0042
title: Hidden files in the processed count
area: processing/status
project: notecatcher
current: L4                 # newest active link id — DERIVED by the linter, never hand-edited
code_refs:                  # the decision→code link
  - src/core/scan/scan_plan.py#hidden_count
  - src/app/summary/summary_card.py#appendix
tags: []
```

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
| `agent-inference` | an agent's reading of the situation | CONTEXT |
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
> (repro, commit, failing test), at any authority. A HOLD forces **live
> revalidation against current source** — a confirmed current-source safety
> violation may block on its own authority; green tests are not dispositive
> unless they actually discriminate against the failure.
>
> **CONTEXT** — informs only. `agent-inference` may never by itself override
> a fix that code and tests already accept.

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
TOP-0042 Hidden files in the processed count — current: L4 (active, agent-inference)
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
- `status: superseded` without `superseded_by` → error
- `reverses:` without `reason_for_change` → error
- `current` not equal to the newest link with `status: active` → error (names the correct value)
- a topic in area `processing/*` or `deletion/*` with no `code_refs` → warning
- any `status` / `authority` / `kind` value outside the five/five/five enumerated above → error

**Deliberately not implemented:** "a link edited after being recorded (hash
mismatch vs git) → reject". That check needs the canonical records to live in
a git repo with an append-only enforcement process around it, which is a
property of how a *store* is operated, not of this schema or its linter.
Anyone wiring a canonical append-only store on top of this should add that
check at the point where commits are made.

---

## 8. Extensions — typed edges, assumptions, invariants, concepts

Everything below extends the shapes above without changing anything already
described: plain topics/records work exactly as before.

### 8.1 Links gain typed cross-references (the chain becomes a DAG)

```yaml
  edges:                      # optional, any link; targets are topic ids, link ids, incident/investigation ids
    - {rel: supersedes,    to: TOP-0042/L2}
    - {rel: preserves,     to: TOP-0042/L2#constraint-a}   # partial supersession: what survives
    - {rel: abandons,      to: assumption:A1}              # what stops being assumed
    - {rel: challenged_by, to: INC-0900}
    - {rel: led_to,        to: TOP-0091/L1}
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

### 8.4 Concept records (`type: concept`) — the code graph's authored layer

```yaml
type: concept
id: CON-007
title: Tag Suggestion
owner_boundary: "src/core/tags — everything that decides which tags to suggest for a note"
implemented_by: [src/core/tags/suggest.py#rank_candidates]
tested_by: [tests/test_tag_suggestion.py]
governed_by: [TOP-0031, TOP-0042]
involved_in: [INC-0004]
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
