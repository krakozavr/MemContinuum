# Changelog

## [Unreleased]

### A list of the jobs the code does once, and a check that keeps it so

The engine now carries `docs/REUSE-INVENTORY.json`: the jobs it implements in exactly one place (the semicolon
list join, the two coverage sentences, the filing authorities, the byte-span walker, the millisecond clock,
the two commit-time lint runs, the record parser), the owner of each, and what a second copy would look
like. A test (`tests/test_reuse_inventory.py`) fails when a recognisable second implementation appears: the
same name, a renamed copy of the same body, a second owner of a guarded construct (including inside the
Python the hooks embed), or a caller that goes around the owner. Deliberate variants (the three chunker
backends, the two link classifiers, the parser adapters) are recorded with their reason instead of
guarded. The list is honest about its limits: each row says what its checks cannot see, and a rewrite
under another name still needs a human. `context-pack FILE... --code-root DIR` now also prints the rows for
the files a brief touches, after the decision records, so a dispatched agent is told to reuse the owner;
an inventory file that is present but unusable is named in the pack instead of being skipped silently.
To make the join checkable, the two scripts that each carried their own copy of it now share one in
`scripts/mc-list-lib.sh`; behaviour is unchanged.

    Technical: `mc_reuse.py` (stdlib only; `python3 mc_reuse.py` checks a checkout) loads and validates the inventory, scans Python with `ast` and shell with a bounded scanner for ordinary shell (`name() {` and `function name {`; heredoc and single-quoted `-c` payloads fed to an interpreter are scanned as Python) and runs the five guard methods `name_owner`, `exact_copy`, `token_owner`, `delegation`, `vocab_owner`; a `known_debt` row is rendered as such and never as guarded; the full scan of the tree takes under a second; the shell scan does not see every spelling of a call, and its known misreads are listed in the engineering notes. `context-pack` reads the inventory of each `--code-root` only, adds `inventory` / `inventory_omitted` to the JSON envelope when rows matched and `inventory_error` when the file is present but unusable, keeps decision records and anchor lines ahead of the rows under `--max-bytes` and names rows the cap left out; the routing rule template gains one sentence. `mc_join_semi` (raw join) replaces both `join_semi` copies; the decision writer percent-encodes each element once, then joins. The engine-copy fixtures list `mc-list-lib.sh` and `mc_reuse.py`.

### Taking back a bind or unbind is exact

Typed the wrong reference, or bound and then changed your mind before committing? Running the opposite
command (`unbind` after `bind`, `bind` after `unbind`) on the same reference now puts the topic file back
exactly as it was before the first command, every byte, anchors and pending edits included, and says so.
If the file or the staged copy has changed since, or the topic's committed text changed without the
first command in it, it refuses without writing and names the way out (commit first, or `git restore`) -- unless the tree is clean and the earlier bytes are still in a commit, in which case the receipt is dropped and the command continues as an ordinary edit; every other changed commit refuses and names `--discard`;
once the first command is committed it is history and the opposite command is an ordinary new edit, as
before. Committing some other file in between changes nothing. `memidx.py undo-binding TOP-xxxx [--list]` lists the
pending undo records or undoes the latest one, and `--discard` drops the latest one without touching the topic when a refusal names it as the way out.

    Technical: `bind` and `unbind` write a receipt (topic bytes before and after, the topic's committed and staged entries, command, reference, mark identity) to `git rev-parse --git-path memcontinuum/receipts` before the topic write, per-topic stack and `flock`; the opposite command restores the before-bytes only while the topic equals the after-bytes, its committed entry at HEAD and its staged entry are the recorded ones and the topic has not moved (a blob equal to the after-bytes at HEAD means committed history; a git probe that fails, including the work-tree check, refuses; `undo-binding --discard` drops the latest receipt and writes nothing); a receipt that cannot be written durably (file and directory fsynced), or a lock that cannot be taken, refuses the command and writes nothing; `--dry-run` writes none; a stale, crashed or malformed receipt is dropped or ignored and never deletes anything; the commit guard and `memlint.py` are unchanged.

### Commit guard refuses on a crash; honest latency bars

Three small corrections. The store's commit guard used to let a commit through when the check itself
crashed, which is how a history edit once slipped past it; now a crash refuses the commit, prints the
engine's output and names the one-commit override (`MC_STORE_LINT_ALLOW_ENGINE_FAILURE=1`), which is read
from the launching environment only, never from the sourced `config.sh`, while a guard that simply cannot
run (no history yet, no Python, wrong folder) still lets the commit through; `memidx.py stats` counts a
crash refusal as refused and shows it as its own sub-count. The hook latency tests now hold a hook to its
own watchdog budget rather than a literal second, and the SessionEnd hook's log line carries `elapsed=`
like the other hooks do, at no measurable cost.

    Technical: `hooks/pre-commit-append-only.sh` on `MC_LINT_APPEND=engine-failure` logs `rc=<n> engine-failure refused`, prints the output plus the override line and exits 1 (override: `skipped=engine-failure override=1`; the unborn-HEAD, no-python, not-the-store, no-lint-lib and root-unset skips are unchanged); the hook reads the override into a readonly `MC_PC_OVERRIDE_IN` before sourcing anything; `memidx.py stats` counts `rc=<n> engine-failure refused` under `refused` and adds `refused_engine_failure`; `assert_hook_logged_under_one_second` becomes `assert_hook_logged_within_budget` (the hook's `elapsed=` against the test env's `MC_WATCHDOG_BUDGET`, default 2 s, or SessionEnd's fixed 1.2 s; 5 s hang guard kept); `sessionend-stamp.sh` appends `$(mc_elapsed_field)` to its line (six runs, 89-94 ms wall before and after).

### Filing tools stop sending agents into dead ends

Four small gaps in the tools that file and bind decisions. `bind` now accepts a directory prefix
(`src/core/`) or a glob (`src/app/*.py`) as a topic's code reference, the two shapes the schema
always allowed: it records the reference and its dated row, writes no anchor (there is no single
file to fingerprint) and says so. `promote` can ratify an inference that is still active, not only
a provisional one, and a reviewer's finding or a derivation too; a superseded link, an owner's own
words or a link already promoted stay refused. When `record` finds its file already there, it now
names the existing topic and the `append` command to add to it, instead of a bare refusal. And the
linter rejects a `type:` that is not a plain word (a list, a mapping, a number, a yes/no or a date), which used to pass and was indexed as its text. The rules file that repo setup renders into each project also gains one sentence:
a decision must still be true tomorrow, so approvals, progress notes and status lines are session
state and stay out of the store; repos pick it up at their next update.

    Technical: `bind` of a non-anchorable ref calls `unanchored_ref_problem` (trailing-slash prefix naming an existing directory under a code root, or a glob character; no `#symbol`), inserts the entry and its `ref_marks` `add` row, writes no anchor row (`bound_edit_problem(anchored=False)` requires none) and exits 0; a directory without its slash still exits 2; `promotable_link` allows authority `agent-inference`/`reviewer-finding`/`code-derived` with status `active`/`provisional`; `path_exists_refusal` serves both `path-exists` sites; `validate_record_shape` reports a `type` or `metadata.type` that is present and not a `str` (absent and null stay legal) (a canonical record is quarantined, a note drops the field with a warning) and `is_canonical_frontmatter` no longer hashes a list `type`; the index generation moved from 6 to 7 so the next reindex re-parses every record once (an unchanged file skips the parser, so a row stored under the old permissive type rule would otherwise survive); `templates/memcontinuum-rules.md` changed, so rendered rules files read `rules-stale` until `memcontinuum-update.sh --apply`.

### Files git ignores no longer count as uncovered

The coverage reminder used to list scratch files, build output and anything else the project's `.gitignore`
excludes as "edited files with no decision topic", and counted them. Now a file the code repository's git
ignores is left out of both the list and the count. A file that is tracked is still reported even when it
matches an ignore pattern, and when git cannot answer (not a repository, not installed, too slow) the file is
reported as before. Editing a `.gitignore` is noticed when the edit passes through the session's edit ledger and the next prompt reaches the coverage check: the session's remembered verdicts
are dropped and every file is checked again. Cost: one extra git call per code root when files are first
classified, about 0.4 s for 400 files on a Windows-mounted drive, nothing for files already classified. The
coverage sentence is now built in one place for the prompt and session-start hooks, and the edit ledger no
longer starts a Python, watchdog or Git process for tools that cannot write files (`SubagentHandback`, `SendMessage`, `Monitor` and
similar).

    Technical: `memidx.py unmapped` gains the verdict `ignored` (JSON list `ignored`, `by_path` value, text line `ignored: N`) from one `git --no-optional-locks -C <root> check-ignore -z --stdin` per code root (plain, not `--no-index`; failure or exit other than 0 or 1 ignores nothing; one shared deadline `MEMCONTINUUM_IGNORE_CHECK_BUDGET`, default a quarter of the hook's effective watchdog budget, exported by the launcher as `MC_WATCHDOG_EFFECTIVE_BUDGET`; JSON `ignore_check`: `complete`, `timeout` or `skipped`; store paths excluded by canonical prefix); `userprompt-remind.sh` caches `ignored` like `unmapped` and drops `unmapped_cache` when the ledger holds a `.gitignore` or `.git/info/exclude` edit newer than the oldest cached verdict; `hooks/mc_nudge.py` (`coverage_fact`, `coverage_question`, stdlib only) replaces the two inline copies in `userprompt-remind.sh` and `sessionstart-remind.sh`; the `ledger-post-edit.sh` prefilter adds ten harness-side tool names and keeps `mcp__*` and unknown names on the shell-diff fall-through; the tests' one-second bounds on hook latency now assert the hook's own `elapsed=` (or the property they stood for) with a 5 s hang guard.

### Hooks stop timing out on a Windows-mounted code root

On WSL, a project whose code lives under `/mnt/` (a Windows drive) saw its prompt, edit-ledger and
refresh hooks killed by the 2-second watchdog, because every file check crosses to the host. Three
changes. The coverage check no longer re-walks each file's directory chain: a 422-file edit list that took
7 s now takes about 0.6 s, with identical results. The prompt hook remembers the verdicts it already
reached in the session and classifies only files edited since the previous prompt; the memory is dropped
as soon as the store has a new commit or any uncommitted change (a binding being filed counts at once),
when the index had to be rebuilt, and for any file edited again. And the watchdog gives a hook twice its
usual time when a configured code root starts with `/mnt/` (the factor 2 is built into the launcher;
`MEMCONTINUUM_WATCHDOG_FACTOR` in `config.sh` overrides it; the SessionEnd hook keeps its fixed budget). `hook.log` kill lines now
show the budget that applied, and the prompt and ledger lines show `elapsed=`.

    Technical: `unmapped` resolves a path as its directory (cached per call, one `lstat` per distinct directory, one listing where a directory holds several of the paths) plus a symlink check of the final name; `mc-watchdog.sh` scales `MC_WATCHDOG_BUDGET` for a `/mnt/` root and honours `MC_WATCHDOG_BUDGET_FIXED=1`; session key `unmapped_cache: {store_head, verdicts, displays, classified_at}` is read and written by `userprompt-remind.sh` under the session lock, absent or malformed means "classify everything"; whenever the hook consults the cache it makes one `unmapped` call (zero paths when nothing is new; the block is entered for a coverage candidate or a moved code HEAD, and a prompt outside both reads no cache and makes none; `unmapped` now accepts none and reports `healed`) and reuses cached verdicts only while the store HEAD is unchanged, `git status --porcelain --untracked-files=all` of the store is empty, the call did not heal the index and the file was not edited after its verdict (`seen_at` and `classified_at` are both fractional epoch seconds; a fractional `seen_at` compares strictly `>`, only a whole-second one from an older hook uses `>= floor(classified_at)`); `memlib.sh` gains `mc_elapsed_field`, built on `mc_now_ms` (now in `mc-query-lib.sh`, which `memlib.sh` sources).

### Changes to a topic's code references leave a record

A topic's `code_refs` decide which files its decisions are delivered for, but a
reference could be added or dropped without a trace. Now every change leaves a
dated, attributed row in a new `ref_marks:` block, and the store's commit check
refuses a change to `code_refs` that has no matching row. `bind` writes the row
itself. The new `unbind` command takes one reference off a topic (a path, a
`path#symbol`, a prefix or a glob), records a `remove` row and leaves the frozen
anchor in place as history; binding the same file again restores the reference
without capturing a second baseline. Existing topics need no rows until their
next change. A delivered record's `type` can no longer be changed once recorded (a quarantined or non-canonical record may be repaired freely, and a delivered record may not become quarantined). A delivered record cannot be replaced by a symlink or a submodule entry either; a path that was itself a symlink or submodule entry in the compared revision delivered nothing, so replacing, retargeting or deleting it is not refused.

**Breaking:** `mark-standing` no longer takes `--authority`. The authority of a
standing change is read from the session transcript like a filed link: pass
`--session ID` with `--yes-message UUID`, or `--owner-message UUID --quote TEXT`.
A change that cannot be confirmed from the transcript is refused. The command
the filing tools print has been updated to match.

    Technical: `ref_marks:` rows `{ref, action, date, recorded_at, recorded_by, authority, source, why}` (SCHEMA "Reference marks"); the commit check pairs rows with the SET of `code_refs` using the same pairing and freeze rules as `standing_marks:`, so reordering needs no row and prefix and glob references count; `memidx.py unbind TOP-xxxx REF [--why] [--dry-run]`; an anchor's ref may be absent from `code_refs` when the newest `ref_marks` row for it is a `remove`; `memlint.py --against-ref` also refuses a changed `type` on a recorded topic; `mark-standing` reads its authority through the same session derivation as `record`; `bind`/`unbind` accept the derivation flags optionally (`agent-inference` without them).

### A rejected option is refused when it comes back

When you file a link, the command now lists the options the candidate topics
already rejected (declined links and rejected alternatives that no later link
restored), with the reason each was rejected. If the new ruling repeats a
rejected sentence word for word, the command refuses; to reopen a rejection,
append a link that names it in `reverses` with a reason. The same rule runs at
commit for links written by hand: an error for new links, a warning for old
ones. A paraphrase is shown, never refused.

    Technical: one comparator in memlint (`rejection_rows`, `rejection_is_operative`, `readoption_matches`, whitespace normalisation only) used by `_declined_check` in `_finish_write` and by `_added_links_errors` under `--against-ref`; `--show-restored`; one `declined-lookup` line per run in hook.log, never text.

### File a decision with one command

`record`, `append` and `promote` now write store records for you. `record`
starts a new topic, `append` adds a link to an existing one, and `promote` turns
an agent's inference into a ruling the owner affirmed. Each prints the exact
change and leaves the commit to you, so the commit message can name the
decision. Existing links are never rewritten: the command only adds lines, and
the same check that guards the store at commit time is run on the result before
it reports success.

The authority of a new owner-tier link is read from the session transcript, not
typed: the owner's quote must appear word for word in a message the owner wrote,
or the owner's yes-message must exist. When the transcript cannot confirm it,
the link is filed as an agent inference with a warning, and `promote` refuses.
Before writing, the command shows the three nearest existing decisions so you
can append instead of starting a duplicate, and afterwards it checks the new
record can be found by its title and prints the `mark-standing` command if the
new link could be standing.

    Technical: `memidx.py record|append|promote`; one splice (`_splice_block_item`) shared with the anchor and standing-mark writers; `--session` with `--owner-message UUID --quote TEXT` (owner-verbatim, exact substring) or `--yes-message UUID` (owner-ratified); `$MEMCONTINUUM_TRANSCRIPTS_DIR` overrides the transcript directory; one `filing` line per run in hook.log carries outcome and ids only, never text.

## [0.5.0] — 2026-11-05

MemContinuum now tells you when code has changed since a decision was bound to
it. Changes to the standing decisions delivered at every session start must
leave an audit record. Reviewers and agents can be handed the decision records
themselves, each with its authority.

### See when code has changed beneath a decision

Before an edit, MemContinuum now says when a bound file or symbol differs from
the baseline captured when the binding was made. The same note can appear at
session start and in reviewer context packs. Missing, never-captured and
unchecked baselines are labelled as such. A warning never changes a decision's
status or authority. When the size cap bites, the session-start summary line is
dropped before any standing line, and in a context pack the least important
anchor lines go first and no topic is dropped to make room.

    Technical: `anchors:` stores the captured hashes; `memlint.py` validates the `anchors:` rows and `--against-ref` freezes them like links; session start needs a configured code root, and context packs include anchor lines when given `--code-root DIR`.

### Connect decisions to code with a reviewed change

You can now capture a code baseline at the moment you add a reference to a
decision. A proposal command reads a session's search fallbacks and edit ledger,
suggests which edited files a topic may want to bind, and lists separately
labelled filing hints for unmapped files; it leaves it to you to review and
apply. References bound before baselines existed can receive one too, labelled
as captured now, and rerunning that backfill changes nothing.

    Technical: `memidx.py bind TOP-xxxx REF` and `anchor-backfill` support `--dry-run`, and nothing is committed for you; `propose-bindings --session ID` prints suggestions, with optional `--write-inbox`.

### Know who changed the standing decisions

Adding or removing a standing decision now records who changed it, when, on
whose word and why. The rows are written on owner authority only. The store's
commit guard refuses a change without a matching record, new rows that match no
change, an edit to a recorded row, a reordering of existing links, and a
`current:` that moves without the newest active link moving.
The rows are never delivered or indexed and add nothing to the session digest.
The linter also reports a standing list that names the same link twice.

    Technical: `memidx.py mark-standing TOP-xxxx Ln --add|--remove --authority ... --source ... --why ...` updates `standing:` and the frozen `standing_marks:` rows, supports `--dry-run`, and commits nothing for you; `memlint.py --against-ref` checks the change.

### Give reviewers and agents the actual decisions

A new command prints one paste-ready block holding the standing decisions and
the decision chains bound to the files being reviewed, each with its authority
and store path. If the size limit leaves a topic out, the pack names it, and a
fingerprint identifies exactly what was sent. The rendered repository rules now
tell reviewer and agent briefs to open with this pack.

    Technical: `memidx.py context-pack FILE... [--topic ID] [--query WORDS]` ends with a SHA-256 `pack sha256=` line; `--blind` prints a fixed notice and opens no index.

### Keep decision lookup working across the checkout

Indexing only a subdirectory no longer limits decision lookup to that
subdirectory, so edits in worktrees inside the checkout still retrieve their
decisions while the code index keeps its configured scope. Code roots now
scope only the index, the new-file nudge and the edit ledger. A worktree edit
under a narrow root is now recorded at its real path. On macOS, a temporary or
home directory behind a symlink no longer sends the fallback search the whole
machine path, because the remapped path is printed in the configured root's
spelling again; where a symlink sits inside the repository, the real-path
spelling is still printed.

    Technical: `repo-init.sh` renders the pre-edit filter and `MEMCONTINUUM_STRIP_PREFIX` for the Git toplevel of each `--code-root`, falling back to the root itself with a `note: retrieval root for ...` line when Git cannot say; the macOS case is `/var` -> `/private/var`.

### Look up decisions from a sibling worktree, and never write the index before an edit

Decision lookup before an edit can no longer modify the index: an outdated
index gets a one-line refusal until the next store commit reindexes it, and the
edit is never blocked. A worktree checked out next to the repository
(`git worktree add ../x`) now gets the same lookup as the main checkout, and an
edit under the repository's parent folder that belongs to no wired project is
declined without a search. The parent folder is not watched when it is `/` or
your home directory; the installer prints a note.

    Technical: `memidx.py for-path` opens the index read-only and exits 5 on `upgrade-required` (logged `index-needs-migration`); `repo-init.sh` renders the pre-edit pair for each retrieval root's parent; outcomes `worktree-unwired`, `worktree-unresolved` and `outside-roots` never run a search.

### See which session received which decisions

Every pre-edit line in the usage log now names its session, and a line that
delivered topics also carries a short stable name for that delivery, so later
lines can point back to it. Code searches now leave a log line too and appear
in the statistics; a logging failure never changes a search's result. The
rendered rules file also gains a duplication rule: a brief tells a dispatched
agent to find and name the existing implementation, and a gate asks whether the
change duplicated anything.

    Technical: `hook.log` pre-edit lines gain `session=` (`-` when none) and `exposure=`; `memidx.py code-search` appends `code-search outcome=ok|no-hits|error hits=N q=... project=...`, reported in the `code_search` block of `memidx.py stats`.

### Small fixes

`memlint.py --code-root` no longer reports a marker example shown inside a
fenced block or an inline code span of a Markdown file. `mark-standing --remove`
now finds a `standing:` item written with YAML quoting. The documentation now
states the append-only guard's known limit: when the earlier version of a
record could not be parsed and the new one can, the edit counts as a repair, and
a removed link, `anchors:` block or `standing_marks:` block is not caught; only a note is printed. The
schema reference no longer says a `project` change can drop a topic from the
standing digest; only a change of `type` can. An internal cleanup in
`memidx.py` changes no behaviour.

    Technical: markers in a Markdown file's prose are still checked; `mark-standing --remove` leaves other list items exactly as written.

### Notes for upgraders

Run `memcontinuum-update.sh --apply` once. It re-renders the wiring (the
retrieval root and the parent-folder filters) and the rules file, and wired
repositories show as stale until then. Topics marked as standing before this
release have no historical mark rows and are not flagged for that absence, so
no backfill is needed.

    Technical: Run `memcontinuum-update.sh --apply`; existing standing marks need no backfill.

## [0.4.0] — 2026-10-31

Code indexes now refresh automatically, and background workers fill in missing
embeddings. This release also adds store backups, warnings for bypassed commit
checks, and clearer reports of repository wiring that needs attention.

### Keep the code index current

MemContinuum now checks the code index at session start and refreshes changed or deleted files at turn end.
Failed or interrupted refreshes remain visible; automatic refresh never creates an index that does not exist.
Indexing respects `.gitignore`, and refresh removes previously indexed ignored content and roots no longer configured.
Re-run the updater to add the refresh hooks to existing projects.

    Technical: Run `memcontinuum-update.sh --apply`; `MEMCONTINUUM_CODE_REFRESH_LIMIT` defaults to 500 files, `MEMCONTINUUM_CODE_REFRESH_BUDGET` allows 8 s on `Stop` and 2 s on `SessionStart`, and `.code-stale` records refresh problems.

### Fill in embeddings automatically

Missing code embeddings now fill in after installation and when a session starts or ends.
Text search remains available during catch-up, and an interrupted worker keeps its completed progress. The refresh at the end of a turn is text-only, so a fully embedded index reads `partial` until the worker catches up.
CPU embedding is about eight times faster, while vector scoring is about ten times faster with unchanged scores and rankings.
The model change triggers a one-time background re-embed of store records and code chunks; the first embedding download is about 133 MB.

    Technical: Embedding status is `none`, `partial`, or `full`; set `MEMCONTINUUM_EMBED_WORKER=0` to disable background embedding.

### Choose code, tests, or both

Code search now returns code by default, with separate options for tests alone or both together.
Results can name up to three indexed tests that mention the matched symbol.
These filters need no reindex, but they cannot return tests excluded from the configured index.
Hybrid code search also uses a revised ranking setting.

    Technical: `code-search --tests` selects tests; `--all` includes both; hits gain `tested_by` and `role`; code ranking uses RRF k=30, while store search keeps k=60.

### Back up the store and see bypassed checks

Store commits can now create verified Git bundle backups and retain a configured number per store.
Backups refuse paths that escape the backup directory and refuse unsafe destination paths.
A commit that bypasses checks is checked again afterward, with a warning that remains visible at session start until a clean commit follows.
Without watchdog Python, neither the backup nor the after-commit check runs.

    Technical: Configure `memcontinuum-setup.sh --backup-dir DIR [--backup-keep N]`; the updater's `backup` column states off, none, current, behind, unreadable or unknown; unsafe project names skip both checks and backups, while an invalid backup directory skips backups only.

### Find hook directories missing from the registry

The updater now reports directories that contain a project's hooks but are absent from its registry entry.
It supplies a command to record the missing directory (marked incomplete, with the repository left for you to fill in, when no local checkout of the remote is known), or asks you to wire the project or remove its hooks when no project entry exists.
This scan reports findings without recording them or changing the updater's exit status.
Session-start and refresh logs also identify the working directory without allowing path text to confuse project statistics.

    Technical: `unrecorded-claude-dir` rows supply `memcontinuum-decide.sh wired ...`, marked INCOMPLETE with `--repo` when no local checkout is known; `MEMCONTINUUM_CLAUDE_JSON` overrides the project list; logs use percent-encoded `cwd=` values.

### Known limitation: lookup follows the indexed directories

Pre-edit decision lookup still follows the configured code roots, leaving edits elsewhere without retrieval.
Worktree edits are recorded, but their recorded paths can incorrectly repeat the first code-root prefix.
Ordinary checkout edits outside both the code roots and the store are reported as out of scope and are not recorded.

    Technical: Wiring only `Sources` and `Tests` limits retrieval to those paths; a worktree ledger path can incorrectly become `Sources/Sources/Foo.swift`.

## [0.3.0] — 2026-10-15

Sessions now begin with explicitly selected standing decisions.
Decision lookup can also offer clearly labelled guesses when a file has no
binding, while stronger store checks protect recorded history.

### Receive standing decisions at session start

Topics can now name owner-authority decisions that apply throughout the project, and sessions receive their own wording as citations.
An unchanged digest is not repeated on resume, but is delivered again after a clear or compaction.
The set is limited to 24 links or 4,800 bytes; invalid entries and oversized sets are refused.
After upgrading, reindex each project's store: until then, session start reports that the digest was skipped, and the updater reports every wired store whose index is behind as needing an upgrade.

    Technical: Use `standing:` and `memidx.py standing [--root DIR] [--json]`, which opens the index read-only, refuses when the index is not current (including one written by a newer engine), and refuses rather than truncating when the set is over the cap; index generation moves from 5 to 6; run `memidx.py reindex --root STORE --project NAME` or `memcontinuum-update.sh --apply` (the updater reports `index: upgrade-required` until then); newer-generation indexes are reported and left untouched.

### Get a possible decision when no binding exists

Before an edit to an unbound file, MemContinuum can search using words from its path and offer up to two nearby decision records.
Results are labelled “may be unrelated,” create no binding, and include bounded decision chains with explicit omission markers.
New files receive the same lookup, using their remapped project path inside worktrees.
The lookup reads the index without migrating it, refuses incompatible schemas, and skips files inside the store.

    Technical: `memidx.py search` gains `--read-only`, `--hydrate`, `--hydrate-max-bytes` and `--decisions-only`; `MEMCONTINUUM_FALLBACK_MAX_BYTES` defaults to 2000 per hit; `MEMCONTINUUM_FALLBACK_MODE` selects `hybrid`, `vector`, or `fts`; the second-hit RRF floor `MEMCONTINUUM_FALLBACK_SECOND_FLOOR` defaults to `0.0314`, with `0` disabling it.

### Search from your prompt when you opt in

An optional prompt channel offers at most one nearby decision per prompt and avoids repeating topics already delivered in the session.
It requires at least four content words and skips recognised machine-delivered messages; unfamiliar framing is treated as a typed prompt.
The raw prompt is never stored, but derived search terms enter the log, so a pasted secret can leave a fragment there.
With the channel disabled, the hook does not read prompt text.

    Technical: Add the project to `prompt-query.projects` or set `MEMCONTINUUM_PROMPT_QUERY=1`; `MEMCONTINUUM_PROMPT_QUERY=0` overrides both; search uses full text and logs derived terms as `q=`.

### Keep weak keyword matches out of hybrid results

Hybrid search now uses vector-only ranking when the leading full-text result shares too little vocabulary with the query.
Explicit full-text and vector modes retain their existing behaviour.
Search output identifies which ranking was used and explains when the full-text channel stepped aside.

    Technical: Hybrid steps aside below content-term coverage `0.25`; `search --json` reports `"fusion": "rrf"` or `"vector-only"`.

### Protect recorded decisions and reject malformed records

Recorded links now preserve their exact bytes, so changes to quoting, wrapping, or key order are refused alongside content edits; valid lifecycle changes remain allowed.
New links must appear newest first, and the store commit check now rejects schema errors, including missing or unclosed frontmatter.
Citation checks reject incorrect commit subjects, out-of-range file citations, and absolute paths; unresolved references warn unless strict checking is requested.
Unknown link kinds and owner quotations phrased as questions block new records, while older records receive warnings instead.

    Technical: `memlint.py --against-ref` enforces append-only checks; `--code-root DIR` checks citations; `--strict-citations` makes unresolved citations errors; question warnings apply only to active or provisional links.

### Retain useful logs without unlimited growth

Hook logs now retain several rotations, and statistics read every retained file.
Interrupted rotation is recovered at the next session start, while a reduced retention setting takes effect on the next rotation.
Statistics also report fallback searches, their timing, and prompt-derived searches.

    Technical: `MEMCONTINUUM_LOG_MAX_BYTES` defaults to 5242880; `MEMCONTINUUM_LOG_KEEP` defaults to 12; `memidx.py stats` gains `fallback` and `prompt_query` blocks.

## [0.2.0] — 2026-09-15

Worktree edits now participate in retrieval and edit tracking, with explicit
limits on where retrieval works. Documentation also clarifies when a retrieved
decision reaches the agent.

### Understand when an edit receives its decision

Automatic retrieval runs before the edit reaches disk, but the decision chain reaches the model with that edit's tool result.
It can guide what the agent does next; it does not change the edit that triggered the lookup.
A failed lookup never blocks or alters the edit.

    Technical: Pre-edit retrieval is automatic and fails open; its context arrives in the same turn as the triggering tool result.

### Track edits inside worktrees

A worktree inside a configured code root now receives decision lookup and ledger entries; a sibling worktree receives ledger entries only.
Unwired and unresolved worktrees are reported explicitly, and code indexing skips nested linked worktrees and submodules.

    Technical: Keep worktrees under `<root>/.worktrees/` for retrieval; logs distinguish `worktree-unwired` from `worktree-unresolved`; indexing skips directories with a `.git` file.

### Keep machine wiring out of the repository

The entire root `.claude/` directory is now ignored because it contains machine-specific rendered wiring.
The machine-content guard also explains what a flagged file risks and the two supported ways to resolve it.

    Technical: The repository ignore rule is now root-anchored `/.claude/`, replacing the narrower skills-only rule.

## [0.2.0rc5] — 2026-09-10

Matched pre-edit log lines now name the topics they injected (`topics=`), so a matched edit can be graded as a retrieval sample and the statistics report the most-injected topics. The hook log now rotates once it grows past a size limit, keeping the previous file as `hook.log.1`, and the statistics read both files. A supported language that appears unwired is now put to you as a choice, wire it now or never mention the extension again, and the new-file hook no longer offers it for files the code index refuses to index, such as `.d.ts`. The updater now reports the machine layer by default, manual wiring through the `memcontinuum` skill is documented as the supported setup route, and the manual uninstall steps and several documentation claims were corrected. A first retrieval benchmark under `bench/` scores this project against a keyword baseline and a no-memory control, with a shuffled-query control that makes an inconclusive run say so.

    Technical: `topics=` is capped at 10 ids with a `+N` tail; `MEMCONTINUUM_LOG_MAX_BYTES` defaults to 5 MiB; `--machine` is an accepted no-op and `--no-machine` opts out; the unwired-language choice reuses `--never-ext`.

## [0.2.0rc4] — 2026-09-07

Append-only history is now enforced when the store is committed: recorded link bodies are frozen, a status may move forward only once from active or provisional to a closed status, and a removed link or a deleted or renamed topic file is refused. The decision index skips symlinks in the store, quarantines a malformed record instead of crashing, hashes every record so a same-size rewrite shows up as a change, and tags every vector with the model that made it, falling back to text search when vectors do not match. The code index judges freshness from more file signals, can prove it on request, and rebuilds once on a schema change, keeping roots and languages but losing embeddings. Hooks now cover every configured code root, the store's post-commit reindex no longer waits for embeddings, the edit ledger catches changes made from the shell, and one line is added when a configured code root's HEAD moves to a commit that names no decision and the session edited at least one file under that root that no topic covers; a commit whose edits are all covered gets no line. The linter now checks decision markers under a code root against the store. Search now defaults to active or status-free records and leaves inbox drafts out, new WSL installs default to a store on the native disk with name conflicts disambiguated or refused, and the documentation states that automatic lookup covers only the Edit and Write tools.

    Technical: `memlint.py --against-ref REF [--staged] ROOT` runs as the store's `pre-commit` hook and `--no-verify` bypasses it locally; `--status any` and `--include-inbox` widen search; `code-search --verify-content` proves freshness; `--db` also moves companion logs and worker files beside the database; run `memcontinuum-update.sh --apply` for multiple-root hook coverage; an explicit `--store` wins, and a store-name conflict can exit 17; a `decision:` marker under `--code-root` must name an active constraint or hold, else it is an error; in a file with a chunker only an exact `path#symbol` reference counts, in a file without one any reference naming the file does; a missing marker on such a link is a warning, and a symbol absent from the file is an error; `--debug` re-raises instead of degrading, for local debugging; `for-path --with-chain-text` folds the chain text into the JSON answer.

## [0.2.0rc3] — 2026-09-04

The code index now chunks JavaScript, TypeScript, TSX, Java, PHP, Rust and Lua alongside Swift and Python, through one shared tree-sitter backend. TypeScript and TSX are separate language values, and the preflight report names each language backend as working, mismatched against its pinned versions, or missing. A file a backend cannot chunk is recorded as not indexed and retried once the missing grammar is installed. The linter's symbol check now separates a symbol that is not there from a file it cannot read, warning with the reason and remedy for the second.

    Technical: `--lang typescript` covers `.ts` and `--lang tsx` covers `.tsx`; `backend-preflight` reports `ok`, `pin-mismatch` or `missing`; only a symbol absent from a readable file is an error.

## [0.2.0rc2] — 2026-09-03

The installer and the decide script now record a project's paths in physical form, so a project reached through a symlink gets one registry row instead of one per spelling. The updater reports a recorded store path that is a symlinked spelling and rewrites just that field on `--apply`. The ledger hook and the new-file nudge share one symlink-safe path helper. The declared minimum Python is now 3.12 everywhere, matching the dependency lockfile, and CI tests Python 3.12 and 3.13 on Ubuntu plus a macOS runner. A session that begins with `/clear` now keeps the coverage and look-back nudges firing.

    Technical: the updater reports `store-form-stale` and, on `--apply`, `store-form-updated`; the shared helper is `hooks/mc-path-lib.sh`; the floor is `requires-python`.

## [0.2.0rc1] — unreleased

Code indexing gained several code roots per project, per-file provenance status, a preflight report, a one-shot heal on search, and a linter that checks a reference against every root. A missing decision index is never created by a read, only by an explicit reindex, and every read now names the problem instead of answering "no decisions" from an empty store; an embedding reindex backfills vectors that no longer match their record, and a stale vector is excluded from ranking until then. Authority tiers and invariants are now enforced, a code reference matches by path segment, an empty reference matches nothing, and individual links are retrievable on their own. The pre-edit hook now has a time bound and answers that retrieval did not complete rather than staying silent. Search-recipe and schema documentation was corrected, and continuous integration, a dependency lockfile and this changelog start here.

    Technical: per-file status is `ok`, `partial`, `failed` or `not-indexed`; an invariant blocks only when owner-confirmed or backed by validated evidence; an empty or fragment-only reference is a linter error.

## [0.1.0]

First tracked release: an append-only markdown decision store (topics, incidents, concepts), schema-validated before a record lands and indexed for search, with a pre-edit hook that hands over the governing decision automatically and a reindex on every store commit. A backend-neutral code index (Swift and Python natively) ties a code hit back to its concept and decision chain, with consent-driven setup per repository.

    Technical: SQLite FTS5 with optional vector search; `scripts/repo-init.sh` wires one store and the hooks per project, and `memcontinuum-setup.sh` sets up the venv and machine config once per machine.
