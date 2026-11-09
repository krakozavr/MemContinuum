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
standing: [L4]               # optional — this topic's OWN link ids that are true of the whole project, always (§7)
standing_marks:              # optional — the audit of changes to `standing:` (see "Standing marks" below)
  - {link: L4, action: add, ...}
ref_marks:                   # optional — the audit of changes to `code_refs:` (see "Reference marks" below)
  - {ref: src/core/scan/, action: add, ...}
```

A prefix and a glob keep serving retrieval exactly as before — `for-path`/`unmapped` match either
against a file path, unchanged (§8.4). Only `path#symbol` names an actual symbol, so in a file
WITH a chunker only `path#symbol` refs take part in marker verification (§8.3): a marker there can
never be checked against a ref that names no symbol. A file whose language has no chunker at all
has no symbol to check against in the first place, so any code_refs form naming the file takes
part instead (§8.3).

`standing:` is never derived and never a per-link flag — a topic names its own standing links
explicitly, on the topic, the same way `code_refs` names its own paths. Nothing here decides
membership by absence (a topic with no `code_refs` is not thereby standing, and one with `code_refs`
is not thereby excluded) — a topic is standing only for the links it lists. `memlint.py` requires
every pointed link to exist in this topic, be `status: active`, and carry `ruling.authority`
`owner-verbatim` or `owner-ratified` (§7) — a pointer to a superseded link is an error, on purpose,
so the supersession and the pointer update land in the same commit. `memidx.py standing` projects
the whole store's standing links, ordered and capped (§7); `sessionstart-remind.sh` delivers that
projection at the start of a session, unprompted, before any edit.

### Standing marks — the audit of `standing:` membership changes

Every change to `standing:` leaves a dated, attributed row in `standing_marks:`, a block list written
immediately after `standing:` (which by convention precedes `anchors:` and `links:`), newest row first. A row is **not a
ruling**, the newest row is **not `current`**, and a `remove` row does **not** mean the link is
inactive: it records only who changed the list, when, and on whose word. Rows are never delivered —
not in the standing digest, not in a chain, a context pack or a pre-edit line — and never indexed;
they are the trace a reader finds with `cat`.

```yaml
standing: [L1, L4]
standing_marks:
  - link: L4
    action: add                       # add | remove
    date: 2026-11-03                  # the date part of recorded_at as written (its own offset)
    recorded_at: "2026-11-03T09:12:41-05:00"   # ISO-8601 with offset, seconds; quoted
    recorded_by: claude-fable-5-1
    authority: owner-ratified         # owner-verbatim | owner-ratified ONLY
    source: "owner message 2026-11-03 09:10 EST, session_..."
    why: "one sentence, no CR/LF"
```

A row is identified by `(link, action, recorded_at)`; repeated transitions (add, remove, add again)
are legitimate, each its own row. `authority` is limited to the two owner authorities because a
standing link reaches every session start of every project. `memidx.py mark-standing` is the one
writer (`memidx.py mark-standing TOP-xxxx Ln --add|--remove --session ID (--yes-message UUID |
--owner-message UUID --quote TEXT) --why TEXT --root STORE [--source NOTE] [--dry-run]`): it edits
the `standing:` line in place (a flow list stays a flow list, a block list a block list, `standing: []`
when the last id goes), prepends one row, prints the diff and commits nothing. One call is one row;
a supersession is two calls — `--remove` the old link, `--add` its successor — before one commit.

The authority is **derived, never typed**: there is no `--authority` option (it was removed in
0.6.0; a script that passed it must pass the session flags instead). The command reads the Claude
Code session transcript the same way the filing commands do: a `--yes-message` uuid must be a human
message (`owner-ratified`), an `--owner-message` uuid must be a human message that contains `--quote`
as an exact substring (`owner-verbatim`). Standing needs an owner tier, so a derivation that fails or
is not given refuses and writes nothing. The row's `source` is the derived source line (message uuid,
session, timestamp, plus the optional `--source` note); the quote is checked and never stored.

### Reference marks — the audit of `code_refs:` membership changes

`code_refs:` decides which files a topic is delivered for, so a change to it leaves a row in
`ref_marks:`, a block list written immediately after `code_refs:`, newest row first. Like standing
marks, rows are never delivered and never indexed.

```yaml
code_refs:
  - "src/core/scan/scan_plan.py"
ref_marks:
  - ref: "src/core/scan/scan_plan.py"
    action: add                       # add | remove
    date: 2026-11-07                  # the date part of recorded_at as written
    recorded_at: "2026-11-07T09:12:41-05:00"
    recorded_by: memidx
    authority: agent-inference        # any of the five; the owner tiers when derived from a session
    source: "memidx bind"
    why: "bound by memidx bind"
```

A row is identified by `(ref, action, recorded_at)`. `memidx.py bind` prepends an `add` row in the
same edit that inserts a reference, and `memidx.py unbind TOP-xxxx REF` deletes exactly that
`code_refs` item and prepends a `remove` row; both accept `--why`, `--source`, `--recorded-by` and the
optional derivation flags of `mark-standing` (without them, or when a derivation fails, the row is
`agent-inference`; a failed derivation warns). `unbind` touches no anchor — the frozen capture stays
as history — and may unbind a prefix or a glob. `bind` of a reference whose anchor for the current
link already exists (an unbound one) **restores** it: the reference and an `add` row come back, no
second anchor is written (one baseline per `(ref, link)`; a new baseline is a new link).
`bind` also accepts a prefix (`src/core/`, an existing directory) or a glob (`src/app/*.py`); either is
relative to a code root (an absolute path or any `..` segment is refused), and a prefix must resolve,
through symlinks, to a directory inside a code root. It adds the entry and its `add` row exactly as for a file and writes no anchor row (there is no byte
span to hash), so a prefix or glob is bound and unbound with the same two commands.

Under `--against-ref` the membership change must be paired with rows exactly as `standing:` is (g2
below), on the **set** of `code_refs` entries (prefixes and globs included): one `add` row per entry
in new-minus-old, one `remove` row per entry in old-minus-new, no more. Reordering the same entries
needs no row. Rows recorded at `REF` are frozen by fields and bytes. A brand-new topic needs an `add`
row per `code_refs` entry. Plain lint never requires rows: existing membership needs no historical
rows, and the first later change needs them even on a topic with no `ref_marks:` yet.

### Anchors — what a code reference pointed at when it was bound

A topic may carry an `anchors:` key, written before `links:`. One list item records the SHA-256
of the code a `code_refs` entry named at the moment the binding was captured, so a later delivery
can say whether that code has changed **since capture**.

```yaml
anchors:
  - ref: "memidx.py#for_path_chain_lines"   # exact code_refs string of this topic
    link: L3                                 # a link id that exists in links
    algo: sha256
    digest: "<64 lowercase hex>"             # SHA-256 of the file bytes (span: file) or of the
                                             # inclusive start..end line bytes of the one chunk (span: symbol)
    span: symbol                             # file | symbol
    captured: bind                           # ruling | bind | backfill
    captured_at: 2026-11-02
    code_head: "6228be7"                     # short commit of the code root at capture, or unavailable
    worktree: clean                          # clean | dirty | unknown (the file's git status at capture)
    chunker_version: "..."                   # symbol rows only
```

`captured` says how trustworthy the baseline is: `ruling` (captured in the same commit as the
link), `bind` (captured when the reference was bound to an existing ruling) or `backfill` (captured
later from ledgered history, never the bytes at the original ruling). Only a single file or a
`path#symbol` reference is anchorable; a prefix or a glob has no single byte span and is reported
"not checked". A `path#symbol` reference whose symbol has no chunk at capture is captured as
`span: file`; one that matches two or more chunks gets no row ("symbol ambiguous").

`memlint.py` errors on: a `ref` not present verbatim in the topic's `code_refs` (unless the NEWEST
`ref_marks:` row for that ref is a `remove` — an unbound reference keeps its frozen capture as
history, and a later `add` row re-arms the requirement); a `link` not in
`links`; `algo` other than `sha256`; a `digest` that is not 64 lowercase hex characters; an unknown
`span`, `captured` or `worktree`; a row for a prefix or glob reference (even one whose ref is unbound); under `--against-ref` every one of these checks also runs on each row new since `REF` and on every row of an added topic (rows recorded at `REF` stay freeze-only); two rows for the same
`(ref, link)`; `span: symbol` without `chunker_version`; a row without `captured_at`
(`ref`, `link`, `captured` and `captured_at` together identify a row, so all four are required).

**Undo receipts are not store content.** `bind` and `unbind` keep a worktree-local receipt (the topic's
exact bytes before and after the command) under `git rev-parse --git-path memcontinuum/receipts`, outside
the store tree, so the opposite command can take back an uncommitted edit byte for byte. A receipt is
never read by `memlint.py`, never committed and never part of a record; the rows and anchors a topic
carries are the same with or without one.

**Anchor rows are append-only**, like links: under `--against-ref`, a row present at `REF`
(identified by `ref`, `link`, `captured`, `captured_at`) must survive with the same fields and the
same raw bytes; new rows are free. A recorded row whose identity cannot be built is an error, not
skipped. One baseline exists per `(ref, link)` in this release — a new
baseline is a new link. The wording of every delivered line is capture-relative ("anchor changed
since capture"), and it never changes a link's status or authority.

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

1. The record exists as an `agent-inference` link that still stands (status `active` or
   `provisional`), or as a `reviewer-finding` or `code-derived` link of either status: the
   owner may ratify a reviewer's or a derivation's wording as well. An owner tier, a terminal
   status (`superseded`, `historical`, `declined`) and a link that already carries
   `promoted_by` are never promotable.
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

`memidx.py promote TOP-xxxx Ln --session ID (--yes-message UUID | --owner-message UUID --quote TEXT)`
performs steps 1-3: it refuses a link that step 1 does not allow, appends the
new owner-tier link, and inserts the one `promoted_by:` line into the old one. It reads the
owner's message from the session transcript and refuses when it cannot establish it.

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
  question is not a ruling (`owner-ratified` is the orchestrator's own paraphrase, not covered).
  Enforced for new links only: an error under `--against-ref` for links absent at REF (every link of
  a new file); a warning in plain mode, so links recorded before the check do not block commits
- a new link (`kind: adopted`, or `restored` without `reverses`; status `active` or `provisional`; no
  `reverses`) whose `ruling.text`, after whitespace normalisation, equals a still-operative rejection →
  error. A rejection is a `kind: declined` / `status: declined` link's text or any link's
  `alternatives[].option`; it is operative until a later `status: active` link names it (or, for an
  alternative, the link that recorded it) in `reverses`. The comparison is the whitespace-normalised
  string and nothing else, so a paraphrase is not flagged. Checked against the link's own topic and,
  for a brand-new topic file, every other topic. Enforced for new links only: an error under
  `--against-ref` for links absent at REF; a warning in plain mode (on the link's own topic and,
  once the whole store is read, across topics; old links are tolerated), so links recorded before the check do
  not block commits; `--against-ref` errors for new links
- `status: superseded` without `superseded_by` → error
- `reverses:` without `reason_for_change` → error
- a `kind: reversed` link whose `reverses:` target's `status` is still `active` or `provisional` →
  error, naming the target and its status (`kind: amended` leaves its predecessor active on
  purpose — not covered)
- `current` not equal to the newest link with `status: active` → error (names the correct value)
- a topic in area `processing/*` or `deletion/*` with no `code_refs` → warning
- any `status` or `authority` value outside the five/five enumerated above → error
- a `kind` value outside the five enumerated above → warning for links recorded before the check
  and an error for new links under `--against-ref` (like the question-form check above, since
  `kind` cannot be edited)
- a `standing:` entry naming a link id not in that topic → error
- a `standing:` entry whose link is not `status: active` → error, even when the link was superseded
  by a valid successor — the supersession and the pointer update (or removal) must land together
- a `standing:` entry whose link's `ruling.authority` is not `owner-verbatim`/`owner-ratified` → error
- a `standing:` entry whose link's `ruling.text` contains a raw CR or LF → error; flatten it to one
  line. Any OTHER line/paragraph separator (U+2028 LINE SEPARATOR, U+2029 PARAGRAPH SEPARATOR,
  U+0085 NEL, a tab) is not an error here — `memidx.py standing` collapses it to one space at
  projection time, so it still reaches an agent's context as a single physical line
- a `standing:` list naming the same link id twice → error
- a `ref_marks:` row (never required) that is not a mapping, lacks one of the eight fields, carries
  an unknown field, has an `action` other than `add`/`remove`, an `authority` outside the five, a
  `ref` that is not a string or names no path, an empty `source` or `why`, a `why` containing CR or
  LF, a `recorded_at`/`date` of the wrong shape (as for `standing_marks:`), or the same
  `(ref, action, recorded_at)` as another row → error naming the row
- a `standing_marks:` row (never required — a topic marked before the block existed has none and is
  clean, §7's grandfathering rule) that is not a mapping, lacks one of the eight fields, carries an
  unknown field, has an `action` other than `add`/`remove`, an `authority` other than
  `owner-verbatim`/`owner-ratified`, an empty `source` or `why`, a `why` containing CR or LF, a
  `recorded_at` that is not an ISO-8601 timestamp with an offset, a `date` that is not its date part
  as written, a `link` that is not a link id of this topic (a superseded link stays valid — old rows
  are history), or the same `(link, action, recorded_at)` as another row → error naming the row
- the store-wide `standing:` set — every topic's pointers, together — exceeding 24 links or 4,800
  bytes (UTF-8) of the complete digest `memidx.py standing` would emit (header, every line, and the
  newlines joining them), whichever line is crossed first → error, naming every topic whose pointer
  sits past that line
- frontmatter that does not parse (unreadable, not UTF-8, unterminated, malformed YAML on a
  canonical record), or a typed field in the wrong shape (`links` not a list of mappings, a link
  missing its `link` id, `ruling`/`rationale`/`invariant` not a mapping, a list field carrying a
  non-scalar, `type` or `metadata.type` present and not a string) → error naming the file/field; the same on a note (no schema id/links/type) → warning
- a file under `topics/`, `incidents/`, or `investigations/` whose first line is not exactly `---`,
  or whose frontmatter block has no closing `---` → error naming the file and the missing fence —
  directory membership decides this, independent of whether the raw text otherwise looks canonical;
  without it the file silently parses as an empty note (no schema check, no append-only protection,
  no correct type anywhere downstream). Notes stay legal and fence-free only under `sources/`,
  `inbox/`, and the store README, exactly as before.

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
renamed, is an error naming the path. New links, and changes to `title`, `tags`, or the
body text, are free; `code_refs` membership (a `ref_marks:` row per added or removed reference), `type`, `standing`, `current` and link order are guarded as below. A store's own git
`pre-commit` hook (`hooks/pre-commit-append-only.sh`, wired by
`scripts/repo-init.sh` the same way `post-commit-reindex.sh` is) runs this on
every commit and blocks the ones that fail it; the same check can run again in
CI against a wider range, for a guarantee `--no-verify` cannot bypass. See
`hooks/install-hooks.md` for how it is wired and `docs/INTERNALS.md`'s memlint
section for the full rule table.

Comparing parsed fields alone leaves one gap: a link regenerated through a
YAML dumper can preserve every parsed field while its bytes change entirely
(quoting, key order, wrapping) — invisible to the comparison above. So a
link present at `REF` also has its RAW BYTES frozen — the exact text span
from the item's first line (its own list marker — `- link: L<n>` by hand,
but a `yaml.safe_dump` re-render starts each item with whichever field
sorts first, e.g. `- alternatives:` or `- date:`, never necessarily
`link:`) through the last line belonging to that list item, as it appears
in the file — whenever its parsed fields survive unchanged; a byte
difference there is refused as `<path>:<link>: link reformatted, not
appended`, the same as any other append-only violation. A legitimate
reformat of history is refused too — reformatting history is what
append-only forbids. This adds no new exception: a link whose parsed
fields changed (a valid lifecycle move, or an already-refused edit) is
judged by the field-level rules above only, never by this byte comparison as
well.

Newest-first order is enforced too, narrowly: every link new at `REF`+now
must sort, in the new file, above every link that already existed at `REF`
— a new link inserted BETWEEN two recorded ones (rather than above all of
them) is refused as `<path>:<link>: link inserted out of order`, even
though the old links' own bytes stay untouched.

**`standing:`, `current:` and link order are under the guard too**. For a topic
present at `REF` and now (all judged on the staged blob under `--staged`, which is what the store's
pre-commit hook reads):

- *Marks are frozen (g1).* Every `standing_marks:` row at `REF` must survive with the same parsed
  fields and the same raw bytes, in the same relative order, with every new row above every old one
  (newest first): `<path>: standing mark (<link>, <action>, <recorded_at>): row changed after being
  recorded` / `row reformatted, not appended` / `row removed after being recorded`, and `marks not
  prepended` / `recorded rows reordered`. Unlike anchors, a marks block whose raw rows cannot be
  extracted — at `REF` or in the new blob — is an error, never a note; on the repair path (`REF`
  never parsed, or parsed only through the lenient fallback) the same comparison runs but the old side
  is empty, so it cannot catch a removed block (see the known limit below).
- *A `standing:` change is paired with its rows (g2).* With `old_set` and `new_set` the id sets of
  `standing:` at `REF` and now, the commit must add exactly one `add` row per id in `new_set - old_set`
  and one `remove` row per id in `old_set - new_set`, no more: `standing changed for L4 (add) with no
  matching standing_marks row`, `orphan standing mark (L4, add): standing: did not change that way`,
  `duplicate standing mark for (L4, add) in one commit`. A remove plus an add of the same id with the
  set unchanged is two orphans. Reordering `standing:` with the same set needs no row. A brand-new
  topic needs one `add` row per `standing:` id; rows beside an empty `standing:` are orphans; a new
  topic with no `standing:` stays free.
- *`code_refs:` changes are paired with `ref_marks:` rows, and the rows are frozen.* The same two
  rules (g1 and g2, one implementation) judge `ref_marks:` against the set of `code_refs` entries:
  `code_refs changed for src/b.py (add) with no matching ref_marks row`, `orphan reference mark
  (src/b.py, add): code_refs: did not change that way`, `duplicate reference mark for (src/b.py, add)
  in one commit`, and the frozen-row messages with `reference mark (<ref>, <action>, <recorded_at>)`.
  Existing membership needs no rows.
- *`type` is frozen (delivered records only).* The frozen type is what the index delivered at the compared
  revision (`infer_type`: an inbox directory, then an explicit `type:`, then `metadata.type`, then the
  record's directory) when that is a canonical record type; a quarantined record or a non-canonical delivered
  type is exempt; a change is any difference in that delivered type: `type changed 'topic' -> 'investigation'
  after being recorded`. Leaving delivery (canonical to anything) is refused; entering it (a quarantined
  record, or a note stored under a non-canonical word such as `memo`, `inbox` or a garbled one, repaired to a
  canonical type) is free, like an added file. A typeless record under `topics/` is frozen as `topic` and may
  stay typeless or declare `topic`; it cannot gain `type: incident` or `metadata.type`. The delivered type is
  frozen whatever it is: a valid `type: incident` under `topics/` is frozen as `incident` (declaring `topic`
  is `type changed 'incident' -> 'topic'`), and a move to another directory is a deletion plus an addition
  under `--no-renames`, governed by the deletion rule. A delivered record may not become quarantined: a new side the index would quarantine (schema-invalid) is `record leaves delivery: the new side would be quarantined (<first schema error>)`, on the worktree and staged paths. Plain lint is unchanged.
  The `REF` side is read ONCE, with the rules in force when it was delivered, for every check of the guard
  (type freeze, links, `code_refs` / `ref_marks` pairing, anchors, standing marks, topic classification): a
  validation rule that tightens later (the string-type rule: `type` and `metadata.type` must be strings) never
  releases the history of a record the older index accepted, so a record whose only fault is such a rule is
  still judged at `REF` as the valid record it was, with its malformed fields as the index then read them (a
  note's `type: 42` was the non-canonical type `"42"`, freezing nothing). The new rule applies to the NEW side
  only. One carve-out: a canonical record whose own `type` is a truthy non-string delivered a repr string
  nobody can keep, so its repair to a scalar type is free (its links, `code_refs` rows and anchors stay frozen).
- *Link order is frozen (g3).* The link ids present at `REF` must keep their relative order in the
  new blob (new links go on top; the older newest-first check refuses one placed between old ids): `<path>: links reordered (L2 before
  L3; the chain head is first-active-in-file-order)`. The chain head is the first active link in file
  order, so a reorder is a silent re-ruling; no per-link byte check sees one.
- *`current:` moves with the head (g4).* A written `current:` must name the newest active link of the
  staged blob, and it may change only when the newest active link changed (a new link, a
  supersession, a `historical` move exposing an older active link) or when it repairs a disagreement
  that already existed at `REF`. A new `provisional` link does not change the head, so it never
  licenses a `current` change; removing `current:` while the head is unchanged is refused, adding one
  to a topic that lacked it is a repair.
- *The plain-mode rules run on the staged blob too.* The store hook lints only the working tree, so
  for every modified or added topic the guard itself applies the `standing_marks:` row rules
  (including owner-only `authority`) and the `ref_marks:` row rules, the duplicate-id and eligibility rules for `standing:`
  pointers, and, for an added topic, `current:` equal to its newest active link. When `REF` parsed
  to a mapping whose links are unrecoverable, `standing:`, marks and `current:` are still compared;
  when `REF` was only readable through the lenient fallback parse the old membership is unknown, so
  pairing is skipped (the staged `current:` check still runs) with the note `standing membership at REF unrecoverable (fallback parse);
  pairing not checked`.

Grandfathering (a stricter check judges only what is written after it): a topic marked before this
block existed has no rows and produces no error and no warning — rows are required only for a
`standing:` or `code_refs:` change made from now on, and a `type` recorded before the freeze is
frozen from now on. The boundary: this guard covers `standing`, `code_refs`, `type`, `current` and
link order; `area`, `title` and `tags` are free (`area` filters a search and names the record's
directory, but decides neither delivery nor the standing digest; `project` comes from the command
argument at index time, never from the file).

**Known limit: a repaired record is not guarded for links, anchors or standing marks.** When the
blob at `REF` could not be parsed cleanly (it never parsed as a mapping, or only through the lenient
fallback parse) and the new blob parses cleanly, the guard reports a `repaired` note and treats the
edit as a repair. The lenient parse never recovers `links`, `anchors`, `standing_marks` or `ref_marks` (they are
complex fields), so the old side of every comparison is empty: a removed link, a removed `anchors:`
block and a removed `standing_marks:` block all pass with only the note. The new blob's own rules
still run (every link in it is judged as new, `standing:` and `current:` are checked, and its marks
rows meet the plain-mode rules).

### Cited commits and paths are verified, not merely written

When `memlint.py` is given `--code-root DIR` (repeatable; otherwise this
check is skipped entirely, exactly like the concept-record checks in §8.4 —
no line is printed about citations at all), every commit citation in a
link's `ruling.source`/`evidence` (or a standalone incident/investigation's
top-level `source:`/`evidence:`, §9) is verified the same way `code_refs` are
already verified against paths.

**Recognizing a citation.** A hex token is read as a commit citation only
when introduced one of two ways, and only `commit `/`merge ` are
subject-eligible:
- `commit `/`merge ` (case-insensitive) immediately before the hex — MAY
  also carry a claimed subject: a double-quoted string on the SAME line,
  separated only by spaces/tabs (`commit a1b2c3d "the exact subject"`).
- `at `/`as ` (case-insensitive), or backtick-wrapping with no introducing
  word at all — never subject-eligible, even when a quote happens to
  follow: this store's owner-verbatim convention often puts a quote of the
  owner's own words shortly after any kind of reference, unrelated to a
  commit (`"...gate at af95af2 \"Codex is back, use it rather than
  Opus\""` quotes the owner about something else entirely, not that
  commit's subject — reading it as a subject claim would produce a false
  mismatch against the real one).

In every case the hex run must be **exactly 7 or 40 lowercase characters**
— git's two canonical hash lengths — which is what keeps a 16-hex snapshot
hash, a sha256 prefix, a session id, or a 12-hex render fingerprint from
ever being read as a commit even sitting right next to a trigger word (a
12-hex fingerprint after `at ` is not 7 or 40 characters and is correctly
excluded) — **except a 40-character hex string in backticks, which is
genuinely indistinguishable by design** from a real 40-hex commit hash;
resolving it against the code root is the only way to tell, which is
exactly what this check then does.

A `path/to/file.ext:123` citation (a dotted extension required, so a bare
`16:09`-shaped token is never mistaken for one) is checked for existence
under the code roots and, if found, for having at least that many lines.

**Unverifiable warns, a substantiated mismatch errors, `--strict-citations`
promotes.** A single `--code-root` cannot tell "fabricated" apart from
"cites a different repository" — this store's own real records include a
topic that legitimately cites another project's installer files (it is
literally about borrowing that project's installer) and an incident citing
a commit in that other project's own store. So a citation that resolves to
NOTHING under any configured root — a hash `cat-file` cannot find, or a file
that is not found at all — is a lint WARNING by default, worded "cites X --
not found under any configured code root; unverifiable, not necessarily
wrong": an ERROR the checker cannot substantiate would be a false
accusation. A citation that resolves to something CONCRETE and WRONG — the
hash exists but its quoted subject does not match (compared after
whitespace normalization: a hand-typed citation is expected to preserve the
subject's words, not its exact internal spacing); the file exists but has
fewer lines than cited — is a lint ERROR unconditionally, naming the record,
the link (or the standalone record's id), the field, the citation, and the
mismatch: the checker verified something and it was wrong, exactly the
fabrication class this check exists to catch, not a style nit. An
absolute-path citation is likewise always an ERROR, strict or not — it
violates the citation shape itself, independent of which repository
anything lives in. `--strict-citations` promotes every unresolved
("unverifiable") case to an ERROR too, for a store whose records are known
to cite only the wired repo, where "not found" really does mean wrong;
rejected together with `--against-ref`, same as `--code-root` (append-only
mode never uses a code root either).

**Known misses**, named rather than silently accepted: a bare hash with no
introducing word and no backticks is invisible — recognizing an arbitrary
hex-looking word as a commit citation would be guessing, not verifying; an
ordinary English word that happens to be exactly 7 characters, all drawn
from a–f (`deadbee`, `acceded`), reads as a citation once it follows
`at `/`as ` — not solvable without knowing English from hex; a `file.py:L123`
line locator (an `L`-prefixed number) is never recognized, only bare digits
after the colon; `file.py:123,456` or `file.py:123:45` or `file.py:123-130`
only ever check the FIRST number; a Windows-style `hooks\file.py:3` is never
a citation at all (a literal backslash immediately before a candidate path
excludes it, the same as any other path/word character); a path containing
a literal space still matches only its suffix after the space, for the same
structural reason.

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
whose backend cannot run here is skipped with a warning naming the reason, while a file whose
language has no chunker at all is instead scanned for a marker by plain regex alone — silent when
it holds none, and, when it does, checked against everything that needs no symbol location (the
topic and link exist, are active CONSTRAINT/HOLD, and the topic's code_refs name the file) with a
warning that the marker cannot be attributed to a symbol, rather than warned regardless of whether
it carries a marker at all. Markers verify at functions, methods, and computed
vars/properties — whatever the chunker itself reports a definition line for; a container type
(a class, struct, enum, …) has no such line of its own and is uncheckable.

`memlint.py --code-root DIR` checks the pair both ways. In a file WITH a chunker, only a
`path#symbol` code_refs entry takes part — a glob or a bare path names no symbol, so a marker
under one is an error, not a skip; a file whose language has no chunker at all has no symbol to
hold a marker to that stricter standard, so any code_refs form naming the file (bare path, glob,
or path#symbol) satisfies it instead, per the no-chunker rule above.

Marker → store, in a file WITH a chunker: every marker under a code root must point at a topic and
link that exist, that link must be `active` and CONSTRAINT or HOLD, and that topic's `code_refs`
must name the marked file with the matching `path#symbol` (a prefix or glob that merely happens to
match the same FILE does not count — that is the error `path#symbol` exists to prevent) — else an
error naming the file, line, and reason. In a file with no chunker at all, the same topic/link/
status/tier checks apply, but any code_refs form naming the file (bare path, glob, or path#symbol)
satisfies the code_refs check instead — there is no symbol to hold it to the stricter standard
above, and the one thing left unverified (attribution to a specific symbol) is a warning, not an
error (§2, §8.3 above). Store → code: every active CONSTRAINT/HOLD link whose topic
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
