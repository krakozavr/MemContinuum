# Internals

Maintainer notes: the constraints the implementation is built around, and why
each one holds. The README describes the product; this file describes the
machine. `docs/DESIGN.md` covers the product-level reasoning (chains over
notes, forced retrieval, no auto-capture) and `docs/SCHEMA.md` is the record
schema and linter contract.

Contents: [hooks](#hooks-and-the-fail-open-contract) ·
[python resolution](#python-resolution-and-configsh) ·
[settings merge](#settings-merge-and-identity-markers) ·
[repo-init guards](#repo-init-guards) ·
[decision registry](#decision-registry-keying) ·
[hook path syntax](#path-syntax-in-hook-filters) ·
[watchdog](#the-watchdog) ·
[bash 3.2](#bash-32-discipline) ·
[code index](#code-index) · [memlint](#memlint) ·
[storage and index](#storage-and-index) ·
[decision index provenance](#decision-index-provenance-and-embedding-lifecycle) ·
[standing decisions](#standing-decisions) ·
[CLI semantics](#cli-semantics) ·
[tests](#test-conventions)

---

## Hooks and the fail-open contract

Eleven hook scripts live under `hooks/`, alongside three shared libraries
(`memlib.sh`, sourced unconditionally by the five write-side hooks; `mc-watchdog.sh`; and
`mc-path-lib.sh`, one pure side-effect-free function -- symlink-safe
containment -- sourced by `memlib.sh` and directly by `newfile-nudge.sh`,
which needs it unconditionally but deliberately does not source `memlib.sh`
for that. `newfile-nudge.sh` does lazily source `memlib.sh` on its own rare
known-but-unwired-language branch (newlang-nudge), to reuse
`mc_state_file_for`/`mc_update_state_json` for that nudge's per-session
dedupe -- never on the common already-wired path, which still pays nothing
extra).
Eight of the eleven are wired into a project's
`.claude/settings.local.json` by `scripts/repo-init.sh`; `post-commit-reindex.sh`
and `pre-commit-append-only.sh` are invoked from the store's own git
`post-commit`/`pre-commit`; `memcontinuum-detect.sh` is
wired one level up, into `~/.claude/settings.json`, by `memcontinuum-setup.sh`.

| script | event | does |
|---|---|---|
| `pre-edit-chain.sh` | `PreToolUse` (Edit/Write, filtered to `--code-root`) | `for-path` lookup on the file being edited; injects matching chains as `additionalContext`. On a genuine miss, runs the search fallback (see below) on the path's own words |
| `newfile-nudge.sh` | `PreToolUse` (Write only, filtered to `--code-root`) | fires only when the write target does not exist yet and its extension is wired for this project; injects one reminder to search the code index first, plus the search fallback (see below) appended to that same reminder. Separately (newlang-nudge): when the extension is one the engine supports but this project never wired, injects a structured, numbered decision point (wire it / decline it permanently via `--never-ext` / not now) naming the language -- deduped once per language per session. The log-only DETECTION outcome (`outcome=language-available-not-wired`) still fires on every occurrence regardless of `--code-root`, unchanged and pre-existing; the VISIBLE decision point is gated on the SAME `--code-root` containment as the wired-file reminder |
| `ledger-post-edit.sh` | `PostToolUse` (every tool; no settings-level matcher) | a bash-only prefilter exits before the watchdog for read-only built-ins (`Read`, `Grep`, ...); `Edit`/`Write`/`MultiEdit`/`NotebookEdit` ledger the tool's own file path (`source: tool`); `Bash` and any tool this hook has no dedicated branch for fall through to a shell-diff (`git status`) tree comparison against a per-root baseline (`source: shell-diff`); an unrecognized or missing `tool_name` additionally logs `outcome=unsupported-mutation-surface` |
| `precompact-persist.sh` | `PreCompact` | persists session state before context is compacted away |
| `sessionstart-remind.sh` | `SessionStart` | on `startup`/`resume`/`clear`, initializes session state (captures the code/store roots' git HEAD, prunes state older than 24h; `clear` resets the session's counters and pending nudges but carries the edit ledger over, `resume` keeps everything), then attempts the standing-decisions digest (see [standing decisions](#standing-decisions)); on `source: compact`, injects what `precompact-persist.sh` left pending, with the digest appended after it |
| `userprompt-remind.sh` | `UserPromptSubmit` | never reads the diff; never reads the prompt text UNLESS the opted-in prompt-query channel is ON for this project (see "Prompt-derived queries" below), in which case it may derive search terms from the prompt, in process, and discard it; fires the coverage, commit, look-back, or prompt-query nudge |
| `sessionend-stamp.sh` | `SessionEnd` | stamps session end into state |
| `code-refresh.sh` | `Stop` and `SessionStart` (projects with a code root) | keeps the code index fresh: runs the existing preflight (`code_index_report`), and on `Stop` the same incremental text heal `code-search` makes (`heal_code_index`, never embeds; see [code-index freshness](#code-index-freshness)). On both events it starts the detached `code-embed-worker` when chunks await a vector (see [code embeddings](#code-embeddings-the-background-worker)). `SessionStart` preflights only and writes the `<project>.code-stale` marker. Silent on every path; one `hook.log` line per run |
| `post-commit-reindex.sh` | store's git `post-commit` | a bounded content-only reindex after every commit; spawns a background embed-worker when vectors are left behind |
| `pre-commit-append-only.sh` | store's git `pre-commit` | runs `memlint.py --against-ref HEAD --staged`; BLOCKS the commit on an append-only violation (a recorded link's body edited, or a link removed, deleted, or renamed — its three lifecycle fields may each move forward once); fails open (lets the commit through) on an unborn HEAD, a missing python, its own cwd not being the store, or any engine failure |
| `memcontinuum-detect.sh` | `SessionStart`, user level | classifies an un-initialized repo and emits its ask into the assistant's context once; no python, no watchdog, no logging by default |

**Fail-open is the contract, not a fallback — with one deliberate exception.**
No hook may block an edit or a commit — not on a missing python, not on a
stale index, not on a lookup error, not on its own timeout. A hook that cannot
do its job logs and exits 0. The reason is asymmetric cost: a missed reminder
costs one un-recorded ruling; a hook that blocks an edit costs the user their
tool, and the first thing anyone does with a tool that blocks edits is remove
it. Failing open also names its reason: a degraded answer carries
`reason_code`, `exception_type` and a safe message; the traceback goes to
`memidx-debug.log`; `--debug` re-raises. `pre-commit-append-only.sh` is the one
exception, and only for the one failure mode it exists to catch: an edit to a
recorded link's body (its three lifecycle fields are exempt — see the
append-only table below). The asymmetry inverts there — the guard exists to
stop a rewrite from ever becoming this store's own committed history in the
first place; once one gets through, the superseded content is still sitting
in git's own history (an earlier commit, recoverable the same way any commit
is), but nothing else guarantees a later reader ever compares against that
earlier commit rather than trusting the rewritten one as current. A refused
commit, by contrast, costs one message and a `git commit --no-verify` away.
Every OTHER way this hook can
fail (no python, an unborn HEAD, memlint erroring out, its own cwd not being
the store) still fails open exactly
like every other hook.

**Search fallback.** `memidx.py search` was the only channel that could
deliver a decision NOT bound to the file being edited, and it had never
once been invoked in this project's recorded history. So the hook calls
it: on `pre-edit-chain.sh`'s own genuine-miss branch (never on a match,
never on `worktree-unwired`/`worktree-unresolved`, both of which already
end the run first), and on `newfile-nudge.sh`'s wired-new-file reminder,
`memidx.py search --read-only --hydrate` is queried on the edited path's
OWN words, and the nearest decisions (if any) are handed to the agent
labelled as a guess, never as a match. Both hooks skip this entirely for a
path under `$MEMCONTINUUM_ROOT` (the store itself — that is
duplicate-detection's job, out of scope here).

The query: the path is relativized first. Both hooks apply the same
worktree remap before building the query (`mc_remap_worktree_gated` in
`hooks/mc-path-lib.sh`), so the `.worktrees/x/...` segment of a wired
worktree path is not spelled into it. Two cases keep extra words: an
unwired worktree parked inside the code root is deliberately not remapped,
so its path words remain; and when the session cwd is a proper ancestor of
the code root, the query is relative to that cwd and keeps the repo
directory name. `newfile-nudge.sh` builds its query after that remap too,
and strips its configured code root (`MEMCONTINUUM_CODE_ROOT`, plus any
`MEMCONTINUUM_STRIP_PREFIX`) rather than relying on the session cwd, which
need not be inside the code root. The colon-separated strip-prefix list is
split without pathname expansion, so a root containing `*`, `?` or `[` stays
literal. Then its extension is stripped, `\` is normalized as a path
separator alongside `/ _ - .`, and the result is split into words on
those separators and on camelCase boundaries, lowercased, deduped, and
filtered: tokens shorter than 3 characters and a fixed generic-stem list
(`src lib sources source docs doc test tests spec main index utils util
helpers internal app core common base readme package makefile config init
setup`) are dropped, and the result is capped at 12 tokens
(`hooks/mc-query-lib.sh`, side-effect-free, its own header states these
limits — including the tokenizer's own ASCII-only limitation: a path
whose meaningful words are non-ASCII degrades to "no query" or a
partial/garbled one, a stated limit, not a bug this feature fixes). An
empty query after filtering never runs a search.

The search itself is ONE `memidx.py search --read-only [--root <store>]
--limit 2 --json --hydrate` call. `--read-only` (new) opens the index
strictly read-only (`open_db_readonly`, mode=ro) and never migrates a
schema behind the current generation to answer a guess — a schema generation gap refuses by name
(`state=upgrade-required reason=index-needs-migration`) instead of the
ordinary rw path's silent migrate-on-open. `--root` (new) makes on-disk
staleness visible instead of silently swallowed, same warning `for-path`
already surfaces on the match path. It is passed only when
`MEMCONTINUUM_ROOT` is set: the pre-edit and prompt-query channels' rendered
wiring exports it, but the rendered new-file command exports neither
`MEMCONTINUUM_ROOT` nor `MEMCONTINUUM_STRIP_PREFIX`, so the new-file channel
runs without `--root` and staleness is not reported there. `--hydrate` adds a `chain_text` field
to each JSON hit, reusing `topic_chain_lines_capped` (the same rendering
`chain` produces, capped — see below), so no second `chain`/`for-path`
call per hit is ever needed. `--decisions-only` restricts results to
decision records — index types `topic`, `link` (a topic's per-link rows),
`incident` and `investigation` — so the store README, `sources/` records,
inbox drops and concepts are never offered as a guess; a plain `search`
still returns them, and an explicit `--type` narrows within the decision set
rather than widening it. The prompt-query channel passes the same flag. No
`--status`/`--authority` filter (the engine's own defaults apply); mode is `MEMCONTINUUM_FALLBACK_MODE`
(`hybrid`/`vector`/`fts`) when set, else the engine's own default
(`hybrid`) — an unrecognized value falls back to that same default. A
drvfs-backed store (SQLite WAL locking is unreliable there — see this
repo's own Windows/WSL environment notes) is a candidate for
`MEMCONTINUUM_FALLBACK_MODE=fts`, which never touches the embedding
backend at all; this is a per-installation recommendation only — the
shipped default stays `hybrid` — ship on the engine's own default,
measure real queries before changing it.

Second-hit score floor (`MEMCONTINUUM_FALLBACK_SECOND_FLOOR`):
the file-miss channels (`pre-edit-chain.sh`, `newfile-nudge.sh`) ask for two
hits, but the first always shows and the second shows only if its fused
hybrid (RRF) `score` is at least the floor — default `0.0314`, a
**provisional** value measured on a 346-case replay (unrelated guesses
~32% → ~23%, cases with at least one relevant guess ~90% → ~86%) and due a
re-check on fresh data. The env var takes a non-negative decimal; `0`
disables the floor; an unparseable or negative value falls back to the
default (never fails the hook). The floor is an RRF value, so it applies
**only when the search `--json` envelope proves RRF scores**: a dict with
`fusion: "rrf"` and no `embedding` / `dimension_mismatch_rows` key (those
mean the vector channel was missing or partial, so the scores may be
single-channel or unevenly fused, and the floor stays off rather than
guess). A run where one channel simply returned nothing still reports
`fusion: "rrf"` with single-channel scores (at most 1/61), so at the default
floor its second hit is always hidden; with `0` or a floor of 0.0161 or less
it is shown. A bare list (`fts`/`vector` mode), `fusion:
"vector-only"` (cosine) or any degraded envelope shows hits exactly as
before. A missing or non-numeric score counts as below the floor; the first
hit is never dropped. A dropped hit is logged as `floor_dropped=<id>`
(`fb_floor_dropped=<id>` on the new-file nudge's outcome line) and is not
written to session state, so it can still surface later. The prompt-query
channel (max 1 hit) does not use the floor. The default and the env read live
once, in `hooks/mc-fallback-lib.sh` (`mc_fallback_second_floor`).

Each hit's `chain_text` is capped at `MEMCONTINUUM_FALLBACK_MAX_BYTES`
(default 2000 UTF-8 bytes, `--hydrate-max-bytes`): the active/current link
renders in full, older links are added newest-first until the cap, then
one explicit `[... N older link(s) omitted]` marker replaces the rest — a
topic that fits under the cap renders byte-identical to the uncapped
`chain` output; one that doesn't (measured: a 16-link topic exceeded 10KB
uncapped) never grows past the cap. On a hit, `additionalContext` gets a
fixed first line, verbatim:

    No recorded decision binds this file. Nearest by search -- may be unrelated:

followed by each hit's `chain_text` — never the `snippet` field. Two
outcomes, logged instead of (not alongside) the plain miss this branch
used to log: `search-fallback` (≥1 hit; `hits=N ids=<comma ids> mode=<m>
q=<+-joined tokens> fb_ms=<N>`) and `search-fallback-empty`
(`reason=no-query` / `reason=no-hits` / `reason=store-root` /
`reason=missing` / `reason=uninitialized` / `reason=index-needs-migration`
/ `reason=quarantined` / `reason=stale` / `reason=search-failed rc=N` /
`reason=bad-json` / `reason=lib-missing` — `hooks/mc-fallback-lib.sh`
itself failed to source, plus `fb_ms=<N>` whenever the search subprocess
actually ran). `fb_ms` is millisecond-resolution wall time of the search
subprocess call only (`date +%s%3N`, a python `time.time()` fallback where
`%3N` isn't supported) — the same field name on both hooks, present only
when the subprocess ran, so `no-query`/`store-root` never contribute a
sample. The query is `+`-joined, not quoted-with-spaces: `memidx.py
stats`'s own generic `key=value` field scan has no quote-awareness
(`\S*`), so a quoted, space-containing value would truncate at the first
space. `newfile-nudge.sh`'s own outcome name for this branch never changes
(`outcome=nudged` — `memidx.py stats`' `nf.nudged` metric already keys on
that literal string); the fallback's own result rides along as extra
`fb_outcome=`/`fb_hits=`/`fb_ids=`/`fb_mode=`/`fb_q=`/`fb_reason=`/`fb_ms=`/
`fb_state=` fields on that same line instead (`fb_state=skipped-lock` /
`fb_state=lib-missing` — see the titling paragraph below; absent when
titling wrote normally or never applied). The 2s watchdog is unchanged and
covers the fallback's own run too (real hybrid-mode measurements:
0.91–0.96s end to end, well inside budget — see `pre-edit-chain.sh`'s own
header for the watchdog-budget accounting); on a watchdog timeout the
existing `MC_WATCHDOG_TIMEOUT_FALLBACK` line is what the model gets, same
as any other timeout — UNLESS the search subprocess had already started
when the kill landed: both hooks write a `.fb-started.<pid>` marker for
that call's own duration only, and `mc-watchdog.sh`'s own kill handler
checks for it, appending `fb_started=1` to its `outcome=watchdog-killed`
line when present (tallied as `memidx.py stats`' `fallback.killed` count
— see below). The fallback subprocess's own stderr is routed to
`/dev/null`, never appended to hook.log (a stray, untimestamped line would
otherwise break the "exactly one line per invocation" contract).

**No write path (scope: the fallback search itself).** The fallback's own
`search --read-only` call never touches the store or the index beyond
reading it — `git status --short` on a fixture store stays clean and the
index file's sha256 is unchanged across repeated fallback-only runs (proven
even against a schema behind the current generation, which the ordinary rw
path WOULD migrate on open — see `--read-only` above). This is narrower
than "the whole hook run never writes the index" — on a MISS, the same
hook run's own EARLIER `for-path` lookup (unchanged
by this feature, and correctly so: a match must keep seeing a migrated,
current schema) still opens the index the ordinary rw way and can migrate/
stamp it exactly as it always has, before the fallback ever gets a turn.
The one state write EITHER
hook makes is titling: on a hit, each surfaced (id, title) pair is
appended to the current session's own state file (the same
`sessions/<project>/<session>.json`, `mc_update_state_json`, every
write-side hook already shares) under a `search_fallbacks` list, deduped
by (file, id) with the position refreshed on a repeat (never a growing run
of near-duplicates for one repeated miss) and each title truncated to 120
characters, capped at the last 20 entries — so `userprompt-remind.sh`'s
look-back nudge can name what was shown, reading only that state (never
hook.log, never the prompt — this look-back read itself never reads the
prompt, unaffected by the prompt-query channel's own amendment to ruling B,
see "Prompt-derived queries" below) and never
re-searching: each entry renders as `search surfaced <title> (<id>) for
<file>`, capped at 8 (the same cap coverage's own `unmapped[:8]` fact line
uses) — `pre-edit-chain.sh`'s own hook.log lines carry no `session=` field
at all (`finish()`'s own fixed field list has none), so this state write is
the ONLY place a title is recoverable per session; the write itself is
sourced and paid for lazily, only on this already-rare branch, THROUGH A
SHORT (0.25s, not the shared `mc_update_state_json`'s own 2.0s default)
lock deadline of its own — this write runs after the search subprocess
has already spent most of the SAME 2s watchdog budget, and a lock held
for the whole run must never be allowed to blow that budget and lose the
already-computed additionalContext guess to a watchdog kill. The write
can therefore now be SKIPPED (`fb_state=skipped-lock` on the outcome
line, above) rather than always landing — the guess itself is unaffected
either way, only its later look-back mention is lost. `hooks/mc-fallback-
lib.sh` failing to source at all degrades the same way (`fb_state=lib-
missing`). This is titling metadata, not a decision: the hook never binds
anything, it only remembers what it already showed.

**No relevance floor, by design.** This channel ships on the search engine's
existing ranking as-is (hybrid default) with no minimum-score cutoff and no
change to how `search` ranks anything — weeks of real queries are meant to
decide that floor (and whether hybrid stays the default mode) later, not
this feature.

**Prompt-derived queries.** A second, unrelated search channel on
`userprompt-remind.sh` itself: a second source of decisions the model
never asked to see, this time derived from the user's own prompt rather
than the file being edited. Off by default, one project at a time — the
ruling calls for weeks of measurement on this project before any other
project turns it on.

The switch: `MEMCONTINUUM_PROMPT_QUERY=1` forces it ON; `=0` forces it OFF
and wins over everything else; otherwise it is ON only when
`$MEMCONTINUUM_HOME/prompt-query.projects` exists and contains a line
equal to this project's name (`grep -qxF`, no python — literal match, not
a regex, so a project name containing a metacharacter such as `.` cannot
match a different line). Decided before the
payload is even read, so an OFF project's `mc_extract_fields` call stays
byte-identical to the pre-L2 shape — it never requests the derived-terms
field at all.

What is read and what is never written: when ON, `hooks/memlib.sh`'s
`mc_extract_fields` reads `payload["prompt"]` (or `payload["user_input"]`
as a fallback — real payloads on this machine carry `prompt`; see the
payload-shape capture above), tokenizes it in that one process, and
returns ONLY the resulting terms — the raw text itself never leaves that
process, is never placed in an env var, argv, a file, session state, or
`hook.log`. The tokenizer is the same content-term vocabulary `memidx.py`
itself uses (`mc_text.py`: lowercase, `[a-z0-9]+` tokens, stopwords
dropped), with four query-specific rules on top: a second, small list of
conversational filler is also dropped (`mc_text.py`'s own
`_PROMPT_FILLER` — `please`, `explain`, `want`, `need`, `help`, `tell`,
`show`, `thanks`, and the like; no domain words), tokens under 3
characters are dropped, the result is deduped (first occurrence wins) and
capped at 12 terms, and — the four-content-word gate — fewer than 4
surviving terms means the channel treats the turn as if it had found
nothing (`reason=too-few-terms`), never searching at all. `_PROMPT_FILLER`
is an ACCEPTED, TUNABLE filter, applied ONLY to this channel's own term
derivation — never to `_content_terms` (memidx's own coverage scoring),
which stays byte-identical. Without it, an owner-shaped prompt like
"please explain me the level of importance" survives the stopword list
alone as four terms ("please explain level importance") and opens the
gate; since `memidx.fts_escape` ORs every term, one leftover word that
happens to occur anywhere in the store becomes a hit on its own —
collecting OR-hits on boilerplate mostly measures the tokenizer, which is
exactly what the ruling's weeks of measurement are meant to decide
instead. This is a term-side filter only: it has no effect on FTS
ranking, on what a document itself contains, or on `_content_terms`.
Accepted privacy
limit: a term is a prompt word after this split, so a secret pasted into
an opted-in project's prompt can leave a fragment in `q=` (an `sk-abc123`
key logs as `abc123`) — the ruling accepts this; it is what makes the
per-project opt-in an INFORMED one, not an incidental one.

The search itself: FTS-only, never hybrid or vector, regardless of
`MEMCONTINUUM_FALLBACK_MODE` — a prompt's own words are typically several
short, unrelated tokens (nothing like a code path's own vocabulary), and
FTS is what returns nothing rather than a plausible-looking wrong guess
when they don't hit; it also costs milliseconds, not the hundreds of
milliseconds an embedding call needs. `memidx.py search --mode fts
--limit 3 --json --hydrate --read-only` (plus `--root` when
`MEMCONTINUUM_ROOT` is configured), mirroring the search-fallback call
above in every other respect. At most one topic per prompt: the search
asks for up to 3 hits so the exclusion filter below has room to skip past
an already-surfaced one, but only the FIRST surviving hit is ever shown.
A redelivered prompt (a RETRY — the same `prompt_id` resubmitted while
`delivery_open` is still true, e.g. after a killed run) never searches at
all: the guess a prior run may have already computed can be lost, but one
prompt is never allowed to yield two topics.

Per-session dedup: a topic already surfaced this session — by a search
fallback (`search_fallbacks`, written by `pre-edit-chain.sh`,
`newfile-nudge.sh`, AND this channel itself) OR by the session-start
standing digest (`standing_ids`, written by `sessionstart-remind.sh` —
see "Standing decisions" below) — is excluded (`mc_fallback_parse`'s own
`EXCLUDE_IDS` argument, the union of both lists' ids). All matching hits
excluded reads as `reason=already-surfaced`, never as a plain miss.
`search_fallbacks` is capped at its last 20 entries (L1's own cap,
unchanged) — on a very long session a topic can therefore age out and be
legitimately surfaced again; this is a stated limit, not a bug.

**Machine-delivered messages.** Claude Code delivers machine
text — subagent hand-back reports, background-task notifications, and
harness-injected system framing — through this SAME `UserPromptSubmit` event
a typed prompt arrives on; a cross-session message arrives the same way. The
payload carries no field that distinguishes these from a typed prompt
(reading `transcript_path` is forbidden, ruling B), so `hooks/memlib.sh`'s
`mc_extract_fields` — the ONE process that reads the prompt — recognises
them by their framing prefix instead: after `lstrip()`, the text is
machine-framed when it starts with `<task-notification>`,
`<cross-session-message`, `<agent-message`, `Another Claude session sent a
message` (with or without a trailing colon, so the "... while you were
working:" variant matches too), `A peer session sent a message`,
`<system-reminder>`, or `[SYSTEM NOTIFICATION` (a PREFIX match, never a
substring one, so a human prompt that merely mentions one of these marker
strings mid-sentence still searches). The marker list is one tuple
constant, `mc_text._MACHINE_PROMPT_PREFIXES`, documented next to
`_PROMPT_FILLER`. A machine-framed prompt is never tokenized and never
searched — the same one-process, prompt-text-never-leaves-the-process
discipline as the rest of this channel — and logs
`reason=non-user`, `q=` empty, with no search subprocess run (no `pq_ms=`).
This is a stated limit, not a guarantee: if Claude Code ever rewords its own
framing, the skip silently stops matching — visible in hook.log as
`reason=non-user` falling to zero while machine-looking terms (hex ids,
`toolu`) reappear in `q=`.

The label, verbatim: `Nearest recorded decision by search on this
prompt's words -- may be unrelated:` — same shape as the search-fallback
label (a guess, never a match), reworded because a prompt has no file to
name. Both labels are constants in `hooks/mc-fallback-lib.sh`
(`MC_FB_LABEL_FILE` / `MC_FB_LABEL_PROMPT`).

Delivery: the guess merges into whichever of this hook's three existing
`additionalContext` envelopes (coverage, nudge-only, look-back) this turn
would already emit — existing content first, the guess last. On a turn
that would otherwise print nothing at all, the guess goes out standalone,
under its own outcome token, `outcome=prompt-query-only` (parallel to
`nudge-only` — a real, terminal turn outcome, not folded under whatever
silent reason the turn would otherwise have logged); this closes
`delivery_open` the same way any other completed turn does, but stamps
nothing else (not `last_inject_turn`, `last_inject_time`, `last_inject_ts`,
not `lookback_count` — a guess is not a nudge).

Logging: one extra `mc_log` line, written right after the query, on every
turn the channel is ON and the prompt parsed (never emitted when OFF —
this is the "the prompt was never read" proof for every other project):

    userprompt outcome=prompt-query hits=1 ids=<id> q=<terms> pq_ms=<n> session=<id>
    userprompt outcome=prompt-query-empty reason=<reason> q=<terms or empty> pq_ms=<n> session=<id>

`reason` is one of `too-few-terms` / `no-hits` / `already-surfaced` /
`store-root-unset` / `lib-missing` / `search-failed rc=N` / `bad-json` /
`retry` (a redelivered prompt_id, delivery_open still open — see above;
the search never ran) / `non-user` (a machine-framed message — an agent
hand-back report, a task notification, a cross-session message — see
above; the search never ran) / an index state name. `q=` is `+`-joined, not space-joined — the same
reason the search-fallback query above is (`memidx.py stats`' generic
`key=value` field scan has no quote-awareness, so a space-containing value
would truncate at the first space); this is the one place a term is
written anywhere, and only individually, never as the prompt's own word
order. `pq_ms=` (millisecond-resolution, `mc_now_ms`) is present only when
the search subprocess actually ran — a `too-few-terms`/`store-root-unset`
turn never contributes a sample, same discipline `fb_ms=` already follows.
This line is SUPPLEMENTAL, the same way `commit-nudge` is: it rides
alongside the turn's own real outcome line, never replaces it (except on
a standalone turn, where `prompt-query-only` IS that turn's own outcome).
`memidx.py stats` tallies it into its own `prompt_query` block — counts,
reasons, `pq_ms` p50/p95 — separate from the unrelated `fallback` block;
`prompt-query`/`prompt-query-empty` are excluded from `user_prompts` (they
are supplemental), `prompt-query-only` is not (it is a real turn).

Budget: this channel runs inside the SAME 2s watchdog every other
`userprompt-remind.sh` step shares. Measured on this repo's own test
fixture (ext4, fts mode, a real hit): ~0.29s end to end for the whole
hook run, five-sample average — comfortably inside budget.

**Post-commit re-lint.** `git commit --no-verify` skips `pre-commit`, not
`post-commit`. After the reindex pass, `post-commit-reindex.sh`
re-runs the two `pre-commit-append-only.sh` checks through the shared
`hooks/mc-store-lint.sh` (`mc_store_lint_schema`, `mc_store_lint_append_only`;
sourced, bash 3.2 safe, they set result variables and never exit), with the
same marker-trust rule: a verdict counts only with memlint's own summary line
(`memlint: N error(s)` / `changed=`); an engine crash is
`relint=skipped:engine-failure`. The only difference is the ref: `HEAD` before
the commit, `HEAD~1` after it (the index equals `HEAD` for the committed
paths). A root commit gets the schema pass only. A finding prints a banner on
stderr, logs `relint=failed:<schema|append-only|both>`, and writes the sticky
`$MEMCONTINUUM_HOME/<project>.relint-failed` marker that `sessionstart-remind.sh`
surfaces as one line at startup/resume/clear. The marker's first line is
`root=<physical store root>`: only a clean run of the same root removes it
(otherwise `relint=ok marker=foreign`), and an engine crash never does. The
marker is keyed by project name only, so a renamed project leaves its marker
under the old name (no migration; the nag stops when the file is deleted).
Without a resolved watchdog python (the script fell through unguarded) the
re-lint and backup log `skipped:no-watchdog` and do nothing. It can
not change the exit code, and its cost is the two lints (~0.8s on a 150-file
store). The pre-commit hook's working-tree-versus-index gap is inherited.

**Store backup bundle.** The same guarded child, after the re-lint, writes a
`git bundle create --all` into `$MEMCONTINUUM_BACKUP_DIR/<project>/` when that
directory is configured (environment, then `config.sh`, resolved by the
config.sh sourcing `mc-watchdog.sh` already does; the installed wrapper is
deliberately not extended, because `mc_installer_wrapper_shape` requires
exactly five lines). The directory must pre-exist; the bundle is written to
`<name>.part.<pid>` (unique per run) and the exclusive open IS the write
(`rm -f`, then `set -C` on the very redirection git's stdout uses: a planted
symlink is refused, not followed; stdout form `git bundle create -`, so no
`.lock`). Grammar-checked leftovers (`.part.<digits>`, old `.part`, old
`.part.lock`) older than twice the budget (default 30 s, so 60 s; the budget is
normalised by `mc_backup_keep_normalize`, bad value -> 30) are swept, never by
name order. The part is verified, and
renamed through `mc_safe_replace` (refuses a symlink or directory destination:
`failed:dest`; the re-lint marker uses a unique `.new.$$` temp and the same
rename; `marker=write-failed`); the manifest line and the newest-N prune (by name sort, manual
bundles with the same pattern included) follow a successful rename only.
Failures are `backup=failed:<reason>` and never touch the exit code. Cost is
about 0.5s for the bundle on a real store, inside the existing 30s budget,
which is unchanged. `memcontinuum-update.sh` reports it: the machine line
(`backup: <dir> (keep N)` / `backup: off`) and a per-store `backup` column
(`mc_backup_state` in `scripts/mc-registry-lib.sh`: off, none, current,
behind, unreadable, and `unknown` on a row that records no project). The
KEEP rule is one function, `mc_backup_keep_normalize`, shared by the hook,
setup and the updater; the bundle name is defined once by `mc_backup_name_ok`
(exact `<project>-YYYYMMDD-HHMM.bundle`) and `mc_backup_bundle_list` fills an
array from it (never a newline-joined string, so a path is never split). The
project name and backup dir are validated once, before any path is built
(`relint=skipped:bad-project backup=skipped:bad-project` / `skipped:bad-dir`);
a symlinked project folder is `backup=failed:bad-project`. A post-commit
re-lint of an amended ROOT commit is schema-checked only (no `HEAD~1`);
amending a store's root commit is not a supported case; for `--amend`
post-commit compares against the amended commit's parent, so a link added by
the amended commit and rewritten in the amend reads as an add (pre-commit
compared against the replaced commit), while links present in the parent are
still caught; `repo-init.sh`
refuses `.` and `..` as `--project`. `--apply` never creates a bundle.

**Logging, per hook.** The eight project-level hooks each write exactly one
`outcome=` line per run to `$MEMCONTINUUM_HOME/hook.log`.
Diagnostic lines may precede it (`pre-edit-chain.sh` logs a missing-python note
before its own `outcome=`). A watchdog kill is included in "every run": the
guarded hook cannot write its own outcome line then — it may be mid-call, or may
never have reached that code — so `mc-watchdog.sh` writes
`outcome=watchdog-killed hook=<name>` itself before exiting. The three hooks
outside that rule are deliberate: `post-commit-reindex.sh` writes its own
`post-commit-reindex: rc=… elapsed=… project=… root=… embed=pending|clean|skipped relint=… backup=…`
line instead, `pre-commit-append-only.sh` writes its own line, one of
`pre-commit-append-only: rc=1 changed=<n> project=…` (refused the commit),
`rc=0 changed=<n> project=…` (clean), or `skipped=<reason> project=…` — an
unborn HEAD, no python, an engine failure, or (fix wave 1 G1) the hook
running with its cwd outside `MEMCONTINUUM_ROOT` (`skipped=not-the-store` —
a wrapper invoked from another repository entirely, e.g. through a
mistakenly shared `core.hooksPath`, must never judge or block THAT
repository's commit) — when nothing was actually judged (no `changed=` on
that shape: nothing was compared). `memidx.py stats` reports these under a
`pre_commit` block (`pass`/`refused`/`skipped`, the last summing every
`skipped:<reason>` outcome its own `outcomes` dict names), exactly like
every other hook kind — these lines carry no `outcome=` field of their own
and are never folded into `user_prompts`.
`code-refresh.sh` writes
`code-refresh outcome=<refreshed|skip|failed> reason=… project=… roots=… unrecorded=… changed=… removed=… healed=… dropped=… elapsed=…s event=<Stop|SessionStart> session=… embed=<spawned|clean|off [reason=no-wired-roots]|failed reason=<marker|log|spawn>|unknown>`
(plus `marker=write-failed` and `detail=…` when they apply); the line is produced by
`memidx.py code-refresh` and logged by the hook, and `memidx.py stats` files it under `other`.
`memcontinuum-detect.sh` writes nothing at all unless
`$MEMCONTINUUM_DETECT_LOG` is set — it runs in every repo on the machine, so its
default is silence.

**pre-edit-chain.sh's `topics=` field.** A `matched` or `index-stale-served`
line also names which topic ids were actually injected, not just that
something matched: `outcome=matched elapsed=1s topics=TOP-nnnn,TOP-nnnn
project=P file=F` — sorted, comma-separated, sitting between `elapsed=` and
`project=` so `project=`/`file=` keep their existing trailing position (the
one thing `memidx.py stats`'s own line parser structurally relies on). This
exists so a later grading pass can check WHICH decisions a matched edit
actually saw, turning a matched edit into a gradeable sample: file, decisions
shown, and a later judgement of whether they were the right ones. Capped at
10 ids: beyond that, a trailing `+N` names how many more were left out, so a
pathological file governed by dozens of topics can never blow up a single
hook.log line. The field is omitted entirely — never an empty `topics=`
token — on every outcome besides a real match (no-match, index-missing,
query-failed, …) and on a matched concept with no governing topics; a line
written before this field existed simply has none, and still parses.
`memidx.py stats` reports this under the `pre_edit` block: `topics_named`
(how many matched runs named at least one topic), `topics_distinct` (how
many distinct topic ids were ever injected in the window), and `top_topics`
(the top 5 by injection count, ties broken by id) — a `+N` cap marker is
never counted as a topic.

**hook.log rotation.** Nothing truncates or prunes hook.log on its own —
`mc_prune_old_state` only ever clears session-state JSON — so an
unrotated hook.log grows without bound (~177 KB/day on a real machine),
with every `memidx.py stats` run reading the whole file cold.
`sessionstart-remind.sh` rotates it once per session, at the
startup/resume/clear session-init boundary only — never from `mc_log`'s own
append path, or from `pre-edit-chain.sh`'s independent logger, both of which
are hot paths that must not gain a `stat()` call for this, and never
re-checked on a mid-session `compact`. Once hook.log exceeds
`MEMCONTINUUM_LOG_MAX_BYTES` (default 5242880 bytes, 5 MiB), it is rotated:
the chain `hook.log.1` (newest) .. `hook.log.$MEMCONTINUUM_LOG_KEEP` (oldest)
shifts up by one (`.N` → `.N+1`, the oldest file dropped), then hook.log's
current content becomes the new `hook.log.1` and a fresh, empty hook.log
starts. `MEMCONTINUUM_LOG_KEEP` (default 12) bounds how many rotated files
are ever kept — no file beyond `.$MEMCONTINUUM_LOG_KEEP`, no dated archive,
no compression, and data older than the oldest retained file is gone by
design once retention fills up. Lowering `MEMCONTINUUM_LOG_KEEP` takes
effect on the very next rotation: the contiguous run of files beyond the
new, smaller bound is deleted outright, not merely left unreferenced. A
rotation interrupted by `SessionStart`'s own 2s watchdog — after hook.log
has been claimed but before it lands at `.1` — is recovered on the next
rotation (oldest recovered claim first, then the newest), never left a
permanent, invisible orphan. `MEMCONTINUUM_LOG_KEEP=1` keeps only
`hook.log.1`, always replaced in place, never a `.2`.

Sizing: a real store's hook.log grew ~5 MB in 21 days (~240 KB/day), so
the default `12 × 5 MiB` bound gives roughly 8-9 months of retained
history at that rate — a bounded, documented allowance, not unlimited
archival. `memidx.py stats` reads every `hook.log.N` it finds (oldest
first) alongside hook.log, so a `--days N` window spanning one or more
rotations still sees everything still retained. Fail-open like every
other path here: a missing/unwritable `$MEMCONTINUUM_HOME`, an unreadable
size, or a concurrent session racing the same rotation all leave the log
alone and never block `SessionStart`.

`userprompt-remind.sh` has its own exception too (Codex 12, fix wave 1 G4): a
turn whose HEAD-moved check (independent of coverage candidacy — see "The
commit nudge" below) finds one or more newly-examined, undecided commits
writes one SUPPLEMENTAL `userprompt outcome=commit-nudge sha=<n> root=<r>
session=<s>` line per such commit, in addition to — never instead of — that
same turn's own real outcome line (`injected`, `no-evidence`, ...). `stats`
excludes `commit-nudge` from its `user_prompts` tally for exactly this
reason: it is not a second prompt, and counting it as one used to inflate a
handful of real turns into double their number.

`ledger-post-edit.sh` itself has two further exceptions to "exactly one
`outcome=` line". A read-only built-in (`Read`, `Grep`, ...) is caught by the
prefilter before the watchdog and writes nothing at all — no line, no
process spawned. The shell-diff branch (`Bash`, or any tool with no
dedicated branch) can write several lines in one run: one
`outcome=appended kind=<code|store> source=shell-diff` per path the tree
diff found changed, always followed by exactly one summary line,
`outcome=shell-diff appended=N roots=R timeouts=T non-git=G
baseline-too-large=L`, so a call that touched zero paths still counts as one
line (and `stats` counts calls, not paths, from that summary line alone).
An unrecognized or missing `tool_name` adds one more line ahead of the
shell-diff pass, `outcome=unsupported-mutation-surface tool=<name>`.

**Writable surface.** The write-side hooks may write
`$MEMCONTINUUM_HOME/sessions/<project>/` and `hook.log`, and nothing else —
never the store, never the code root. `ledger-post-edit.sh`'s shell-diff
branch runs `git status --porcelain -z` (via `git --no-optional-locks`) in
every configured code root and the store root on a `Bash` (or unrecognized-
tool) invocation — that call is read-only end to end, so it does not widen
this surface: a `git status`/`git diff` run in that root afterwards reports
exactly what it would have before (the regression test for this claim is
`test_shell_diff_git_status_calls_leave_the_tree_exactly_as_found`). Two
hooks reach the decision index's own SQLite cache as well, and only that:
`userprompt-remind.sh`'s coverage check
calls `memidx.py unmapped`, which self-heals a drifted index with a
`reindex --no-embed --auto`, and `precompact-persist.sh` runs the same
self-healing `unmapped` call for ledger entries under the code root — or a
plain `reindex --no-embed --auto` when the session only touched the store.
`--auto` keeps the self-heal mode-preserving: it never embeds inside a hook's
time budget, and never lets a no-op heal pass claim a fuller `embedding_mode`
than the index already had (see [Decision index provenance](#decision-index-provenance-and-embedding-lifecycle)).
`post-commit-reindex.sh` (the store's git `post-commit`, not one of the
five write-side hooks above) writes the decision index directly (that is its
whole job) plus, when its content pass leaves rows without a fresh vector,
`<project>.embed-pending` (a marker), `<project>.embed.lock`
(an `fcntl.flock` target for the embed-worker it spawns) and
`<project>.embed.log` (the worker's own stdout/stderr, never the shared
`hook.log`). These three, and `memidx-debug.log` (a timestamped traceback
`memidx.py` appends whenever a command degrades on an internal error), all
land beside the database the command in question is serving
(`Path(db_path).parent`) rather than at a fixed `$MEMCONTINUUM_HOME` path —
a custom `--db` moves them with it. Only when no database is in scope at all
(`backend-preflight`) does `memidx-debug.log` fall back to
`$MEMCONTINUUM_HOME`. Every one of these files is created only when its
directory already exists (none of them is what creates that directory out of
nowhere), and none is ever surfaced to stdout/stderr/JSON.

**Session state** lives at `$MEMCONTINUUM_HOME/sessions/<project>/<id>.json`,
written by atomic rename (`os.replace`) and guarded by a real
`fcntl.flock(LOCK_EX)` (retried up to 2s) taken inside the state-update helper
in `memlib.sh` — a Python call, never a shelled-out `flock` binary, which macOS
does not ship.

**The commit nudge.** Commit messages name the decision they land under
(a routine, not enforced elsewhere) — `userprompt-remind.sh` nudges once
when a commit does not. State gains two keys: `last_seen_heads` (`{root:
sha}`, seeded at `SessionStart` alongside `start_code_shas` and advanced on
every prompt this check actually runs on) is the per-PROMPT baseline this
compares against, distinct from `start_code_shas`' per-SESSION one;
`nudged_commits` (a list of sha, bounded to the last 20) dedupes a commit
that keeps coming back as HEAD across prompts. `clear` re-seeds both from
the current HEADs, the same way it re-seeds `start_code_shas` — nothing
here survives a clear.

Codex 9 (fix wave 1 G4): the moved-HEAD comparison itself runs on EVERY
prompt, independent of coverage's own candidacy (ledger-growth/cooldown)
gate — only computing the actual nudge (the `unmapped` call, the commit
message read, the fact line) is skipped when NEITHER coverage is a
candidate NOR any root actually moved, so the overwhelmingly common turn
still costs nothing extra. Before this fix, the whole comparison lived
inside coverage's own candidacy gate: once a coverage reminder had fired
once (consuming the ledger-growth signal), a commit with no decision id
followed by any number of further prompts with no NEW ledger growth
produced zero commit nudges at all — `last_seen_heads` never even
advanced, because the comparison never ran.

For every configured root whose HEAD differs from `last_seen_heads[root]`,
the hook reads the new commit's subject and body (one `git log -1`, a 2s
timeout of its own, tighter than the hook's outer watchdog) and, when the
message names no decision id and the SAME `unmapped` call this turn already
made finds at least one file edited under that root during the SESSION
(the ledger — never that specific commit's own diff, which this feature
never reads) with no topic, adds one fact line and logs `userprompt
outcome=commit-nudge sha=<short> root=<root>` (one line per nudge,
`project=` last like every other `mc_log` line). Which of the session's
ledger files count toward that per-root number is decided by `memidx.py
unmapped`'s own `by_path` field — keyed by the exact absolute path the
hook fed it, never a bare relative-path string two sibling code roots
could otherwise share. A root whose message already names a
decision, or whose edited files are all covered, or whose commit was
already nudged, adds no line — but `last_seen_heads` still advances, so
that commit is never re-examined.

On a turn where coverage IS a candidate, the fact line(s) above are folded
into the SAME `additionalContext` block as coverage's own fact line, and
the turn's outcome is `injected` (or `lookback-injected`), same as before
this fix. On a turn where coverage is NOT a candidate but a root moved, the
nudge fact line(s) are the WHOLE `additionalContext` (no coverage fact
line, no store-record question — those are coverage's own content, and
coverage was never asked to fire this turn); the turn's own outcome is
`userprompt outcome=nudge-only`, and coverage's own cooldown/delivery
bookkeeping (`last_injected_pairs`, `last_inject_turn`, `last_inject_time`)
is left untouched — a nudge must never quietly re-arm coverage's own
cooldown clock. The commit nudge itself never reads the diff and never
reads the prompt — that stays true regardless of the prompt-query channel
below, which is a separate, opt-in code path this hook may also run.
`stats` counts `outcome=commit-nudge` as `nudges.commit_nudges`, separate
from `nudges.total`, and excludes it from `user_prompts` (Codex 12: it is
SUPPLEMENTAL to the turn's own real outcome line — `injected`,
`nudge-only`, or otherwise — not a second prompt of its own; five real
prompts each also writing their own commit-nudge line used to report ten
prompts, not five).

**The shell-diff ledger branch.** `ledger-post-edit.sh` runs the tree-diff
pass for `Bash` and any tool it has no dedicated branch for, inside the same
locked python transform `mc_update_state_json` already uses (one subprocess,
one flock, no extra process per root). For every root — each configured code
root, then the store root — it skips a root with no `.git` (counted
`non-git`) and otherwise runs `git --no-optional-locks status --porcelain -z
--untracked-files=all` under a `subprocess.run(..., timeout=...)`, budgeted
by two env vars: `MEMCONTINUUM_SHELL_DIFF_BUDGET` (default 1.2s, the total
wall-clock ceiling for the whole call, tracked with `time.monotonic()`) and
`MEMCONTINUUM_SHELL_DIFF_ROOT_BUDGET` (default 0.8s, the per-root ceiling —
the smaller of the two, or whatever total budget remains, is what each `git`
call actually gets). A timed-out or failing root counts `timeouts` and is
retried on the next call; it never touches that root's baseline. The `-z`
porcelain output is NUL-delimited (`XY<space>PATH\0`); a rename or copy
status (`R`/`C`) is followed by a second NUL-terminated path (the source),
and both are treated as changed. The FIRST successful `git status` seen for
a root only establishes a baseline (a `{path: sha256}` map, `None` instead
when the root has more than 500 dirty paths, counted `baseline-too-large`
and never retried) and appends nothing — a file already dirty before
MemContinuum ever looked is not something a later, unrelated edit gets
credited or blamed for. Every call after that compares the current dirty
set against the baseline and appends a ledger row (`source: shell-diff`,
`kind: code` or `store`) for every path whose content hash changed,
including a deleted path (`content_sha256: ""`). Each appended row logs its
own `outcome=appended kind=<kind> source=shell-diff file=<path>` line, and
every call — even one that appended nothing — ends with exactly one summary
line, `outcome=shell-diff appended=N roots=R timeouts=T non-git=G
baseline-too-large=L`; `memidx.py stats` counts these dynamically under
`ledger_appends.shell_diff_calls`, the same way it already counted
`ledger_appends.code`/`store`. An unrecognized or missing `tool_name` (an
MCP tool this hook has no branch for, or a malformed payload) logs one more
line, `outcome=unsupported-mutation-surface tool=<name-or-"unknown">`,
counted under `ledger_appends.unsupported_surface`, and still runs the same
tree-diff pass — an unrecognized tool that mutated a file is still caught.

This is deliberately best-effort, not strict mutation coverage: nothing here
refuses an undeclared shell mutation or blocks a commit over one. Structured
edits get automatic retrieval — the hook's lookup runs and completes before
the edit reaches disk — but only for the `Edit` and `Write` tools, and only
alongside that same call's own result: the tool call already carries its
final content by the time the hook runs, so what comes back governs the
agent's next move, not the content of the edit that triggered the lookup.
The `PreToolUse` matcher names only `Edit`/`Write`, and the hook reads only
`tool_input.file_path`, so it has no branch that could fire for a notebook
payload. `MultiEdit` and `NotebookEdit` are ledgered after the fact
(`source: "tool"`, same as `Edit`/`Write`) but get no automatic lookup at
all. Shell mutations get best-effort post-detection, after the fact, bounded
by whatever the git-status budget above could see in time.

**The detector is deliberately unlike the others.** It fires on every session
start on the machine, including in repositories that have nothing to do with
this tool, so it must cost near-nothing and depend on nothing: pure bash and
`git`, ~12 ms, no python, no `memlib.sh`, no watchdog, no logging unless
`$MEMCONTINUUM_DETECT_LOG` is set, and it fails open on any error.

**A hook reports a state; only the skill records a decision.** The detector
never installs anything and never writes to `decisions.tsv` — a hook must not
write down a consent it did not collect. It emits its one `additionalContext`
line in exactly one of five states:

| state | decision row? | wiring | behaviour |
|---|---|---|---|
| `not-a-repo` | — | — | silent |
| `opted-out` | — | — | silent (`$MEMCONTINUUM_HOME/no-ask` exists) |
| `decided` | yes (`wired` or `declined`) | any | silent — the recorded answer is authoritative regardless of current wiring |
| `wired-full-no-row` | none | `full` | silent — an install predating the registry reads as already wired |
| `undecided` | none | `partial` or `none` | emits, once |

`partial` wiring emits rather than staying silent: a half-wired repo is the
repair path, and silence there would leave it with no route back to health.
`memcontinuum-state.sh` reports decision and wiring as two separate facts
(`decision=` / `wiring=`, plus a combined `state=` line) because a
hand-edited settings file or an interrupted install can leave them disagreeing.

`wiring=full` means the **five always-wired write-side hooks** are all present
(`ledger-post-edit.sh`, `precompact-persist.sh`, `sessionstart-remind.sh`,
`userprompt-remind.sh`, `sessionend-stamp.sh`). The two PreToolUse hooks are
deliberately excluded from the count: a rationale-only install omits them on
purpose, and their absence must never make a healthy repo read as unwired.

`memcontinuum-decide.sh wired` refuses anything short of `wiring=full` and names
the missing hooks: a `wired` row silences the detector forever, whether or not
the install actually succeeded.

`--repo` is required for `wired`/`declined`/`forget`. Each silences or
unsilences one repo permanently and there is no safe `$PWD` default for a write
like that — a shell sitting in the engine checkout would record the decision
against the engine's key while the repo actually meant stayed undecided forever.
`memcontinuum-state.sh` is read-only and keeps its `$PWD` default.

## Python resolution and `config.sh`

`~/.memcontinuum/config.sh` is sourceable shell rather than JSON on purpose:
every consumer that needs it (`hooks/memlib.sh`, `pre-edit-chain.sh`,
`post-commit-reindex.sh`, and the watchdog guard each write-side hook runs
*before* it sources `memlib.sh`) reads it to find python, and so must not need
an interpreter to do so. Values are single-quoted by `sh_quote` — source the
file, never parse it with `sed`/`tr`.

It carries four values: `MEMCONTINUUM_ENGINE` (this checkout),
`MEMCONTINUUM_PYTHON` (set only when the environment has none),
`MEMCONTINUUM_HOME`, and `MEMCONTINUUM_MACHINE_CLAUDE_DIR` — the user-level
Claude directory `memcontinuum-setup.sh` installed the detector hook and the
`memcontinuum` skill into. That last one exists because setup takes
`--claude-dir` and nothing else on the machine knows what it was given;
`memcontinuum-update.sh --machine` reads it rather than assuming `~/.claude`.

Resolution order, for hooks and for `scripts/repo-init.sh` alike:

    $MEMCONTINUUM_PYTHON → $MEMCONTINUUM_HOME/config.sh → <engine>/.venv/bin/python

`repo-init.sh` ends the chain with a hard error naming `--bootstrap-venv`; hooks
end it by failing open.

The middle step exists because a venv need not live at `<engine>/.venv`. Without
it, every hook line in every project has to carry `MEMCONTINUUM_PYTHON` by hand,
and the one that forgets fails silently behind a log line nobody reads. In
practice it matters mainly for hand-wired hook lines, or ones rendered before
this baking existed: `repo-init.sh` and `memcontinuum-setup.sh` bake
`MEMCONTINUUM_PYTHON` into every hook line they render now.

**The pointer case.** When setup runs with a `MEMCONTINUUM_HOME` other than the
fixed default, it writes the real `config.sh` under that home *and* a minimal
pointer at `~/.memcontinuum/config.sh` recording only the real
`MEMCONTINUUM_HOME`. Every consumer follows through identically: source the
default/env path first, and if that just redefined `MEMCONTINUUM_HOME` to a
different directory, source the real `config.sh` there too. This runs
unconditionally, even when `MEMCONTINUUM_PYTHON` is already baked into the hook
line, because `MEMCONTINUUM_HOME` still has to resolve correctly for session
state and `hook.log` to land under the real home; `config.sh`'s own
`if [ -z "$MEMCONTINUUM_PYTHON" ]` guard preserves env/baked precedence for
python either way. `repo-init.sh` follows the pointer too, but only when its
resolution actually reaches the `config.sh` step — an explicit
`$MEMCONTINUUM_PYTHON` returns before any sourcing, since repo-init needs the
python, not the resolved home.

`memcontinuum-setup.sh --uninstall` removes **both** artifacts, resolving the
same env → pointer → default chain, so an uninstall run with no
`MEMCONTINUUM_HOME` in its own environment still finds the real one rather than
only the pointer.

## Settings merge and identity markers

`scripts/mc_settings_merge.py` is the single settings-merge implementation,
shared by `repo-init.sh` and by `memcontinuum-setup.sh`'s detector-hook merge.

It identifies "its own" hook entries by two things at once: one of the eight
script basenames (`pre-edit-chain.sh`, `newfile-nudge.sh`, `ledger-post-edit.sh`,
`precompact-persist.sh`, `sessionstart-remind.sh`, `userprompt-remind.sh`,
`sessionend-stamp.sh`, `code-refresh.sh`) appearing in a hook item's `command`, **and** a
`MEMCONTINUUM_PROJECT=` marker carried in that command. The marker is what lets
two projects share one `--claude-dir`: an entry naming our scripts but marked
for a different project survives the other project's re-run.

`--project NAME` must match `[A-Za-z0-9._-]+` for exactly this reason — it is
embedded unquoted as that identity marker in every hook command line the merge
step's identity check depends on. The constraint is not merely "no `/`".

An entry naming one of the eight scripts with **no** `MEMCONTINUUM_PROJECT=`
marker at all is treated as wiring from before that marker existed, and stays
sweepable by any project's re-run of a shared `--claude-dir`. Re-running
`repo-init.sh` for a project rewrites that project's entries with the marker.

Merge behaviour: drops only its own items, per item and not per group (a foreign
hook sharing a matcher group with one of ours survives), removes any group left
empty, appends freshly rendered groups, and leaves every other top-level key
(`permissions`, unrelated hooks) untouched. `settings.local.json` is backed up
to `settings.local.json.bak-memcontinuum` before every write that touches an
existing file, and the write itself is a same-directory tmp file plus
`os.replace` with the original mode preserved — never a truncate-in-place.

## repo-init guards

Each of these refuses rather than guesses, with its own exit code, because the
cost of guessing wrong is writing into somebody else's repository. The
complete exit-code list (all sixteen, including the rules-file and skill-copy
foreign-file refusals below) is `repo-init.sh --help`'s — the canonical,
user-facing one; what follows here is the subset whose reasoning needs more
room than a help line.

- **Explicit `--store` with no `--claude-dir` is a hard error** (exit 2). An
  explicit store may legitimately be wired from any cwd — a test harness, a
  script, an unrelated checkout — so the cwd is not a safe signal for where the
  hooks belong, and even a git cwd can be the wrong repo. When `--store` is
  omitted instead, `--claude-dir` defaults from the CHECKOUT the cwd is in
  (that repo's own `.claude`) — never from wherever the defaulted store itself
  ends up landing. Those two happen to coincide when the cwd is not inside any
  git repo (the store then defaults directly into the cwd, and `--claude-dir`
  derives from that same cwd), but deliberately do not for a checkout that is
  a git repo: the store defaults beside it, "`<repo>-MemContinuum-Store`",
  while `--claude-dir` still comes from the repo's own toplevel, not the
  store's parent. This split matters for the WSL rule below — a checkout on a
  Windows-mounted drive defaults its store onto an entirely different disk,
  and the hooks must stay with the repo regardless.
- **The default store lands on the WSL disk, not beside the repo, when the
  checkout itself is Windows-mounted**: a checkout whose
  physical path resolves under `/mnt/<letter>/` while running under WSL (its
  kernel names Microsoft — case-insensitive) walks that store over drvfs/9P
  on every retrieval and ledger call, costing seconds instead of milliseconds.
  `mc_default_store_for` (`scripts/mc-registry-lib.sh`, shared with no other
  caller today — `memcontinuum-decide.sh` only ever records a `--store` it is
  explicitly given, and `hooks/memcontinuum-detect.sh` never proposes a path
  at all) detects this (`mc_is_windows_mounted_checkout`, with
  `MEMCONTINUUM_PROC_VERSION_FILE`/`MEMCONTINUUM_TEST_WSL_MOUNT` test seams so
  the check need not depend on the CI runner actually being WSL) and defaults
  the store instead to `$HOME/dev/<repo>-MemContinuum-Store` when `$HOME/dev`
  is a directory (this machine's convention for where checkouts live), else
  bare `$HOME/<repo>-MemContinuum-Store` — printing one line naming why.
  `--claude-dir` is unaffected (see above); an explicit `--store` always wins
  and skips this rule entirely, on WSL or anywhere else.
- **The WSL-disk name is disambiguated, or refused, rather than silently
  shared, when it already belongs to someone else**: the plain name keys
  only on the checkout's basename, so two different checkouts sharing one
  (`client-a/app`, `client-b/app`) used to collapse onto the identical
  default. `mc_default_store_for` now tries the plain name first, unchanged,
  for the first checkout that ever wants it (never disambiguated
  preemptively); `mc_store_belongs_elsewhere` (same file) then checks
  whether an EXISTING marked store at that path already belongs to a
  different checkout or project before handing out a name a second time,
  three signals in priority order: `decisions.tsv`'s own `store=` field,
  keyed by `mc_repo_key`, when a row exists (authoritative either way, self
  or foreign, since it is the one signal a same-checkout re-run under a
  renamed `--project` cannot fool); otherwise the rendered store
  `README.md`'s own `<!-- memcontinuum-checkout: PATH -->` marker
  (`mc_store_checkout_identity`, stamped at store-creation time with the
  checkout's physical path, never rewritten on a re-run) compared physically
  against this run's checkout — an unambiguous path comparison a shared
  `--project` value cannot fool either, closing the one gap the registry and
  project-name signals alone left open (two checkouts sharing a basename
  AND a `--project` value, neither ever run through `--record-decision`);
  only when NEITHER of those has anything to say (an older store from before
  this marker existed, or a hand-authored README) does the rendered
  `README.md`'s first line (the project name templates/store-README.md.tmpl
  stamps there) compared against this run's `--project` decide it — the
  weakest of the three, since two checkouts can legitimately share a
  `--project` value by coincidence. When the plain name is already someone else's, the checkout's
  own parent directory name disambiguates it instead
  (`<parent>-<repo>-MemContinuum-Store`); when that name is ALSO already
  someone else's, the installer refuses (exit 17) and asks for an explicit
  `--store DIR --claude-dir DIR` rather than guess a third name or adopt a
  foreign store.
- **An existing git repo at `--store` carrying none of this tool's markers is
  refused** (exit 9) — markers being a `topics/`, `incidents/` or `concepts/`
  directory, or a `README.md` mentioning MemContinuum. A mistyped `--store`
  must never seed store directories and a replacement `post-commit` hook into
  an unrelated repo. There is no `--force` carve-out for this one.
- **A store inside another git working tree is refused** (exit 4) unless
  `--force`. The check is by *location*, not by tracked content: it walks up
  from `--store` to the nearest already-existing ancestor and refuses if that
  ancestor is inside any git working tree, whether or not anything at the store
  path is tracked, committed, or even exists yet. It is skipped once `--store`
  is already its own git repo, so `--force` only ever matters the first time.
- **Adopted vs freshly seeded stores are classified before anything is
  written**, and the classification decides how lint findings are treated: on an
  adopted store, `memlint` findings are printed in the verification summary but
  do not fail the install; on a freshly seeded one, any lint error is fatal
  (exit 8). Nothing but this installer's own templates could have put a finding
  in a fresh seed, so one there is an installer bug — while a finding in an
  adopted store reflects content that predates the adopt.
- **The store's `post-commit` is a generated wrapper, not a symlink.** A
  symlinked git hook carries no environment of its own, and
  `post-commit-reindex.sh` silently no-ops without `MEMCONTINUUM_ROOT` set. The
  wrapper exports `MEMCONTINUUM_ROOT`/`_PROJECT`/`_PYTHON` and `exec`s the
  canonical script by absolute path, so it still picks up future edits to that
  script.
- **The install-time `reindex` passes `--no-embed`.** A freshly seeded store
  holds only stub content, and a real embed here would make a first install
  depend on network access (or a warm fastembed cache) it otherwise does not
  need. The store's own `post-commit` runs its own bounded, content-only pass
  (`--no-embed --auto`, under the watchdog) on every commit -- including the
  first real one -- and the first EMBED is the background embed-worker's,
  spawned whenever that content pass leaves rows without a fresh vector (see
  [Decision index provenance](#decision-index-provenance-and-embedding-lifecycle)).

## Decision registry keying

`$MEMCONTINUUM_HOME/decisions.tsv`, one row per repo:
`key <TAB> decision <TAB> iso-date <TAB> note`.

The key is the `origin` remote URL when there is one and the working tree's
absolute path otherwise. Remote-keyed on purpose: a path key evaporates the
moment a repo moves on disk, and a settled decision then looks unmade. On a
path-key miss after a move the repo reads as `undecided` and the detector
emits its ask once more — re-emit, never assume.

A decline stops the asking, not an existing installation. Nothing in this system
ever deletes a store: a store is its own git history, not an installer artifact.

### Registry row parameters (`decide.sh wired`)

The `note` column carries `store=`/`project=` unconditionally (blank when not
given, back-compatible with every pre-existing row) and, when given,
`claude-dirs=`/`code-roots=`/`langs=`/`never=` — each of the last four is a
`;`-joined list, omitted entirely (not written blank) when never passed.
`--claude-dir` is repeatable: one project can list more than one claude-dir
(a working-dir `.claude` beside a bare code checkout, or two session homes
pointed at the same store), and `wired` refuses unless **every** one given is
fully wired — recording the row after checking only the first would recreate
"nothing tracks the set of claude-dirs a project's wiring lives in."
`mc-registry-lib.sh` carries the shared parsing: `mc_note_field NOTE KEY`
pulls one `key=value` field out of the note (space-separated between fields,
so `store=` — first, no leading space — and every later field parse the same
way), and `mc_command_env_value CMD VAR` pulls one `VAR=value` token out of a
rendered hook command line — `memcontinuum-state.sh` and
`memcontinuum-update.sh` both use it instead of each hand-rolling the same
extraction. It is pure parameter expansion, anchored on an assignment
boundary (so a variable that is a suffix of a longer name is never read out
of it), and it sets `MC_ENV_PRESENT` alongside `MC_ENV_VALUE`: an explicitly
empty `MEMCONTINUUM_LANG_EXTS=''` is a *recorded answer* (language-less
wiring, chosen on purpose), while the variable being absent means the wiring
predates the set being written down at all — unknown, not "none". Reading the
second as the first would silently turn a project's code indexing off, so the
two are kept apart everywhere.

**Values with spaces.** The note column is space-separated `key=value` fields
with `;` between list elements, so a raw space inside a value would end its
own field early and turn the remainder into garbage fields — and real stores
do live under paths with spaces. Every value is therefore percent-encoded on
the way in (`mc_note_encode`: `%` → `%25` first, then ` ` → `%20`) and
decoded on the way out (`mc_note_decode`, in the opposite order, which is
what lets a value literally containing the text `%20` survive). `;`, tab and
newline are *not* encoded — `decide.sh` refuses a value containing one, since
a decoded `;` would arrive after the field had already been split on `;`.
Accepted edge: a row written before this encoding, holding a path with a
literal `%` followed by `20` or `25`, decodes wrongly; the fix is to rewrite
the row.

Splitting a stored list back into arguments goes through `mc_split_semi`, and
the `--code-root`/`--langs`/`--never-ext` tail every re-run command shares is
built once by `mc_build_wiring_args`. Both exist because the hand-rolled
versions they replace used `tr ';' ' '` with an unquoted expansion, which
word-splits every path containing a space and glob-expands the rest.

## Render fingerprint and the updater

Every rendered hook line, every rendered `<claude-dir>/rules/memcontinuum.md`,
and the installed `memory-search` skill copy carry a stamp:
`MEMCONTINUUM_RENDERED=<fingerprint>` as one more env token on each of the
eight hook command lines, and `<!-- memcontinuum-rendered: <fingerprint> -->`
as an HTML comment — the rules file's second line (after its identity-marker
first line); the skill copy's, right after the frontmatter's closing `---`
(never at byte 0 — the skill loader needs the opening `---` to stay line 1).
The engine repo is itself a wired MemContinuum project, so its own
`.claude/` install artifacts (the installed skill copy included) are
gitignored render targets like any other checkout's — never tracked — with
the templates under `skills/` and `templates/` staying the sole tracked
source.

**The stamp is a fingerprint of the render inputs, not the engine's HEAD
commit.** Two kinds of change reach a wired repository in completely
different ways. A fix to a *script* — a hook, `memidx.py`, the walker —
arrives the moment the checkout is pulled, because every rendered hook line
runs that script from the checkout by absolute path; nothing needs
re-rendering. A change to what gets *rendered* — a template, this installer's
own rendering, the settings merge, a skill copied into a repo — reaches nobody
until the installer is re-run there. A HEAD sha moves on both, so it marked
every wired repo on the machine stale after any commit to anything: the exact
distinction this command exists to draw, drawn wrong. Hashing the inputs draws
it: a scripts-only commit leaves every repo `ok`; a template or installer
change flips them `stale`.

**There are two fingerprints, one per layer.** The per-repo wiring and the
machine layer are re-rendered by different commands, and a repository has no
way to act on the other one's drift. With a single combined fingerprint, an
edit to `memcontinuum-setup.sh` marked every *per-repo* row `stale`, and
`--apply` then re-rendered all of them to no effect while leaving the drift
that actually existed — in `~/.claude` — untouched.

`mc_render_fingerprint SCOPE ENGINE_ROOT` (`mc-registry-lib.sh`) is the one
function that computes both. `SCOPE` is `repo` or `machine`; anything else
returns `unknown` rather than quietly hashing something. It is 12 hex
characters of a `sha256sum` (falling back to `shasum -a 256`) over the scope's
inputs in a fixed order, each preceded by its path relative to the checkout so
that adding, removing or renaming one counts as a change.

| scope | input | why |
|---|---|---|
| `repo` | `scripts/repo-init.sh` | does the per-repo rendering |
| `repo` | `templates/*` | everything rendered from a template |
| `repo` | `skills/memory-search/SKILL.md` | the skill copied into a project |
| `machine` | `memcontinuum-setup.sh` | renders the detector hook line and `config.sh`, both templated inside it |
| `machine` | `skills/memcontinuum/SKILL.md` | the skill copied to the user level |
| both | `scripts/mc_settings_merge.py` | lands the rendered blocks, per repo and machine-wide alike |

Who uses which: `repo-init.sh` stamps with `repo`; `memcontinuum-update.sh`
compares each registry row against `repo` and, by default now, the
detector entry in the claude-dir `memcontinuum-setup.sh` recorded (usually
`~/.claude`) against `machine` — `--machine` is an accepted no-op and
`--no-machine` is the opt-out that skips this comparison entirely, not the
other way around; `memcontinuum-state.sh`'s per-repo hint uses `repo`.
`memcontinuum-setup.sh` renders `MEMCONTINUUM_RENDERED=<machine
fingerprint>` onto the one hook line it installs — that is what this
machine-layer comparison reads back.

`hooks/*.sh` are deliberately **not** inputs in either scope: they are
executed by path, so pulling updates them live. That includes
`hooks/memcontinuum-detect.sh` — the machine layer renders a hook *line*
naming it, never a copy of it. `LC_ALL=C` is set around the globs so the file
order is byte-ordered and a checkout fingerprints identically on every
machine. The literal `unknown` when no sha256 tool is available or the
checkout is incomplete — read downstream as "cannot tell, re-render to find
out", never as an error.

`<claude-dir>/rules/memcontinuum.md` is rendered from
`templates/memcontinuum-rules.md` on every install, fresh or re-run — the one
rendered file that is never write-if-absent, because its whole job is to
always name the *current* store. It is refused (exit 14), before any other
mutation, when it already exists and its first line does not match the
template's identity marker verbatim: a hand-authored or foreign file at that
path is left alone, loudly, rather than overwritten.

The installed `memory-search` skill copy is refused the same way (exit 16) —
before any mutation, when `<claude-dir>/skills/memory-search/SKILL.md`
already exists and is not this tool's own. Identity here cannot be
a fixed first line the way the rules file's is (the opening `---` has to stay
byte 0 for the skill loader), so it is instead "the frontmatter carries a
`name: ...` line" — read from `skills/memory-search/SKILL.md`'s own
frontmatter at runtime (`mc_skill_identity_marker`), the same way
`mc_rules_identity_marker` reads the rules file's marker from its template,
never a literal hardcoded in the installer or the library — and checked by
`mc_skill_copy_is_ours` (`mc-registry-lib.sh`), the one predicate
`repo-init.sh`'s refusal and `memcontinuum-update.sh`'s `skill` column
(below) both call against that marker, rather than each hardcoding its own
copy of the check. A copy that already carries that identity is overwritten
on every re-run regardless of its stamp, same as the rules file.

`scripts/memcontinuum-update.sh` walks every `wired` row and, for each
claude-dir the row lists, compares five things against the engine right now:
the stamp on that claude-dir's rendered hook lines, the row's own `store=`
against the rendered `MEMCONTINUUM_ROOT` on those same lines (a stamp match
alone cannot catch a store renamed under the same engine version), the rules
file's identity marker + stamp, the installed `memory-search` skill
copy's identity + stamp, and the STORE's git post-commit/pre-commit wrapper
state (`store-hooks`: ok/stale/missing/foreign/not-checked, informational
only — see `mc_update_store_hooks_state`'s own comment). It prints one table row per
(row, claude-dir): `repo | claude-dir | stamped | engine | store-match |
rules | skill | store-hooks | action`, action being one of `ok`, `stale`, `store-mismatch`,
`store-form-stale`, `store-form-updated`,
`rules-missing`, `rules-stale`, `rules-foreign`, `skill-foreign`, `migrate`,
`migrate-needs-claude-dirs`, `migrate-needs-langs`,
`migrate-needs-never-exts`, `migrate-dirs-disagree`, `store-missing`,
`no-wiring`, or
`unrecoverable`. `store-form-stale` is the reporting walk's answer when the
row's `store=` names the same store the wiring renders, written in an
unresolved (symlinked) string form: nothing is mismatched, so the remedy it
prints is to re-run with `--apply`. `store-form-updated` is what that
`--apply` pass reports after rewriting just the registry's `store=`
field, leaving `claude-dirs`/`code-roots`/`langs`/`never` exactly as
recorded. A `store=` naming a *different* store stays `store-mismatch`,
which the pair never stands in for.
`--dry-run` (the default with no `--apply`) only prints;
`--apply` re-runs `repo-init.sh` per non-`ok` claude-dir with the row's own
recorded parameters, always passing `--adopt-only` (below), so this command
cannot create, rename, or delete a store on any path through it.

The `rules` and `skill` columns share one determination helper
(`mc_update_artifact_state`) for the missing/stale/ok part — parsing the
`<!-- memcontinuum-rendered: ... -->` stamp comment and comparing it through
`mc_fingerprint_match` — because that part is identical for both artifacts.
Identity detection differs in shape: the rules file's identity marker is a
literal, fixed first line (`mc_rules_identity_marker`, from the template that
defines it); the skill copy's opening line has to stay a bare `---` for the
skill loader, so its identity is instead "the frontmatter contains the
`name: ...` line `mc_skill_identity_marker` reads from
`skills/memory-search/SKILL.md`'s own frontmatter" — one shared predicate,
`mc_skill_copy_is_ours` (`mc-registry-lib.sh`), used here AND by
`repo-init.sh`'s own refusal (above) against that same runtime-read marker,
rather than each hardcoding its own copy of the check or the marker text —
and its stamp sits right after the frontmatter's *closing* `---` rather than
at a fixed line number.

The `store-hooks` column covers the store's git
`post-commit`/`pre-commit` wrappers — the only artifacts `repo-init.sh`
renders into a project that carry no stamp of their own (they are three raw
values, not a template): `ok`/`stale`/`missing`/`foreign` per wrapper, the
column showing the worse of the two (`foreign` > `missing` > `stale` >
`ok`), currency judged by re-deriving the exact bytes `repo-init.sh` would
write now for the row's `store`/`project`/resolved python and comparing
byte-for-byte. `not-checked` when the store is gone, git resolves its hooks
directory outside its own `.git` (a shared `core.hooksPath` —
`repo-init.sh` itself refuses to install a wrapper there), or no python can
be resolved to build the comparison against. It is informational only:
unlike `rules`/`skill` it does not feed the `action` column or `--apply`'s
re-render decision — `repo-init.sh` already regenerates or correctly skips
both wrappers unconditionally on every install it performs, so a stale or
missing `store-hooks` gets fixed as a side effect whenever `--apply`
re-renders a claude-dir for any other reason; `foreign` never does,
since `repo-init.sh` refuses to overwrite a hand-authored wrapper even
then. None of the three moves the `action` column, so the gap this
narrows rather than closes is wider than just python drift: a row that
is otherwise fully `ok` stays `store-hooks: stale` (only the machine's
resolved python changed underneath it), `missing` (the wrapper was
deleted, or predates this feature), or `foreign` (hand-authored) until
something else triggers a re-render (or the row is re-rendered by hand).

Beneath the table, a `not-checked: ...` line names every OTHER artifact
`repo-init.sh` renders into a project that has no column of its own — the
store's `README.md`, `.gitignore`, and its tree (`topics`/`incidents`/
`investigations`/`concepts`/`sources`/`inbox/*`, each `.gitkeep`-marked).
None of these are ever re-rendered over an existing file, so nothing here
ever looks at their current content to judge it stale — not because their
content structurally cannot vary, but because looking would mean
overwriting something this command must not silently clobber (an "adopt
an existing store" install must not clobber a hand-authored README).
README.md and .gitignore are the clean case: each is skipped outright
once it exists, so a hand edit rides through every later re-render
untouched. The tree is narrower: `mkdir -p`/`touch .gitkeep` run
unconditionally rather than behind an existence check, so an entry that
is still there is equally left alone, but one that was deleted is
silently recreated (empty, `.gitkeep`-only) the next time any re-render
touches that claude-dir — not because anything inspected it, only because
nothing ever looked to notice it was gone. `not-checked` is the honest
and complete answer for all of them either way, printed every walk
rather than left to read as silence. A dedicated coverage test
(`tests/test_update.py`
`TestUpdaterCoversEveryRenderedArtifact`) derives the artifact list from a
real `repo-init.sh` run's own before/after filesystem diff, not from a
hand-copied list, so a new rendered artifact this table does not account
for fails that test rather than silently reading as health.

A missing or stale skill copy reports as the same generic `stale`
action the hook-stamp check already uses (not a `skill-missing`/`skill-stale`
action of its own, unlike the rules file) — it is the same kind of drift, not
a new question. A *foreign* skill copy reports `skill-foreign` and is refused
like `rules-foreign` (below) — belt and braces with `repo-init.sh`'s own
refusal, the same relationship the rules file's own belt-and-braces has: the
identity check is what makes the refusal correct either way; the ranking
above it is what keeps `--apply` from calling `repo-init.sh` just to watch it
refuse for a reason already named in the table.

**The flag/mode matrix** — which mode each flag combination selects, and what
each mode consumes versus refuses — is documented once, in
`memcontinuum-update.sh --help` ("MODES, AND WHICH FLAGS EACH ONE TAKES").
That text is the prose source; the script's own comments point back at it
rather than restating it. What follows here is the maintainer-level detail
--help does not carry: why the refusals exist, and how the row-dependent half
is implemented.

Two refusals precede the modes entirely, at the argument loop. An **empty or
whitespace-only value** for any flag that takes one is refused
(`<flag> needs a non-empty value`, rc 2): each of these reads its own empty
value as "not given", so an empty one never failed — it changed what the
command *was*. `--repo ''` walked every wired row; `--langs ''` migrated with
whatever it recovered; `--claude-dir ''` narrowed a recorded row's walk to the
empty set and exited 0 with no table, which is the shape of a clean bill of
health for a repository it had just been told to re-render. And **`--apply`
with `--dry-run`** is refused in either order — they are opposites, and
last-one-wins would make the same pair of flags write or preview depending
only on typing order, discarding the other in silence.

Everything else decidable from the command line alone is enforced before the
registry is opened. The rest is a property of the *row*, so `repo` mode splits
when the row is read:

- a row that **records no `claude-dirs=`** also consumes `--code-root`,
  `--langs` and `--set-never-ext`: they supply the parameters the row never
  recorded.
- a row that **already records them** refuses those three — this mode does
  not rewrite what a row records (`--add-lang`/`--never-ext` do, additively,
  or a `decide.sh wired` line). `--claude-dir` changes meaning rather than
  being refused: it **narrows** the walk to the dirs it names, and each must
  be one the row already records. One that is not is refused as
  `dir-not-recorded` — never walked, never installed into.

`--repo` naming a repository with no `wired` row — undecided, or a recorded
`declined` — is refused before the walk begins:
`no-wired-row: <key> (decision=none|declined|…)`, non-zero. It used to match
nothing and print an empty table at exit 0, which reads as "checked, all
current" for a repository this command never had anything to say about. It is
also the case that left the row half of the matrix unreachable: with no row to
judge them against, the row-dependent flags were neither consumed nor refused.
One helper (`require_wired_row`) serves both the walk and targeted mode, so
the answer is the same whichever asked, and it names the decision that *is*
recorded rather than reporting a bare miss.

**Action precedence.** The answers this command will never act on come first:
`store-missing`, then `no-wiring`, then `rules-foreign`, then
`skill-foreign`, then the `migrate-needs-*`/`migrate-dirs-disagree`
questions, and only then the drift it can act on (`stale`,
`store-form-stale`/`store-form-updated`, `store-mismatch`, `rules-missing`,
`rules-stale`, a missing/stale skill copy folded into `stale`, `ok`).
Ordering them the other way would name some lesser drift in the action
column and then have `--apply` call the installer just to watch it refuse
for a reason already known.

`store-form-stale`/`store-form-updated` sit between `stale` and
`store-mismatch` in that chain, and they are the one answer `--apply` fixes
without re-rendering anything: the wiring already renders the physical
store, so only the registry's `store=` field is rewritten. A row that is
*both* fingerprint-stale and store-form-stale reports the higher `stale`,
and a single `--apply` still corrects both — the store-form correction rides
along with that re-render rather than waiting for a second pass.

`store-missing` outranking `no-wiring` matters on its own. A claude-dir with
no hook lines is normally "finish the install" — but when the row's store is
gone as well, sending someone to re-install is sending them to seed a fresh
store over a dead one and call the result repaired. The dead store is the
fact that has to be said first, so the store is checked before the wiring is
looked at at all.

**Exit codes.** Three refusals happen before any row is walked and are not
the walk's own answer: an empty/whitespace-only flag value, `--apply`
together with `--dry-run`, and `--repo` naming a row that is not `wired` —
each exits non-zero with nothing read and nothing written. Past that point,
the reporting walk always exits 0 — there, a stale row is the answer, not an
error. `--apply` exits 0 only when every claude-dir it walked ended up
correct: already `ok`, or re-rendered successfully. Anything left undone — a
failed installer run, a dir deliberately skipped, or a row that could not be
resolved to a claude-dir at all (`unrecoverable`: no `project=` recorded, or
a remote-keyed row with no `claude-dirs=` on record) — exits non-zero, with
the table still printed in full and the reason on stderr. `no-wiring` is the
single exception, for the reason below.

**An `unknown` fingerprint never compares equal.** `mc_render_fingerprint`
returns the literal `unknown` when it cannot compute one: no `sha256sum`/
`shasum` on the machine, or a checkout missing its render inputs. That is the
absence of an answer, not an answer, and `[ "$a" = "$b" ]` on two absences
reports the artifact as current — precisely the claim nobody was able to
check. One helper, `mc_fingerprint_match`, is the only comparison: `unknown`
or empty on either side is a mismatch, and a mismatch is `stale`. It is used
by the table's stamp column, the rules file's own stamp line (the stamp is
parsed out of the rendered comment rather than the whole line being compared
as text), the machine layer, and `memcontinuum-state.sh`'s drift hint.
Re-rendering something already current is a no-op; calling something current
that nobody verified is not.

**`--adopt-only`.** `repo-init.sh` grows a flag that refuses, before writing
anything at all, unless `--store` is already a git working tree carrying this
tool's markers (`mc_is_marked_store`, the same predicate the installer's own
classify step uses). No `--force` carve-out, and `--dry-run` refuses too. The
re-render command passes it on every installer run it makes.

Actions that are deliberately never auto-applied:

- **`store-missing`** — the row's `store=` path is no longer an existing
  MemContinuum store (renamed, deleted, or replaced by an unrelated git
  repo). Re-running `repo-init.sh` against a missing `--store` would *seed a
  fresh one* there, which is exactly the "stores never touched" line this
  command does not cross. It outranks every other answer, including a stamp
  and a `store=` that both still look right: those compare strings, and a
  string agrees just as happily with a store that is gone.
- **`rules-foreign`** — `repo-init.sh` itself refuses to overwrite a foreign
  rules file (see above), so calling it would just fail loudly for a reason
  already named in the table. Reported; skipped.
- **`skill-foreign`** — the installed `memory-search` skill copy's frontmatter
  does not carry the identity marker (`mc_skill_copy_is_ours`, checked
  against `mc_skill_identity_marker`'s runtime read of the template), so it
  was not rendered by this installer. Same relationship to `repo-init.sh` as
  `rules-foreign`: `repo-init.sh` itself refuses to overwrite it (exit 16,
  same identity check), so calling it would just fail loudly for a reason
  already named in the table. Reported; skipped.
- **`no-wiring`** — a claude-dir this row lists has none of this project's
  hook lines at all (settings deleted or badly broken). That is the `memcontinuum`
  skill's repair path (an undecided/broken install), not this command's — a
  `wired` row is never a license to *wire* anything; it only ever re-renders
  wiring that is already there.

A row written before this registry format existed (no `claude-dirs=` in its
note) is an **old-format row**: its single claude-dir is recovered as `<repo>/.claude`
when the row's key is a path (starts with `/`); a remote-keyed old-format row's
repo path is not recoverable from the registry at all and is reported as
`unrecoverable` with a fix command, never guessed (guessing could touch the
wrong repo's `.claude`). A recoverable old-format row also has its `code-roots=`/
`langs=`/`never=` recovered from what is actually rendered on its
`newfile-nudge.sh` line today (`MEMCONTINUUM_CODE_ROOT`/`_LANG_EXTS`/
`_NEVER_EXTS`, the extension globs mapped back to language names via
`chunkers.LANGUAGE_TABLE`'s own extension sets) rather than from
`mc_wired_commands_for_project`, which is scoped to the five always-wired
write-side basenames and never matches `newfile-nudge.sh`/`pre-edit-chain.sh`
by design (see "The five basenames" above) — `memcontinuum-update.sh` has its
own basename-parametrized scan for this one case.

**The migration proposes; a human decides.** The recovered `<repo>/.claude`
is shown in the table as a *proposal*, never written from. Migrating an
old-format row requires the human to name the full claude-dir set on the
command line:

```
scripts/memcontinuum-update.sh --apply --repo PATH \
    --claude-dir DIR [--claude-dir DIR ...] \
    [--code-root DIR ...] [--langs LIST] [--set-never-ext LIST]
```

Without it the action is `migrate-needs-claude-dirs` and nothing is written.
The reason is structural: one project can have several claude-dirs, and
nothing on disk says how many — the command can see the one it found and no
more. **A second claude-dir joins a project's row only by being named on a
`memcontinuum-decide.sh wired` command line** (directly, or through the
`--claude-dir` above, which builds that command). Nothing discovers one; that
topology is manual by design.

Two more refusals follow the same rule — the row is what every future
re-render replays, so a value invented here would be permanent:

- **`migrate-needs-langs`** — the wiring carries no `MEMCONTINUUM_LANG_EXTS`
  at all, so it predates the language set being written onto the hook line.
  Unknown, not "none": recording "none" would turn code indexing off for a
  project that had it on. Pass `--langs LIST`. (An explicitly empty
  `MEMCONTINUUM_LANG_EXTS=''` is a recorded answer and migrates without one.)
- **`migrate-needs-never-exts`** — the rendered never-mention list is not a
  plain extension list (hand-edited). Pass `--set-never-ext LIST`.
- **`migrate-dirs-disagree`** — the named claude-dirs were recovered
  separately and do not hold the same code-roots, languages or never-list.
  Every dir's recovery is printed under the row; resolve it with explicit
  `--code-root DIR` (repeatable), `--langs LIST`, `--set-never-ext LIST`.

  The comparison is on the **raw rendered values** — the
  `MEMCONTINUUM_LANG_EXTS`/`MEMCONTINUUM_NEVER_EXTS` glob strings and the
  code-root list exactly as they sit on the hook lines — not on the language
  names they normalize to. Normalizing is lossy in the one direction that
  matters here: `*.py` and `*.py *.zz` both come back as the language list
  `python`, and treating them as equal lets one dir's wiring be replayed over
  the other's, changing what it indexes. Partial-render notes are collected
  per dir and all of them printed, attributed to the dir they came from; the
  first dir's note standing for the row meant a fully rendered first dir hid a
  partially rendered second one entirely.

**One row is one project, and one project has one wiring set.** A registry
row records a single `code-roots=`/`langs=`/`never=` triple, and every
claude-dir the row lists is rendered from it. That is what makes a re-render
deterministic, and it is why an old-format row's parameters are recovered from
*every* named claude-dir rather than from whichever comes first. The whole
reason an old-format row is being migrated is that nothing ever wrote its
parameters down — so nothing enforced that invariant either, and two dirs
installed months apart can genuinely differ. Reading the first and replaying
it onto the rest would silently re-render the others with languages they
never indexed and record the result as though a human had chosen it. So the
recovery refuses instead, and the human names the set.

`--set-never-ext` is the migration's own spelling, deliberately not
`--never-ext`. `--never-ext` ADDS one extension to a row that already has its
parameters recorded, and it has no second meaning: an earlier round let
`--apply` silently change what the same flag *meant*, which is the kind of
quiet reinterpretation a command that writes a registry must not do.
Combining `--add-lang`/`--never-ext` with `--apply` is refused, with both
spellings named in the error.

Each named `--claude-dir` must already carry this project's wiring: the
migration *records* what is installed and never wires a directory from
scratch. `--apply` re-renders every named claude-dir first and rewrites the
registry row only if all of them succeeded (`migrate` in the table); a row
rewritten after a failed render would describe wiring that exists nowhere.
Every subsequent walk sees the row as current-format.

`--add-lang LANG [--never-ext .ext] --repo PATH` and
`--never-ext .ext [--add-lang LANG] --repo PATH` are additive-only (a human
typing the command is the consent, so this mode applies unless `--dry-run` is
given explicitly — not gated on `--apply`, which the walk mode's dry-run
default would otherwise make it silently no-op): they union the given
language/extension into the row's existing `langs=`/`never=` lists and never
drop what was already there.

**Render first, record second**, always: every claude-dir the row lists is
re-rendered, and only if all of them succeeded is the row rewritten via
`decide.sh wired` with every field. A row written first would describe a
language set that exists nowhere the moment a render failed — and every later
re-render replays that claim.

The re-render itself is **all-or-nothing**, as far as two installer runs can
be made to be: each claude-dir is run with `--dry-run` first, and only an
all-clear turns into real writes. `repo-init.sh`'s refusals (a foreign rules
file, an unwritable claude-dir, an `--adopt-only` store that is not there)
all fire during `--dry-run`, before it writes anything, which is what makes
the preflight worth running. The dirs on one row share one language set by
construction, so converting the first and failing on the second would leave a
project describing itself two different ways. If a real run still fails after
its own dry run passed, the drift is reported explicitly — which dirs
converted, which did not, and that the row is unchanged — rather than exiting
on a bare failure.

Four refusals come before any of that, in this order:

- **row records no `claude-dirs=`** — this mode re-renders the dirs a row
  *names*; `<repo>/.claude` is not substituted for them, ever (a project's
  wiring can live outside the repo, and in more than one place). Migrate the
  row first, with the command above.
- **`no-code-root`** — the row records no `code-roots=`. `repo-init.sh`
  ignores `--langs`/`--never-ext` without a `--code-root` to wire them into,
  so the language set would render nowhere while the row claimed it.
- **store-missing** — the same `mc_is_marked_store` check the walk uses. No
  row is ever rewritten to describe wiring for a store that is gone.
- **unknown language** — validated against `chunkers.LANGUAGE_TABLE` before
  anything is touched. `repo-init.sh` validates `--langs` too, but only when
  the install has a `--code-root` to wire it into.
- **`dir-not-wired`** — a recorded claude-dir that carries no wiring *for
  this project* (`mc_wired_commands_for_project`, so another project's hook
  lines in the same claude-dir do not count). A row is a record, not a
  warrant: it can name a directory that was wiped, and pointing the installer
  at one would wire it from scratch — the one thing this command never does.
  Checked for every dir before any of them is touched.

The machine layer is reported as one extra line
(`machine: DIR rendered by X, engine at Y, skill S, config C -- ok|stale`),
comparing the `machine` fingerprint against the stamp on that dir's detector
entry. `S` and `C` are informational only — a direct byte-for-byte check of
the machine-level skill copy (`ok`/`stale`/`missing`/`not-checked` when the
engine's own copy cannot be read) and whether `config.sh` exists
(`present`/`missing`) — neither feeds the `ok|stale` verdict or `--apply`'s
refresh decision, both of which stay keyed on the detector hook's own stamp
alone; they exist because the skill copy carries no stamp of its own; only
the hook line does, so without this it is inferred from a sibling artifact
rather than checked directly. *Which*
claude-dir is a fact only `memcontinuum-setup.sh` knows — it takes
`--claude-dir` and defaults to `~/.claude` — so it records the answer in
`config.sh` as `MEMCONTINUUM_MACHINE_CLAUDE_DIR`, and this reads it back
(falling back to `~/.claude` only for a `config.sh` written before that
existed). Assuming `~/.claude` reported a real install elsewhere as absent,
and `--apply` would then have rendered a *second* machine layer at the
default path while the stale one stayed stale. With `--apply` it re-runs
`memcontinuum-setup.sh --claude-dir DIR`, but only when the comparison says
`stale`. Reported by default now — a health check that only answers for the
layer it was asked about reads as "everything is current" to whoever runs
it, and that is exactly how a stale machine-level skill copy went unnoticed
for days. `--machine` is kept as an accepted no-op for anything that already
types it; `--no-machine` is the opt-out for the rare caller that wants the
repo rows alone. A missing registry does not skip the machine report either,
because a machine can perfectly well have its own layer installed before any
repository is wired.

Immediately after a refresh that SUCCEEDED (never on an already-`ok` layer,
never on its own outside a refresh, and never after a refresh that failed —
a failed one leaves a `config.sh` it never rewrote, so the python it names
is not the one this run was told to use),
`--apply --machine` also reconciles the pinned tree-sitter grammar wheels and
the tree-sitter runtime.
It re-reads `config.sh` fresh — the refresh's own
`memcontinuum-setup.sh` call may just have rewritten
`MEMCONTINUUM_VENV_MANAGED` via that script's sticky-flag determination
(below) — and branches on it: `1` (an engine-managed venv, one
`memcontinuum-setup.sh` created itself with no explicit `--python`)
reinstalls `requirements.lock` into it (`<python> -m pip install -r
requirements.lock`, falling back to `uv pip install --python <python> -r
requirements.lock` when `-m pip` fails and `uv` is on `PATH` — a
`uv venv`-created venv ships no `pip` module by default, verified against
this machine's own `uv`, so the fallback is what actually reconciles one of
those rather than the primary attempt failing silently on the exact venvs
this step exists to maintain) and then runs `memidx.py backend-preflight
--json`, warning by name about any row still `ok:false`. `0` (a foreign
`--python`) skips the reinstall entirely — this command never pip-installs
into a python it was not told to manage — and instead reports each missing
row by name with the remedy: install the seven pins into that python
yourself, or re-run `memcontinuum-setup.sh` without `--python` for a venv
this command can maintain.

`MEMCONTINUUM_VENV_MANAGED`'s own sticky-flag rule lives in
`memcontinuum-setup.sh`, not here: an explicit `--python` starts unmanaged
(`0`) unless it re-affirms the *exact same path* `config.sh` already
recorded as managed (`1`) — the case that matters because
`memcontinuum-update.sh --machine` always re-resolves and re-passes a
python explicitly (`mc_update_resolve_python` / `--python "$PY"` in
`SETUP_ARGS`) on every run after the first, so an engine-created venv would
otherwise read back as a "foreign" `--python` on its own second refresh and
reconciliation could never fire again. Choosing "use this python" from
`memcontinuum-setup.sh`'s own interactive setup menu (below) counts as an
explicit `--python` for this same determination.

`memcontinuum-setup.sh`'s own interactive setup menu — "1) use this python
(PATH) / 2) create or reuse the engine venv / 3) abort" — appears only on a truly
unscripted run: no `--python`, no `--venv`, and stdin is a real terminal
(`[ -t 0 ]`, the same guard `repo-init.sh`'s own census dialogue uses). Any
scripted, CI, or explicit-flag run bypasses it and keeps the plain
create-if-absent default this script has always had.

Only `1`, `2` and `3` are answers. Anything else — a typo, a stray word, a
bare Enter — is asked again, up to three times, and then the run aborts with
exit 1 having written nothing. Enter is not a shortcut for any option: the
create-if-absent default belongs to the runs that never see this menu.

`memcontinuum-state.sh` stays python-free and prints one extra line,
`update: wiring rendered by X, engine at Y -- run scripts/memcontinuum-update.sh`,
only when the two differ — pulled from the same registry-pinned hook command
line its `store=`/`project=` extraction already resolves, plus one
`mc_render_fingerprint` pass over the engine checkout's render inputs (never
anything on the repo being reported on). It looks for that command line in the
claude-dir(s) the registry row records, falling back to `<repo>/.claude` when
the row names none: a project whose wiring lives in a session home beside a
bare checkout would otherwise have had the hint go permanently silent —
exactly the repositories most likely to drift.

## Path syntax in hook filters

Rendered `if` filters carry a **second leading slash** on top of the absolute
path — `Edit(//abs/path/**)`, `Write(//abs/path/**)`. In Claude Code's
permission-rule path syntax a single leading slash anchors the pattern at the
settings source directory rather than at the filesystem root, so
`Edit(/abs/path/**)` silently matches nothing. Every rendered filter therefore
doubles it. `hooks/install-hooks.md` documents the wiring each hook receives.

Each `--code-root` gets its own correctly-scoped entry in both PreToolUse
hooks: `pre-edit-chain.sh` an `Edit`/`Write` pair, `newfile-nudge.sh` a `Write`
entry with `MEMCONTINUUM_CODE_ROOT` set to that specific directory. The five
write-side hooks receive every configured code root: `MEMCONTINUUM_CODE_ROOT`
carries the first (kept, for a reader that only ever looks at one root) and
`MEMCONTINUUM_CODE_ROOTS` carries the complete JSON list of physical paths.
`hooks/memlib.sh`'s `mc_code_roots` reads the list (falling back to the single
variable when the list is absent) and every write-side hook containment/
comparison walks it — `ledger-post-edit.sh` checks a path against every root,
`userprompt-remind.sh`/`precompact-persist.sh` pass every root to `unmapped
--code-root` (repeatable) in one call, and `sessionstart-remind.sh` records
each root's git HEAD.

## The watchdog

macOS ships neither `flock` nor `timeout`, and both hooks and installer must run
on stock macOS. The lock is solved in Python (above); the deadline is solved by
`hooks/mc-watchdog.sh`: a guarded hook re-execs itself as a child under a small
Python launcher that kills the whole child process group once a budget expires —
2 seconds by default, 1.2 seconds for `sessionend-stamp.sh`.

Guarded: the five write-side hooks, plus `newfile-nudge.sh` (which has no
write-side state of its own but shares the same guard rather than growing a
second bespoke timeout story for the one hook that happens to be fast),
`pre-edit-chain.sh`, `code-refresh.sh` (budget by event, table below), and `post-commit-reindex.sh` (its own budget,
`MEMCONTINUUM_POST_COMMIT_BUDGET`, default 30 seconds -- generous on purpose:
its guarded content pass is measured well under a second; the budget is a
backstop against a hung/slow filesystem, not a tuned ceiling). Unguarded:
`memcontinuum-detect.sh` and `pre-commit-append-only.sh` — deliberately, in
the second case: this hook's whole point is to fail CLOSED on a history edit,
and a watchdog that kills a slow check and lets the commit through anyway
would turn the exact slowness the check exists to catch into a bypass. It
still sources `mc-watchdog.sh` for the `MEMCONTINUUM_HOME`/`config.sh`
pointer-chain resolution the guard block also performs, but never invokes
that guard block itself.

| hook | budget |
|---|---|
| the five write-side hooks, `newfile-nudge.sh`, `pre-edit-chain.sh` | 2 s |
| `sessionend-stamp.sh` | 1.2 s |
| `code-refresh.sh` on `SessionStart` | 2 s |
| `code-refresh.sh` on `Stop` | `MEMCONTINUUM_CODE_REFRESH_BUDGET`, default 8 s (a current index refreshes in 0.2-0.7 s, a weeks-stale one in 1.4-2.8 s, measured with `--no-embed`) |
| `post-commit-reindex.sh` | `MEMCONTINUUM_POST_COMMIT_BUDGET`, default 30 s |

`code-refresh.sh` picks its budget from the event: it peeks `hook_event_name` from a bounded
prefix of the payload before the guard, the same way `ledger-post-edit.sh` peeks `tool_name`. That
read is bounded in bytes (4096) and in time (about 2 s) and uses shell builtins only, one character
at a time (bash 3.2 discards what a timed-out `read -n` had collected), so a writer that keeps
stdin open cannot hang the hook before the watchdog exists; the worker receives the prefix and
takes the two fields it needs from it even when the JSON is cut short. The shell normalises
`MEMCONTINUUM_CODE_REFRESH_BUDGET` once and exports the result as `MC_WATCHDOG_BUDGET`; the
Python side reads only that.

`pre-edit-chain.sh`'s own inner budget is confirmed against a real
measurement of its wired command line across the engine's own store and two
other real, live projects' stores, one of them hosted entirely on a slow
drvfs (`/mnt/c`) mount, code root and store both:
34 timed samples give an overall p95 of 0.198s and p99 of 0.206s, comfortably
under the unmodified 2-second default (roughly 10x headroom). Claude Code's
own per-hook `timeout` field defaults to 600 seconds when unset
(code.claude.com/docs/en/hooks.md, "Common fields"); `pre-edit-chain.sh`'s
rendered `"timeout": 5` is a backstop against a hung watchdog itself, not the
mechanism meant to fire — the inner 2-second budget above is.

That headroom is what the controlled measurement above found, not a
guarantee about every store this hook ever runs against — a real store's
actual behavior depends on where it lives, and a reading of one machine's
`hook.log` taken before a slow store had been moved off a Windows drive
mixed that store's old, misconfigured timings in with its current,
native-disk ones; that comparison has been withdrawn as invalid, not
corrected into a new multiplier. The 2-second budget still holds
regardless (fail-open means a fired watchdog costs one citation, never a
blocked edit), and the 34-sample figure above is still an accurate
description of the controlled run that set it. What production lookups
actually look like on a given store is best read fresh from that store's
own `hook.log`, dated after its own last known move — see the README's
lookup paragraph for the current reading.

Unlike the five write-side hooks, whose timeout costs at most a lost
reminder, `pre-edit-chain.sh`'s timeout carries an asymmetric cost: it costs
the one citation this tool exists to put in front of the model before the
edit lands. Fail-open still applies — the edit proceeds either way — but
losing that citation is not the same size of loss as a dropped write-side
nudge.

A timeout on the inner watchdog path emits a minimal `additionalContext`
stating that retrieval timed out and that the absence of a decision was not
established, so the model reads an honest uncertainty signal instead of an
empty context indistinguishable from a genuine no-match. The outer Claude
Code timeout is a last-resort backstop only and cannot provide this
fallback, since it discards the hook's output entirely — it guarantees the
run ends, not that anything useful comes back. Every `pre-edit-chain.sh`
timeout the inner watchdog wins (the expected case, since it starts before
and is bounded well under the outer budget) is named in `memidx.py stats`
output under its own `watchdog-killed` outcome inside the `pre_edit` bucket,
not folded into a generic one, and a repeated pattern (three or more within
a window) raises its own FLAG there.

Ordinary coreutils (`cat`, `dirname`, `date`, `mkdir`, …) are used freely — what
is avoided is specifically the two GNU-only binaries macOS lacks.

## Write-safety checklist for hooks and scripts

Every new hook or script that writes outside the store cites this list in its
brief. Each rule names the code that already implements it; reuse that code
rather than writing a second copy. Background lives in
[hooks and the fail-open contract](#hooks-and-the-fail-open-contract), the
post-commit re-lint and backup paragraphs there, and
[the watchdog](#the-watchdog); shell portability is in
[bash 3.2 discipline](#bash-32-discipline).

1. **Validate names before building any path.** The two values that become
   path segments are checked once, in the validation block of
   `hooks/post-commit-reindex.sh` (before the reindex, ahead of any
   project-derived path), not inside each section.
   - Project name: empty, `.`, `..`, a `/` or a control character is
     `PATH_UNSAFE`; the hook logs `skipped:bad-project` and exits having built
     no project-derived path. A name outside `[A-Za-z0-9._-]` is `BAD_PROJECT`
     only: the reindex still runs, the re-lint marker and backup are skipped.
     `scripts/repo-init.sh` refuses `.` and `..` as `--project`.
   - Backup directory: empty means off; it must be absolute, and a control
     character is `BAD_DIR`. Only the backup section is skipped
     (`skipped:bad-dir`).

   Compare paths through `mc_physical` (`scripts/mc-registry-lib.sh`) so a
   symlink cannot alias a protected path; the backup uses it to contain
   `<dir>/<project>` inside the real backup directory.
2. **A configured root must pre-exist; the hook creates only its own
   subfolder.** Never `mkdir -p` a user-configured destination: on WSL an
   unmounted `/mnt/d` would receive the files on the Linux root and the hook
   would report success. A missing directory logs `skipped:dir-missing`.
3. **Published files are created exclusively, under a per-invocation name, and
   the exclusive create is the write.** This covers the bundle
   (`<name>.part.<pid>`) and the re-lint marker (`.new.$$`): any stale file of
   that name is `rm -f`'d, then `( set -C; cmd > "$part" )` creates it.
   Never create then write, and never share a temp name between overlapping
   invocations. `MANIFEST.txt` is the other pattern, for an append-only log:
   `>>` only after checking it is not a symlink or a non-regular file.
4. **A replaced artifact is published by rename, and the rename refuses
   symlink or directory targets.** `mc_safe_replace` in
   `hooks/post-commit-reindex.sh` refuses a
   symlink or directory destination, then calls `os.replace`. It does not
   check filesystems: the caller must create the temp file beside its
   destination so the rename stays atomic. No `mv` over a path a user controls.
5. **Sweep leftovers by age and exact grammar, never by loose glob.** A temp
   file is removed only when it is older than twice the hook's watchdog budget
   (an invalid budget counts as 30 s) and its name passes `mc_backup_part_ok`;
   a fresh file from an overlapping
   run is never a candidate. Published bundles are different: the prune lists
   them with `mc_backup_bundle_list` (exact `mc_backup_name_ok` grammar, name
   order) and keeps the newest N by that order, with the bundle just written
   excluded explicitly.
6. **Never `$(( ))` on a value you did not normalise.** A leading zero is octal
   in bash (`08` crashes, `010` is misread). Keep one normaliser per setting,
   shared by every reader; `mc_backup_keep_normalize` (hook, setup and updater)
   is the model.
7. **A verdict needs a positive signal, not a zero exit.** Each lint check is
   trusted only when its own marker is present: `MC_LINT_SCHEMA_SUMMARY` for
   the schema pass, the append-only `changed=` count (`MC_LINT_CHANGED`) for
   the append-only pass (`hooks/mc-store-lint.sh`). A crash or a missing
   python is `skipped`, never `ok` and never a finding, but a crash in one
   check does not hide a genuine finding from the other. A sticky marker is
   cleared only by a genuinely clean run whose recorded root matches (a marker
   with no recorded root is cleared too).
8. **Nothing after the main job may change the exit code**, and each extra
   section logs its own `key=value` on the one `hook.log` line (the post-commit
   line format is under the per-hook logging list above).

## bash 3.2 discipline

Stock macOS ships bash 3.2.57, so every shell script here — hooks, installer,
state/decide scripts — must parse and run under it. The constructs the existing
scripts call out as unavailable, in their own header comments: associative
arrays, case-modification expansions (`${var,,}`), `mapfile`/`readarray`, and
namerefs (`local -n`).

`tests/run_bash32.sh` enforces this by re-running the hook suites under a real
bash 3.2.57 (built into `~/.cache/bash32` on first use; point `MC_BASH32` at an
existing binary to skip the build). `bash --posix` under a modern bash is not a
substitute — it does not reject bash-4/5-only syntax. Every hook subprocess call
in `test_hooks.py`/`test_write_hooks.py` goes through `$MC_BASH`, so it is the
same suite under a different interpreter.

## Code index

### Registry

`chunkers/` is a backend-neutral registry. `LANGUAGE_TABLE` holds one row per
language, and there is exactly one row per language: exclusivity is
structural, with no API to register a second backend for an existing
language. Backends are imported lazily through `get_chunker(lang)`.

Two backend kinds share the table. A native backend (`swift`, `python`) is its
own hand-written module and carries `{backend, module, extensions, shebangs,
impl_version, skip_dirs}`. A tree-sitter backend (`javascript`, `typescript`,
`tsx`, `java`, `php`, `rust`, `lua`) is ONE generic module
(`chunkers/treesitter.py`) driven entirely by each row's own data and a
per-language `.scm` query file under `chunkers/queries/` — never a
per-language Python function — and carries those same keys plus
`grammar_module`, `language_fn`, `runtime_pin`, `grammar_pin`, `query_file`,
`containers` (the ancestor-walk qualification map), `method_if_ancestor_in`,
`max_bytes` (optional, absent by default), and `doc_comment_types` (optional,
defaults to `("comment",)`; java's row sets `block_comment`/`line_comment`,
rust's sets `line_comment`/`block_comment` — a `///` doc comment is not a
distinct top-level node type in this grammar; it parses as an ordinary
`line_comment` node whose own children carry the `///` marking internally).
`typescript` and `tsx` are two separate rows
sharing one grammar module and query file but different `language_fn`s
(`language_typescript` / `language_tsx`) — `get_chunker`/`for_language` still
take `lang` alone, with no per-file dialect selection anywhere.

The public contract is `chunk_file(text, rel_path) -> ChunkResult`, where
`ChunkResult` carries `chunks`, `gaps` (`(start_line, end_line, reason)`) and
`status` (`ok` | `partial` | `failed`). Nothing backend-shaped may escape a
provider. `lang` is per chunk, so one file may emit more than one.

**Kind vocabulary** is frozen and language-agnostic: `function`, `method`,
`constructor`, `accessor`, `closure`. Swift's `func`/`init`/`subscript`/computed
`var` map into it; nothing Swift-shaped leaks into another language's search
results. Qualification is per-chunker and in-file (Python classes and nesting,
Swift's type stack); module and package prefixes are not part of
`qualified_name`, because every row carries `path` and the caller supplies it —
`billing/load.py:load` and `settings/load.py:load` are distinguished by path.

`declared_symbols` is served through the registry per language, which is how
memlint's `#symbol` vocabulary check is routed rather than forked: a fragment on
a Python path is checked against Python's vocabulary, not Swift's, with no
language branch on that path.

### `chunker_version`

`chunker_version(lang)` is the first 12 hex of a sha256 over
`backend:module:impl_version` for a native row, stored per file in `file_sha`
alongside the content sha. A file is skipped on reindex only when **both**
match.

A native row that declares `interpreter_sensitive` — the Python row does, the
Swift row does not — adds this python's own `major.minor` to that payload.
Python chunks through `ast.parse`, whose accepted syntax and node shapes move
with CPython, so the interpreter is part of that chunker the way an installed
grammar wheel is part of a tree-sitter one: without it, every already-indexed
`.py` file whose bytes did not change would be left as-is under a parser that
can now read it differently. `major.minor` only — a patch release does not move
the grammar, and re-chunking every project on a 3.12.7 → 3.12.8 bump is the cost
the stamp exists to avoid. Swift is a hand-written lexer over `re` and string
operations, with no interpreter-provided parser behind it, so its own source
fingerprint and `impl_version` already cover it. A source-sha-only skip would serve chunks from a superseded chunker
forever after a backend change — a one-way door. Bumping a row's
`impl_version` is therefore the supported way to force re-chunking of one
language's files.

For a tree-sitter row the payload is wider:
`backend:module:engine_version:runtime_pin:grammar_module:grammar_pin:installed{<dist>=<version>,…}:cap{<effective max bytes>}:query_fingerprint:row_shape:impl_version`,
where `installed{}` names the two distributions the row depends on at the
versions this python actually has, `cap{}` is the effective per-file byte cap
(both below), and `query_fingerprint` is the first 12 hex of a sha256 over the
row's `.scm` query file's own bytes.

`engine_version` is `chunkers/treesitter.py`'s own `ENGINE_VERSION`. One
generic module produces the chunks for all seven tree-sitter rows, so a
behavior change inside it — the dedup passes, qualification, kind
resolution, doc extraction, signature rendering — changes stored chunk
content for every one of those languages at once. Bumping it is the one
knob that invalidates all of them together; bumping seven `impl_version`s
by hand is the step someone forgets.

`row_shape` is `treesitter.row_shape(row)`: a sorted, stable rendering of
the row's own chunk-shaping data — `containers` (which drives every
`qualified_name`), `prefix_scopes` (which drives it for a declaration that
scopes what FOLLOWS it rather than what it holds — PHP's `namespace A;`
beside its own braced `namespace A { }`), `method_if_ancestor_in` (which
drives `kind`),
`doc_comment_types`, `max_bytes`, and `language_fn` (rendered as
`grammar_module.language_fn`, e.g. `tree_sitter_typescript.language_tsx`).
Sorted rather than as-written so reordering a row's containers does not
force a rechunk while adding, removing or repointing one does.
`language_fn` joined the payload after `typescript` and `tsx` were found to
fingerprint identically despite calling different functions
(`language_typescript` vs `language_tsx`) on the same grammar module — same
`chunker_version`, different parse behavior for the same source.

`runtime_pin` and `grammar_pin` are the **pinned** version strings
`LANGUAGE_TABLE` declares, and each is joined by the version of that same
distribution **installed in this python** — `importlib.metadata.version` of
`tree-sitter` and of the row's own grammar wheel (`tree_sitter_javascript`
ships as `tree-sitter-javascript`: one hyphen rule covers the table, so no row
carries a second name to keep in step). The fingerprint therefore moves both
when the table's pin changes and when the install underneath it drifts: a
grammar one version off the pin parses the same source into different nodes,
and a fingerprint built from the pins alone would leave every file of that
language marked current across the swap. Reading the metadata never imports
the wheel, so `chunker_version` still answers on a machine that has none — an
absent distribution contributes the fixed token `absent`, and
`BackendUnavailable` out of `get_chunker` stays the only report of a missing
wheel. `backend-preflight` names the same mismatch as `pin-mismatch` (below)
for a human, with both versions.

The **effective** per-file byte cap joins them — `max_parse_bytes(row)`, the
row's own `max_bytes` or `MEMCONTINUUM_MAX_PARSE_BYTES` or the 1 MiB default,
whichever wins — not just the row's raw `max_bytes` field that `row_shape`
already carries. The cap decides which files are chunked at all, so raising it
re-attempts the over-cap files an earlier run skipped and lowering it
re-examines the ones it accepted. `impl_version`
stays each row's own escape hatch on top of that: bumping it forces a
re-chunk of one language's files without changing what any other row's
`chunker_version` computes to, which matters the moment a chunker-behavior
fix (not a pin change) needs to invalidate only its own already-indexed
files.

`open_code_db` compares the stored schema version against the current
one. When the stored version is OLDER, the index is rebuilt on first use,
keeping its roots and languages: every derived table (`chunks`, `fts`,
`embeddings`, `file_sha`, `code_meta`, `code_schema`, `code_project`) is
dropped and recreated inside one transaction, carrying `(project,
code_root, langs)` forward into the fresh tables first — `langs` comes
from the old `code_project` when that table exists (schema v2 and later,
where langs actually lives), falling back to `code_meta`'s own `langs`
column only for a v1 db (which has no `code_project` at all and stored
langs directly on `code_meta`). `embedding_mode` does **not** survive: the
fresh `code_project` row this rebuild inserts always starts at `none`,
since an older schema tracked no such concept to carry forward — so the
first `code-search` after a rebuild reads the project as `stale` (never
`uninitialized`, since its roots and languages did survive) and, if it
heals, heals without embeddings until the background `code-embed-worker`
(or a `code-reindex` without `--no-embed`) fills the vectors. When the stored version is NEWER than this engine's
own, `open_code_db` refuses the db outright (`CodeIndexTooNew`, a
`sqlite3.DatabaseError`) rather than silently using or rebuilding it —
`code-search`/`why` fail open around that refusal (a message, no crash,
no results) instead of destroying an index a newer engine wrote.

### Tree-sitter capture convention

A query file's capture names carry the whole per-language contract, so the
generic backend never needs a per-language branch. `@chunk.<kind>` names a
span and its kind (`function`/`method`/`constructor`/`accessor`); an optional
`@chunk.name` gives the symbol; an optional `@chunk.qualifier` supplies an
explicit qualifier prefix, for a binding no lexical container names (Lua's
table/method syntax, a JavaScript object literal bound to a `const`); an optional
`@chunk.default` marks the export-default case, whose symbol and qualified
name are both the literal string `default`; an optional `@chunk.doc_anchor`
names a DIFFERENT node than `@chunk.<kind>` to look for a preceding doc
comment on, for the one shape where the kind node itself has no sibling of
its own (Lua's `M.f = function() ... end` binds the kind capture to the
anonymous function nested inside the assignment; the doc comment sits above
the assignment statement instead, so that outer node is the anchor). A match
with neither `@chunk.name` nor `@chunk.default` is dropped — the deferred,
unbound `closure` kind.

**Qualification composes; it does not replace.** A chunk's `qualified_name` is
every qualification container the ancestor walk finds above the node (the row's
own `containers` map), then the query's `@chunk.qualifier` if it captured one,
then the symbol — joined with dots. The two sources are about different things,
so a binding a query names can itself sit inside a container the walk names:
`class A { run(){ const api = { open(){} } } }` is `A.api.open`. A row that
declares no containers at all (Lua) simply contributes nothing from the walk and
the query's qualifier stands alone.

Three consequences of that rule are worth stating on their own, because each is
a name a reader will look up:

- **A container whose name is a string literal is not a container.**
  TypeScript's `module` node type spells both `module M { }`, a namespace, and
  the ambient external module `declare module "react" { }`, whose name is a
  quoted package specifier. The first names a scope; the second does not, so a
  declaration inside it keeps its own unqualified name rather than `"react".f`.
- **A JavaScript private member keeps its `#`.** `#open()` and `open()` are two
  different members of one class — the private worker and its public wrapper are
  an idiom, not a coincidence — so the symbol is `#open` and the qualified name
  `Vault.#open`. A record spells the reference `widget.js##open`: the
  path/fragment split takes the FIRST `#`, so the fragment keeps its own marker
  and resolves. TypeScript's `private m()` is a modifier on an ordinary name and
  is unaffected.
- **A bound object literal qualifies every kind it holds, not just methods.**
  An object literal is no container, so `const A = { open(){} }` needs the
  query's qualifier to become `A.open`. Its accessors and a member spelled
  `constructor` need their own bound patterns for the reason the dedup section
  below gives: kind is resolved before qualification, so a bound reading must
  meet its bare reading at the SAME kind or lose.

### Interval-taint gaps and dedup

Every tree-sitter chunker shares one gap rule. `_merge_error_intervals`
collects and merges every `ERROR` node's byte span and every `is_missing`
token's zero-width position into one sorted interval list — the taint set. A
capture becomes a gap (reason `parse-error`) when its own node's span
overlaps one of those intervals, or when its own node is a descendant of an
`ERROR` node even with no byte overlap.

Overlap is measured half-open at both ends, because that is what both spans
are: an interval `[s, e)` and a node `[ns, ne)` overlap when `ns < e` and
`s < ne`. A definition that merely TOUCHES a broken neighbour therefore
survives — `function ok(){}` immediately before a stray token keeps its chunk
instead of being dropped with it. The one exception is a ZERO-WIDTH interval, a
MISSING token: it has no width to overlap with and is judged inclusively, since
the closing brace an unterminated body lacks is reported exactly at the end of
the node it breaks and a strict test would read every such node as clean.

An interval that overlaps no capture
at all still surfaces as its own gap, derived from its raw byte offsets, so
garbage between two clean functions with nothing query-shaped nearby is never
silently absorbed. A file with any error and no surviving chunks at all is
`failed` outright, not a file of all-gaps `partial` chunks.

Tree-sitter's own error recovery can pull a syntactically clean neighbouring
definition into the same `ERROR` subtree as a genuinely broken one — observed
on the TypeScript grammar, after an unclosed bracket — so a file reported
`partial` is a cue to look at the neighbours of the reported gap line, not
only the line itself.

Two dedup passes run before gaps are computed, in this order.
`dedup_by_priority` resolves a SAME-span collision — a get/set accessor also
matching the generic method pattern, a constructor also matching it —
keeping the highest-priority kind (constructor, then accessor, then
method/function). An equal-kind tie is broken by QUALIFICATION: an entry whose
query supplied an explicit `@chunk.qualifier` is the more specific reading of
the same span, and it wins over one whose ancestor walk found no container —
which is what makes a bound object literal's `A.open` beat the bare `open` the
generic pattern reads at the identical span, rather than the winner depending
on the order the grammar completed two matches in. Kind is decided FIRST, so a
qualifier never rescues a worse kind, and a bound reading of an accessor or a
constructor has to be written at that kind to survive. Ties with nothing left
to separate them keep the first-seen entry. `dedup_nested` then
resolves a DIFFERENT-span containment — an export wrapper's outer node
capturing the same callable as the inner definition node it wraps — keeping
the innermost match and dropping an outer one only when three things hold
together: the two share a **qualified name**, they share a **kind**, and the
outer node's type is a **declared wrapper** — `WRAPPER_NODE_TYPES` in
`chunkers/treesitter.py`, which holds `export_statement` today.

That last clause is a membership test, not a difference test, and the
distinction is the whole rule. A wrapper is a *known* wrapper node type;
every other containment is a callable nested inside a callable, and both
levels are their own chunk. `function f(){ function f(){} }` yields two
chunks even though both levels carry the same symbol and the same qualified
name — a function body is not a qualification container in any row's
`containers` map, so both qualify to plain `f`. So does `function f(){ const
f = () => {}; }`, where the two node types genuinely differ
(`function_declaration` containing `arrow_function`) and the outer level is
still the one a caller imports. Qualified name rather than bare symbol is
what separates `class A { m(){ class A { m(){} } } }` into `A.m` and
`A.A.m`, two chunks with two names.

### Parse safety: a byte cap, not a timeout

A per-file byte cap bounds parse cost instead of a wall-clock timeout:
`DEFAULT_MAX_PARSE_BYTES` (1 MiB), overridable by the
`MEMCONTINUUM_MAX_PARSE_BYTES` environment variable, with a language row's
own `max_bytes` (optional) taking priority over both. The cap is checked
before a single byte reaches the parser, never during or after — a file over
it raises before parsing starts and is recorded not-indexed, retried like any
other not-indexed row on the next explicit `code-reindex` (the automatic heal
itself only retries a not-indexed row when the backend set or the chunker
version changed, not on a plain re-run); editing the file down under the cap
and running `code-reindex` again picks it back up.

A `signal.alarm`-based wall-clock timeout was tried first and measured not to
bound parse time at all: a registered signal handler only actually runs
between CPython bytecodes, and a single call into the tree-sitter C
extension does not return control to the interpreter until it finishes, so
the OS delivers the alarm on schedule but Python never observes it mid-call.
Measured directly against one large (30 MB) file: the plain, unguarded parse
took 8.65s wall time; the same file through the alarm-based mechanism raised
its "timeout" exception at 11.08s — later than the parse it was meant to cut
short, not a bound on it. A subprocess-per-file timeout was considered and
rejected too, not on correctness (it would genuinely bound wall-clock time)
but on fit: `code-reindex` is an interactive command a person can interrupt,
no hook ever calls a tree-sitter backend, and forking a process per file
would dominate a reindex's own runtime for a case the byte cap already
screens out before parsing is ever attempted.

### `backend-preflight` and grammar admission

`backend-preflight [--json]` attempts `get_chunker(lang)` for every table row
and reports one of three states by language — `ok`, `pin-mismatch`, `missing` —
fail-open per row so one backend's own import bug never hides the rest of the
report. A native row fails only on an engine bug of its own; a tree-sitter row
is `missing` when its pinned grammar wheel is not installed in this python, and
the reported reason names that wheel by module — the same underlying
missing-module reason a `code-reindex` row stamps `not-indexed` for when the
same backend goes missing mid-run (that row's own reason text carries an extra
`BackendUnavailable:` wrapper around the identical exception), so a
not-indexed reason for a tree-sitter language usually names the same module
this reports.

`pin-mismatch` is the third state: the backend imports, but the grammar wheel or
the `tree-sitter` runtime is installed at a version the row does not pin, and the
reason names the distribution with both versions (`tree-sitter-javascript:
pinned 0.25.0, installed 0.25.1`). A mismatched row is usable — `ok` stays true
in the JSON and the exit code stays 0 — so this is a report, not a gate; the
index it feeds is honest either way, because `chunker_version` hashes those
same installed versions and re-chunks the language's files. An install pointed
at an interpreter the engine does not manage (`MEMCONTINUUM_VENV_MANAGED=0`) is
where a mismatch actually appears, since a managed venv is reinstalled from
`requirements.lock`.

The state is deliberately not called `drift`. `memidx.py drift` is this
product's subcommand for decision-vs-code drift — a different question, a
different answer, and the name users already know it by — so the installed-vs-pinned
condition carries its own word rather than a second meaning for that one.

`memcontinuum-update.sh --apply` runs this check on EVERY `--apply` run (the
machine layer is reported and, when stale, refreshed by default — `--machine`
is no longer required for this), warns by name about any row missing, and
warns separately about any row off its pins. The report sits outside the
staleness gate that decides
whether the machine layer is re-rendered and the lockfile reinstalled: a
pin mismatch is a runtime-install condition — someone pip-installs a newer
grammar into the venv and nothing about the wiring goes stale — so gating the
report on staleness would have hidden exactly the case it exists to report.
The pip reinstall itself stays gated, which is what the no-op-reconciliation
guarantee is about.

Five of the six grammar wheels (`tree-sitter` itself, the seventh pin, is the
runtime, not a grammar) come from the official `github.com/tree-sitter/`
organization; `tree-sitter-lua` is the one exception, published under
`github.com/tree-sitter-grammars/`. A grammar admitted from outside the
official org clears five checks — a process rule this doc states as policy,
not something any code path enforces — before it is wired into the table: an
MIT/SPDX-clean license, an exact version pin with no floor, a wheel matrix
that covers every platform this engine ships on (verified by installing and
importing it on real Mac hardware, not assumed from a PyPI listing), a real
parse-and-query probe against a fixture rather than a bare import check, and
fixtures/goldens shipped alongside it. The same five checks admit any future
non-official-org grammar — this is the reusable rule, not a one-off
exception for Lua.

`tests/mac_smoke.sh` installs the six tree-sitter grammar wheels and the tree-sitter runtime by exact version
into a disposable venv, copies this repo's own `chunkers/` package over, and
for one real fixture per language runs it through the same
`chunkers.get_chunker(lang).chunk_file(...)` entry point the local suite
uses — proving each grammar builds a parser and query, parses the fixture on
real Mac hardware, and reproduces the exact `(kind, qualified_name)` chunk
identities `tests/test_chunkers.py` already golds for that file. It is a
parser probe, and nothing wider: not a substitute for the macOS arm64 CI job,
which installs the full hash-locked `requirements.lock` and runs the whole
unit test suite (`fastembed`/`onnxruntime` included). A green run of one says
nothing about the other, and neither reading covers what the other checks.

### Skip predicate

A language's `skip_dirs` prune only that language's own files. The walk in
`iter_code_source_files` prunes `CODE_SKIP_DIR_NAMES` — an alias of
`chunkers.UNIVERSAL_SKIP_DIRS` (`.git`, `.build`, `node_modules`, `vendor`,
`venv`, `.venv`, `__pycache__`, `.tox`, `.eggs`; the one universal noise set
every code-index walker prunes regardless of which languages are wired, or
of whether any are) — plus `chunkers.common_skip_dirs(wired)`, the
**intersection** of the wired languages' skip sets, a pure optimization, since
any file under such a directory would be dropped by its own language's rule
anyway. Every other directory is walked, and a file is dropped iff one of its
root-relative ancestor directory names is in **its own** language's set
(`chunkers.path_is_skipped_for_lang`).

Consequence, and the point of the design: with Swift and Python both wired,
`Tests/foo.py` is indexed (Python's skip set has no `Tests`) while
`Tests/Foo.swift` is not. A union rule dropped both, silently losing Python
source the census had just proposed Python on the strength of.

The census walk applies the same skip predicate. It prunes `CODE_SKIP_DIR_NAMES`
only — there is no wired language set at census time to intersect against, so
it prunes nothing narrower and nothing wider — and then drops a supported file
by the same per-file rule above: an ancestor directory in *that file's own*
language's `skip_dirs`. An unsupported extension has no language and so no
`skip_dirs` of its own; it is always counted outside the global noise dirs,
never hidden behind another language's skip set.

Worktree gap: every one of these walkers (`iter_code_files`,
`iter_code_source_files`, and the census walk) also prunes any directory
that is itself a linked worktree's own toplevel — a `.git` FILE (never a
directory) sitting directly inside it, e.g. `<root>/.worktrees/feat/`
(`memidx.py`'s shared `_prune_dirnames_for_code_walk`). A linked worktree
checked out inside a configured code root now gets retrieval and ledger
coverage (the write-side fix above); without this exclusion the SAME
`os.walk` that indexes the root's own tracked tree would also descend into
the worktree and index its (possibly different-branch, possibly
uncommitted) content as if it belonged to the root. Accepted side effect,
named rather than hidden: a git SUBMODULE checkout also has a `.git` FILE
at its own top (pointing at `<superproject>/.git/modules/<name>`), so this
same predicate also excludes a submodule's content from the code index —
no fixture or real project this repo's own tests cover currently wires a
submodule under a code root; closing that gap too, if it is ever needed,
is a separate follow-up.

### Unindexed-file tally

`iter_code_source_files` mutates a caller-supplied `Counter` in place: every
walked file not yielded because its extension maps to no language at all, or to
a language outside the wired set, is tallied by extension. Keys are
compound-extension aware (`chunkers.extension_of`: `foo.blade.php` counts as
`.blade.php`, never `.php`), and an extensionless file with no recognized
shebang is tallied under `NO_EXTENSION_BUCKET`. A file dropped by its own
language's skip set is **not** tallied — that is deliberately-pruned noise, not
a blind spot.

That Counter is the data behind `code-reindex`'s end-of-run provenance line. The
rule it enforces: no growing blind spot may be silent.

### Census and consent

`code-census --root DIR [--json]` counts source files by extension, applies
compound-extension rules, and reads the first line of extensionless files for a
recognized shebang stem. It is documented exit-0-always: a missing or unreadable
root is not an error, it just walks nothing — the result is every table language
at zero and no unsupported-extension rows at all.

The JSON always seeds **every** table language at zero, whether or not the tree
holds one of its files, so a consumer can present three categories without
re-deriving the known-language list: `status: "supported"` with `files > 0`
(proposed), `status: "supported"` with `files == 0` (supported but not found),
and `status: "unsupported"` (the key is the extension, or `"(no extension)"`).

`repo-init.sh` runs the census across every `--code-root` given, merges the
counts, and offers four answers on a tty: skip (language-less wiring), enable all detected,
select from detected, or never-mention-this-extension. The fourth records
extensions onto the nudge hook line (`MEMCONTINUUM_NEVER_EXTS`) and does not
change which languages are enabled.

On a real (non-dry) run, `repo-init.sh` verifies each `--code-root` exists
itself before running any census, rather than inferring a missing directory from
a census that walked nothing and proposed nothing. Under `--dry-run` that check
is skipped — a preview may name roots that do not exist yet — and the run
proceeds as "nothing proposed". Either way, a census that fails, prints
non-JSON, or prints JSON that is not an object is a hard error, never a silent
empty result.

Non-interactive use: `--langs LIST` (wins over `--non-interactive`; an unknown
name fails with the list of known languages), `--never-ext LIST`,
`--non-interactive` (language-less wiring, initial `code-reindex` skipped).

The dialogue is only reached when the census proposes at least one supported
language: an empty tree, or one holding nothing this engine can chunk, wires
language-less with no prompt and no error. When there *is* something to propose
and stdin is not a tty, `repo-init.sh` exits 12 with a message naming the driven
flow rather than hanging on `/dev/tty` — an agent has no tty, so the skill runs
the census itself, presents it, and re-runs with `--langs`.

`--lang` is required on a project's first `code-reindex` (exit 1) and reused
from `code_project.langs` afterwards; there is no hardcoded default. The
initial code-reindex in `repo-init.sh` hard-fails the install on a non-zero
exit (exit 13, distinct from the decision-store reindex's exit 7): per-file
handling already fails open, so a non-zero exit there is structural.

### Code embeddings: the background worker

The code index used to get vectors only from a hand-run `code-reindex` without `--no-embed`; the
first `code-reindex` repo-init runs is `--no-embed` (the install stays fast), and every heal is
text-only, so a live code index had zero vectors forever. The code side now has the same lifecycle
the store has (`embed-worker`, see [Decision index provenance and embedding
lifecycle](#decision-index-provenance-and-embedding-lifecycle)); both workers share one
lock/marker/loop skeleton, `_run_embed_worker_loop`.

- **`memidx.py code-embed-worker --project P --root DIR [--root DIR ...] [--db DB]`** takes a non-blocking `flock` on
  `<db dir>/<project>.code-embed.lock` (held: exit 0), loops while `<project>.code-embed-pending`
  exists, and per pass calls `cmd_code_reindex(no_embed=False, full=False, retry_not_indexed=False)`
  for every root in `code_meta` that is ALSO named by a `--root` (resolved paths compared) and exists
  on disk. The wiring is the only consent for a root and `code_meta` is derived state: a recorded root
  the caller did not name is stale and is skipped, and a `--root` that is not recorded is ignored (the
  refresh hook's `unrecorded=` covers it). Without any `--root`, in the worker form and in `--spawn`
  alike, the worker refuses with one stderr line naming the flag and exits 2; it never falls back to
  "every recorded root" (a retired root left in `code_meta` would be reindexed and embedded
  beside the new one, every symbol twice). That call's unchanged-file top-up already
  finds every chunk without a vector at the current fingerprint (a missing vector and a stale one
  look the same), so nothing else looks for stranded chunks. The marker is claimed FIRST, by
  renaming it to the per-run name `<marker>.claim.<pid>` (after an `lstat` that refuses a symlink
  or any non-regular marker): a touch before the rename is inside the claimed request (the pass
  derives its work from coverage), a touch during or after the pass creates a new marker inode the
  next `lstat` sees, a producer's path-based `utime` that loses the race to the rename fails and
  recreates the marker, and after the lock is released the marker is checked once more (taking
  the lock again if it reappeared), so no request is deleted unseen. A clean pass deletes the
  claim; a failed pass re-raises the request by touching a fresh marker (never by renaming the claim
  back: nothing is ever renamed onto the marker path, which is only created or touched through the
  O_NOFOLLOW touch or renamed away to the claim) and deletes its verified claim; an inspection
  failure or a non-regular claim also re-raises the request but leaves the claim; a worker killed
  mid-pass leaves `<marker>.claim.<pid>`, swept when older than an hour (exact name grammar,
  regular files only), and the next hook preflight recomputes the awaiting count and raises the
  marker again. A claim rename error other
  than ENOENT exits 3 at once instead of spinning. An exception, a failed-open embedding backend
  (`code_embedding_backend_failed()`) or a code-reindex rc of 1 or 2 leaves the marker, appends the
  traceback or reason to `<project>.code-embed.log` and exits 3; each pass also logs its summary
  line there. It does not take the code-refresh lock: the model loads before any write, the text
  pass commits before the first embed call, and each batch is its own short `BEGIN IMMEDIATE`
  whose `INSERT … SELECT … WHERE EXISTS (chunk)` is atomic with the existence check, so a
  concurrent heal cannot leave an orphan vector (ids are `AUTOINCREMENT`, never reused). A final
  sweep of every worker pass deletes any orphan that still got through and logs the count, and
  `code_hits_vector` inner-joins `chunks` so an orphan can never take a result slot. The worker's
  log, marker and lock are opened through one helper (`_open_aux_file`: `O_NOFOLLOW`,
  `O_NONBLOCK`, `fstat` must be a regular file); a symlink or FIFO is refused (`embed=failed
  reason=log|marker|lock`; the worker exits 3 with the reason on stderr).
- **Batching.** The embedding step of `cmd_code_reindex` embeds `CODE_EMBED_BATCH` (128) chunks at
  a time and commits after each batch (vectors, the fingerprint, and `embedding_mode`), so a killed
  worker keeps its progress and a first run over thousands of chunks never holds one multi-minute
  transaction. The model is loaded once per pass.
- **Three modes.** `code_project.embedding_mode` is recomputed from real project-wide coverage at
  the end of every `cmd_code_reindex` pass, embedded or not (`_code_embedding_coverage`): `full` =
  every chunk has a vector at the current fingerprint, `none` = none does, `partial` otherwise. A
  `--no-embed` pass that adds chunks to a `full` project therefore reads `partial`, and the worker
  restores `full`. A pass with a loaded model compares the exact fingerprint; one without compares
  the static prefix (model, dim, pipeline, … up to `;revision=`), so a vector made by another
  model or pipeline never counts. The summary line ends with `, N chunk(s) awaiting embedding`
  (the store's grammar), `code_index_report` carries `awaiting_embedding` and `chunk_total`, and
  `code-search --json` exposes `embedding_mode` and `awaiting_embedding`. `code-search` in vector
  or hybrid mode prints one stderr line when chunks lack a vector (`N of M chunks have no vector;
  the background embed worker fills them`) and ranks over the vectors present: `code_hits_vector`
  only reads rows at the current fingerprint, so an FTS-only chunk still ranks through the FTS
  channel of the fusion in `hybrid` mode, while pure `vector` mode ranks vectors only and cannot
  reach it (its notice says `use --mode hybrid to reach them`). `code-search` never spawns the
  worker.
- **Spawn sites.** `spawn_code_embed_worker` and the store's `spawn_embed_worker` (reached by
  `post-commit-reindex.sh` as `embed-worker --spawn`) both go through `_spawn_embed_worker`, the
  repo's one `Popen` site: raise the marker (create it, then `utime` it BY PATH, retried up to 3
  times if a worker claimed it in between, so a running worker loops again) and start the worker with a detached python `subprocess.Popen` (`start_new_session=True`,
  stdin `DEVNULL`, stdout and stderr to the worker log, `close_fds`), never bash `&` or `setsid`.
  It ALWAYS spawns when the marker is raised (a worker that finds the lock held exits 0 at once;
  probing the lock first could leave a request waiting while a worker was finishing). It returns
  `spawned`, `off` (`MEMCONTINUUM_EMBED_WORKER=0`: the marker is still raised) or `failed
  reason=<marker|log|spawn|bad-project>` (`bad-project`: the name is empty, `.`, `..`, or holds a
  `/` or a control character; the workers exit 2 on it). Callers: `scripts/repo-init.sh` runs
  `code-embed-worker --project P --spawn --root DIR...` (every `--code-root` it indexed) once after its
  last `--no-embed` code-reindex, and `code-refresh` calls it, handing over the hook's wired roots
  (`--root` each; a hook naming none spawns nothing and logs `embed=off reason=no-wired-roots`), on BOTH events whenever `awaiting_embedding > 0`, after the preflight and
  independent of the drift check (a current index with zero vectors is filled too); on `Stop` it
  looks again after the heal, which adds unembedded chunks. This is how an existing index with no
  vectors fills itself on the first `SessionStart` or `Stop` after the upgrade. The hook budget is
  untouched: a spawn costs milliseconds.
- **The model path.** `memidx._text_embedding()` is the one `TextEmbedding` construction site. It
  registers `memcontinuum/bge-small-en-v1.5-fp32` once per process (CLS pooling, normalised, 384
  dimensions, `onnx/model.onnx` from the HF repo `BAAI/bge-small-en-v1.5`) instead of fastembed's
  default name, which resolves to the quantised `qdrant/bge-small-en-v1.5-onnx-q` build whose
  MatMul (89 % of its profile) does not parallelise: 0.3-0.4 s per text at 1 or 24 threads on a
  24-thread Ryzen 9900X. The fp32 file gives cosine 1.0000 against it on real chunk texts and runs
  at 37-44 ms per text with 8 or more threads (about 8 times faster; 2.5 minutes instead of 22 for a
  3,300-chunk index; a worker pass over a 320-chunk temp index took 23 s). Stored vectors stay
  semantically valid (the parity test, `TestEmbedModelParity`, asserts cosine ≥ 0.9999), but the
  fingerprint names the pipeline, so it changes and every old vector is re-embedded once by the
  workers: `reindex --no-embed` counts those store records, and `code-reindex --no-embed` those
  chunks, as awaiting embedding. `requirements.lock` is unchanged (same fastembed, same
  onnxruntime); CI caches the new snapshot under `fastembed-model-v2-…`.
- **Cold-start cost in hooks.** A hook that reaches the vector channel pays python start, the
  fastembed import (0.26-0.65 s measured) and the model load (0.17-0.26 s for the 133 MB fp32
  file, 0.12-0.22 s for the 67 MB quantised one; no network when the model is cached). On a 3-vCPU
  macOS CI runner that crosses the pre-edit hook's 2 s budget, so that job runs the hooks' search
  fallback in fts mode (`MEMCONTINUUM_FALLBACK_MODE=fts`). Options for step 7: a pre-optimised or
  serialised ORT session, a resident embedder, or the quantised file for single-query embedding
  (identical vectors) with a fingerprint that names the logical model.

### Code-index freshness

`hooks/code-refresh.sh` (`Stop` and `SessionStart`) keeps an existing code index current. It owns
no staleness logic: `memidx.py code-refresh` calls the preflight every reader already uses,
`code_index_report` (five stat signals, sha-256 on drift, the git HEAD trigger), and, on `Stop`
only, `heal_code_index` with `caller="code-refresh"` and `lock_held=True`. The heal is the same
incremental text pass `code-search` makes (`no_embed=True` always,
`retry_not_indexed=False`, `full=False`, rc checked); embeddings are not part of it. A deleted
file counts toward `changed` (`_root_report` adds `removed` into it), so a deletion alone makes
the heal eligible and purges the file's rows.

`heal_code_index` returns `(report, conn)` as before. Passed a `result` dict it fills in `outcome`
(`ineligible`, `over-limit`, `locked`, `reindex-failed`, `healed`, `error`), `rc`, `exc`,
`reindexed` and `messages`, and then prints nothing; without one, its messages go to stderr
prefixed by `caller` (default `code-search`).

Mutual exclusion is a non-blocking `flock` on `<home>/<project>.code-refresh.lock`
(`_acquire_code_refresh_lock`, opened `O_NOFOLLOW`). The hook holds it for its whole run, preflight
included, because the preflight may commit stat-cache refreshes; `code-search`'s heal takes it for
the duration of the heal and skips with a one-line notice when it is held.

`<home>/<project>.code-stale` is the honest-staleness marker: `reason=<r> since=<iso> roots=<n>`.
`refreshing` is written before a `Stop` heal and removed after a clean one (rc 0 and nothing left
to repair, and the report is `current` or `metadata-current`); a failure, a `--heal-limit` refusal
or a fail-open exception rewrites it with the reason; a watchdog kill leaves `refreshing`
(per-file savepoints, nothing corrupt); a preflight that finds nothing to repair removes it only
when the report is `current`/`metadata-current`. A `degraded` report (a recorded root is missing,
a file is not-indexed) keeps or writes `degraded missing-root=<n> not-indexed=<n>`
(`outcome=skip reason=degraded`, or `failed reason=degraded` after a heal that left it so). If the
`refreshing` marker cannot be written the heal does not start (`skip reason=marker-unwritable`).
`SessionStart` writes `stale changed=<n>` and never reindexes.
**Stale roots.** When the hook names roots (`MEMCONTINUUM_CODE_ROOTS`), a `code_meta` root
they do not name is stale. `Stop`, inside the refresh lock and before the report, drops each one with
`drop_code_root` (the deletion `code-reindex --drop-root` uses; one `BEGIN IMMEDIATE` transaction per root, its snapshot read under the write lock), then
recomputes `embedding_mode` from real coverage, and logs `dropped=<n>` plus
`detail=dropped-roots:<path>;<path>`. `SessionStart` never deletes: it writes the marker
`stale-root=<n>` (`skip reason=stale-marked`) and the next `Stop` clears it by dropping. The heal
(`heal_code_index(wired_roots=...)`) and the embed worker skip such roots meanwhile. A hook that names
no roots has no opinion: nothing is dropped or skipped. Two guards withhold the drop for that run (`detail=stale-check-skipped:<reason>`): every wired root must be absolute and strictly resolve to a directory (else `relative-wired-root`, `unresolvable-wired-root` for a missing path or symlink loop on every Python version, or `wired-root-not-a-directory` for a path that resolves to a file), and at least one wired root must be among the recorded roots (else `no-wired-root-recorded`: wiring and index disagree entirely). Filtering by the resolved set still applies. Both sides are compared resolved, so a recorded alias of a wired root is never stale. The worker's text pass passes `require_recorded`: inside its write transaction it refuses a root whose `code_meta` row is gone (a Stop dropped it while the worker was loading the model) and writes nothing. `code-search`, which has no wiring in hand,
still heals every recorded root. A code db older than the engine's
schema (an unversioned one included) is never opened for rebuild by either event: the marker
is `stale schema-older-than-engine` (`skip reason=stale-marked`) and the rebuild stays with
`code-reindex`. `sessionstart-remind.sh` shows the marker as one line next to the re-lint marker.

A root with no `code_meta` row is counted (`unrecorded=`) and left alone: the first index of a
root remains an explicit `code-reindex --lang`. No git `post-commit` hook is installed in code
repos: a commit made in a session is caught by the next `Stop`, one made outside any session by the
next `SessionStart`, and the index can lag by the commits made between two sessions.

**Walker and `.gitignore`.** `_iter_code_tree` is the one enumerator under `iter_code_files`,
`iter_code_source_files` (and so `code-reindex` and `_root_report`) and `code_census`. Inside a git
work tree it lists `git ls-files -z --cached --others --exclude-standard` (one call; timeout `MEMCONTINUUM_GIT_LS_TIMEOUT`, default 5 s, which `code-refresh` lowers to half
its own watchdog budget, minimum 0.5 s)
and applies the same noise-directory and nested-worktree pruning plus an existence check
(`--cached` lists a tracked file deleted from the work tree); git missing, timing out, failing, or a
root that is itself a `.gitignore`d directory of a parent repo falls back to the `os.walk`
unchanged and says so once on stderr. Because both the report and the reindex use it, ignored
content an older walk indexed shows up as `removed` and is purged by the next refresh.

### Root-scoped indexing

A project has one language set (`code_project.langs`, `code_project.embedding_mode`)
shared by every code root it indexes, and one row per root
(`code_meta(project, code_root)`, holding that root's `last_indexed_at` and
`head_sha`). `head_sha` is a git-diff TRIGGER, not proof by itself: it names
the HEAD this root was last hashed clean against, and refreshes only once
every file that HEAD's diff touched verifies (below). `chunks` and
`file_sha` carry `code_root` in their key, so a project can index several
trees at once without one root's rows colliding with another's.

`code-reindex --code-root DIR` touches only that root: it queries, writes and
deletes `file_sha`/`chunks` rows scoped to `(project, code_root)` alone, and
never sees another root's rows at all — running it once per root, as
`repo-init.sh` does, indexes every root given, and repairing one root cannot
strand another. Root validation runs *before* `open_code_db`, so a missing or
unreadable `--code-root` neither creates a db file nor triggers the schema
rebuild above; it prints the fix (`code-reindex --drop-root DIR` if the root
is gone for good) and exits 2. `code-reindex --drop-root DIR` removes one
root's `code_meta`/`file_sha`/`chunks` rows outright — nothing is walked, so
it works even when `DIR` no longer exists on disk.

**File status.** Every indexed file's `file_sha` row carries a `status`:
`ok` (chunked cleanly), `partial` (chunked with at least one warned gap,
`gap_count` > 0), `failed` (the chunker raised or reported `status=failed` —
`sha256` and `chunker_version` are stored, so the row is skipped until the
source or the chunker version changes, or `--full`; this is the
**deterministic** bucket — retrying it without a code or chunker change would
just fail again), or `not-indexed` (`sha256` is NULL — a backend unavailable
on this machine, a permission error, or any other exception this engine did
not itself validate; this is the **retryable** bucket, and each such row
stamps `attempt_key` with the backend-availability fingerprint
(`chunkers.backend_availability()`) at the moment it was written). A
`not-indexed` row is retried when the run is an explicit `code-reindex`
(the default), `--full`, or its stored `attempt_key`/`chunker_version` no
longer matches the current one — never on an unrelated edit elsewhere in the
tree with nothing about the backend or the chunker having changed, which is
exactly the situation `code-search`'s heal (below) runs in.

Writing a `failed`/`not-indexed` row is itself guarded: each file's whole
write (the chunk INSERTs, or the failure-path purge-and-stamp) runs under its
own savepoint, released on success and rolled back on any failure — so a
mid-file exception can never leave a partial write committed alongside the
files before and after it. If the purge or the status write ITSELF fails
(disk error, a locked db), the savepoint rolls back that attempt too — the
file's previous chunks and status row are left exactly as they were, nothing
is half-updated — `code-reindex` prints `cannot purge stale rows for <path>
(<Type>: <msg>); index integrity not guaranteed`, counts it as an integrity
failure, and continues with the next file; every file that DID complete
cleanly is still committed. A run with one or more integrity failures exits
**5** and its summary line gains an `N integrity failure(s)` token.

**`code_index_report(project)`** is the preflight both `code-search` and the
heal consult, one entry per recorded root. States, in order: **uninitialized**
(no `code-reindex` has ever run for this project — no `code_meta` rows at
all); **stale** (some root has files that changed, were removed, or moved to
a new chunker version since the last `code-reindex` — a removed file counts
as changed too, and `changed` here means CONTENT changed: a stat-signal
match or the git trigger proved it, never metadata alone); **degraded**
(nothing changed, but some root has a `not-indexed` file, a
backend-availability change since a `not-indexed` row was stamped, or a
recorded root missing on disk — a missing root can never read `current`);
**current** ONLY when `--verify-content` hashed every file in every recorded
root during THIS call; otherwise **metadata-current** — the honest default:
the stat signals (and, when it fired, the git trigger) found nothing, but
nothing was proven by a full hash either. `failed` files are never part of
this state calculation at all — an index with only `failed` files still
reads `metadata-current`/`current`, and `code-search` reports the failed
count as its own separate line. The preflight is otherwise read-only: the
one write it may commit is refreshing a drifted-looking file's stored stat
signals once its content turns out unchanged (sha256 still matches) — cache
bookkeeping so the same file isn't re-hashed on the next call, never a
chunk, status, or meta row.

**Stat signals and the git trigger.** A file's freshness gate does not stop
at `chunker_version` or a bare mtime/size comparison: `file_sha` stores five
signals from ONE `os.stat()` call — size, `mtime_ns`, `ctime_ns`, inode,
device — and any one differing from the stored row triggers a content hash
(a row written before this signal set existed has every new signal NULL,
which reads as differing, so the very first report after the upgrade hashes
once and fills them in). A same-size, same-`mtime_ns` rewrite — a
metadata-preserving restore, a coarse-timestamp filesystem, some sync tools
— moves `ctime`/inode/device regardless; userspace cannot fake those, so the
signal set catches it where mtime/size alone could not. After the stat
pass, when a root's stored `head_sha` differs from its current git HEAD,
the commit's own changed paths (`git diff --name-only`, remapped
root-relative via `git rev-parse --show-prefix`) are hashed too, regardless
of what the stat pass already decided — a TRIGGER, not proof on its own: a
path the diff never touched is never hashed by it, and a non-git root, a
missing `head_sha` (never reindexed inside a git repo), or any git failure
or timeout (a 3-second budget per report call, `git rev-parse HEAD` alone
capped at 2 seconds) leaves this signal off without blocking the report.
`head_sha` refreshes only once every one of those diffed, in-root,
still-source paths verifies clean.

**Heal.** Before answering, `code-search` consults the report and, unless
`--no-heal` is given, may repair it once: eligible only when the state is
`stale` or `degraded` **and** there is something to actually fix (some root's
`changed` count is above zero, or availability changed since a `not-indexed`
row was stamped) — a report that is `degraded` only because a root is
missing, or only because a `not-indexed` file's backend is still unavailable,
is left alone, since nothing about running `code-reindex` again would change
either. `--heal-limit N` (default 500) caps the total `changed` count the
heal will attempt; above it, `code-search` prints the count and tells the
caller to run `code-reindex` instead. Within the cap, every recorded root
that still exists on disk is reindexed once, in-process, with the
retry-not-indexed rule above turned off (so an unrelated edit never retries a
known-broken backend) and `--full` never set (a heal repairs drift, it does not rewrite
the project's language set); it is always text-only (`--no-embed`). This
amends Anatomy M2a binding point 2, which had the heal follow the project's own
`embedding_mode` (embed only when `full`) so that a heal could never silently
leave a `full` project's new chunks unembedded. Now `embedding_mode` is
recomputed from real coverage after every pass, so such a project is simply
reported `partial` and the background `code-embed-worker` restores `full`
minutes later (and the heal reindexes only recorded roots the hook's wiring still names when
it is given one, `wired_roots`, so a retired root is never revived); an inline embed of a 300-chunk drift (about 13 s on the
quantised model) would be watchdog-killed on every `Stop` with no way out.
`code-search` prints `index healed` only when EVERY in-process
`code-reindex` call it ran exited 0 AND the state after healing reads
`current` or `metadata-current` (both mean `changed == 0` — the only
difference is whether this call proved it with a full hash) — a non-zero
exit (an integrity failure on some root) prints `heal did not complete
(code-reindex exit N); answering from the current index` instead, never
"healed" over it. Any exception during the heal is fail-open — the original
report stands and the search still answers from whatever was already
indexed.

**Code versus test (role).** `chunkers.chunk_role(rel_path, lang)` returns
`"code"` or `"test"` from a root-relative path and the language alone. The
conventions are data: each `LANGUAGE_TABLE` row's `test_markers` (directory
names, directory globs such as swift's `*Tests`, multi-part directory
substrings such as java's `src/test`, and basename globs), matched
case-sensitively against the directory parts and the basename only, never the
code root's own name (same root-relative discipline as
`path_is_skipped_for_lang`). It is computed at query time: no schema column, no
reindex, and `chunker_version` does not read `test_markers`, so adding or
editing a marker invalidates nothing. `cmd_code_search` runs both channels and
the fusion unchanged, then filters by role while it builds hits in rank order
and stops at `--limit`: default `code`, `--tests` for tests only, `--all` for
both (mutually exclusive); the exact-symbol-first path of `code_hits_fts` is
filtered the same way. The reason for filtering rather than re-ranking: a
measurement on this repo's own index found most chunks (about 86 percent) are
test chunks, but that did not make tests outrank code, so the change is scoped to
what the caller sees. `code_tested_by` gives each code hit up to three test
chunks (distinct paths) found by one FTS query on the hit's symbol over at most
50 rows, role-filtered in Python through the same `chunk_role`; symbols under 4
characters or `main`/`init`/`__init__`/`test`/`setUp` get no lookup. "Tested
by" matches a test chunk whose qualified name, signature, doc or indexed body
excerpt (the first 25 lines of each chunk body, stored in the FTS `body` column)
names the symbol. `--tests`/`--all` select among chunks already indexed; Swift's
`skip_dirs` still prunes directories named `Tests` and `Resources` under a code
root, so a Swift project's test files reach the index only when a `Tests`
directory is wired as its own code root (its own name is never consulted by the
skip rule). JSON hits gain `role` and, for code
hits, `tested_by` (a list of `{path, qualified_name}`); the envelope gains
`role_filter`; text output prints `[test]` on test hits and an indented
`tested by:` line.

**Multi-root output.** `code-search --json` wraps hits in an envelope:
`state`; `code_root`/`indexed_at`/`head_sha`, naming the first recorded root
alphabetically (`indexed_at` here is that root's `last_indexed_at`, renamed
at this top level only); `code_roots`, the full per-root report list (each
entry carries its own `last_indexed_at`/`head_sha`/`git_delta`/`verified`,
not renamed); `changed`, `failed`, `not_indexed`, `embedding_mode`,
`awaiting_embedding` (chunks with no vector at the current fingerprint); and
`results`. `--verify-content` hashes every indexed file before answering, so
the reported state is content-proven (`current`) rather than the
metadata-only default (`metadata-current`) — there is no automatic
verification on an empty result; the caller asks for proof explicitly. A
`code_roots` entry's `git_delta` is `unavailable` (no stored HEAD to compare,
or git failed/timed out), `unchanged` (HEAD hasn't moved), `verified` (HEAD
moved, every diffed path in this root proved clean), or `changed` (HEAD
moved and the diff hashed a real change). A nothing-found result is evidence
only under `current` — under `metadata-current` it is honest uncertainty,
not proof of absence. In plain output, a hit's location is qualified with
its root (`root/path:line`) only once a project has more than one recorded
root — the common single-root case keeps its plain `path:line` line, since
only a multi-root project can have the same relative path indexed under two
roots at once.

The write-side hooks see every code root. `ledger-post-edit.sh` checks an
edited path against each configured root and stamps the ledger row with the
physical root it matched (`""` for a store-root row) plus `source: "tool"`;
`userprompt-remind.sh`/`precompact-persist.sh` classify the ledger's code
paths with one `unmapped` call carrying every root, and compare each root's
current git HEAD against the session's own start-of-session map — "code HEAD
changed" is true when any root moved. `newfile-nudge.sh` stays the one
per-root **lifecycle** hook: it gets its own wired entry, with its own
`MEMCONTINUUM_CODE_ROOT`, for every `--code-root` given — the other five
write-side hooks fire once per event regardless of root count, never once per
root.

## memlint

`memlint.py ROOT [--code-root DIR ...] [--strict-citations]` imports memidx's own walker, so a
session buffer is never linted as a topic, the walker's no-symlinks rule
applies here too, and reuses
`memidx.fragment_declaration_status` — the same predicate `code-search` uses for
concept attachment at runtime — rather than a from-scratch regex, so a
`#symbol` fragment validates exactly the way attachment accepts it, comments and
string literals already masked out.

That predicate is tri-state, and the severity table below turns on which state
it returns. True and False are the backend's own answer about the text. The
third state means the chunker backend for that file's language does not run in
this python — an optional tree-sitter grammar wheel the interpreter lacks — so
the symbol is neither proven present nor proven absent. A record stays valid
across that gap: `memlint` emits a warning naming the missing wheel, and
reserves its error for a symbol an available backend proves absent. Every other
surface fails open on the same gap (`code-reindex` records the file
not-indexed, `code-search` reports the index incomplete, `backend-preflight`
reports `MISSING`); a lint error there would make an optional dependency
mandatory in one place only.

Parse-level rules — every record, before the type-specific rules below even
run (see "Tolerant parsing and quarantine" above for the full mechanism):

| rule | severity |
|---|---|
| frontmatter does not parse (unreadable file, not UTF-8, unterminated block, malformed YAML on a canonical record) | error, naming the file and the failure |
| a typed field is the wrong shape (`links` not a list of mappings, a link missing its `link` id, `ruling`/`rationale`/`invariant` not a mapping, `tags`/`code_refs`/… not a list of scalars) on a canonical record | error, naming the field |

A note's own parse diagnostics (malformed YAML recovered leniently, an
unterminated frontmatter block) are warnings instead — a note has no chain
shape to protect, and the rule pass below is skipped only for a record these
parse-level rules flagged as an error, never for a mere warning.

Topic-chain rules:

| rule | severity |
|---|---|
| `ruling.authority` is `owner-verbatim`/`owner-ratified` but `ruling.text` and/or `ruling.source` is missing | error |
| `status: superseded` with no `superseded_by` | error |
| `reverses:` set with no `reason_for_change` | error |
| frontmatter `current:` does not equal the newest link with `status: active` | error (names the correct value) |
| a topic in area `processing/*` or `deletion/*` has no `code_refs` | warning |
| a topic's `code_refs` entry is empty (`""`) or fragment-only (`"#Foo"`) — names no path | error |
| a `status`/`authority` value outside the schema enums | error |
| a `kind` value outside the schema enum, or an `owner-verbatim` `ruling.text` ending in `?` (active/provisional) | warning in plain mode for every link (earlier links stay committable); error under `--against-ref` for links absent at REF (all links of a new file) — `_new_link_errors` is the one definition |
| an edge `rel` outside the seven enumerated relations | error |
| a `standing:` entry names a link id not in that topic | error |
| a `standing:` entry's link is not `status: active` | error — even a valid successor does not clear it; the supersession and the pointer's own update/removal must land in the same commit |
| a `standing:` entry's link's `ruling.authority` is not `owner-verbatim`/`owner-ratified` | error |
| a `standing:` entry's link's `ruling.text` contains a raw CR or LF (a YAML block scalar, most often) | error — flatten it to one line. Any OTHER line/paragraph separator (U+2028, U+2029, NEL, a tab) is not an error here — `standing_line` collapses it to one space at projection time |
| the store-wide `standing:` set (every topic's pointers together, in the projection's own order) exceeds 24 links or 4,800 bytes (UTF-8) of the complete digest (header, every line, and the newlines joining them) | error (corpus-wide; names every topic whose pointer sits past whichever line is crossed first) |

Concept-record rules (`type: concept` files). `--code-root` is repeatable —
one project can have several code roots, and every root given is checked:

| rule | severity |
|---|---|
| (`--code-root`) an `implemented_by`/`tested_by` path is absolute | error |
| (`--code-root`) a relative path escapes every code root given (a `../` that walks outside all of them) | error |
| (`--code-root`) a path does not exist under any code root given | error (names every root tried) |
| (`--code-root`, several roots) a path exists under more than one code root | error (one reference must name one file) |
| (`--code-root`) a `#symbol` fragment matches nothing the chunker recognizes in that file | error |
| (`--code-root`) a `#symbol` fragment on a file whose language backend does not run in this python | warning (names the missing grammar wheel) |
| (`--code-root`) `implemented_by` with no `#symbol` fragment on a file over 400 lines | error |
| `governed_by` names a topic id not in the linted corpus | error (only when the corpus has at least one topic) |
| two concepts claim the same `implemented_by` `path#symbol` | error (corpus-wide; `tested_by` excluded — sharing a test file is fine) |
| a concept has no `tested_by` | warning, unconditional |
| a concept body has no "not this concept" sentence | warning |

Decision marker rules (`--code-root`; SCHEMA §2/§8.3). A marker is a comment line matching
`decision: TOP-\d+ Ln` (any positive integer id — SCHEMA's own running example is `TOP-42`, not a
fixed four digits) on a symbol's own definition line, or within the (up to) three lines above it —
located through the chunker registry, same as the concept rules above; a language whose backend
cannot run here warns rather than errors, while a language with no wired chunker at all is scanned
by plain regex instead (see the no-chunker row in the table below). In a file WITH a chunker, only
a topic's `path#symbol` code_refs entries take part; a prefix or a glob names no symbol there and
is never marker-verified.

The window is searched nearest-first (the definition line itself, then one line up, then two,
then three) and never crosses into another declaration's own line — two adjacent short
declarations with no body between them each keep their own window, never borrowing the other's
marker. Every marker actually inside the window is examined, not just the first one found: a
valid marker followed, farther up, by a bogus one still errors on the bogus one, and two markers
in one window (a member two independent topics each constrain) each satisfy their own topic.

The scan does still visit every directory under a code root (there is no way to know which
subtrees a glob/prefix code_ref might reach without looking), but a file is opened only after its
FILENAME already matches at least one topic's code_refs (by any of the three forms) — the
scan never opens a file nothing references, even to check whether it is binary.

| rule | severity |
|---|---|
| a marker names a topic id or link id that does not exist | error |
| a marker names a link that is not `active`, or whose tier (§4) is CONTEXT, not CONSTRAINT/HOLD | error |
| in a file WITH a chunker, a marker's topic has no `path#symbol` code_refs entry naming this exact file and symbol — either no entry names this file at all (a prefix or glob that merely matches the FILE does not count), or one does but names a DIFFERENT symbol at it | error, naming the symbol the code_refs entry actually names when there is one — the no-chunker row below is the file-level-only counterpart of this rule, where a prefix/glob DOES count (there is no symbol to hold it to a stricter standard) |
| a marker's topic names a `path#symbol` that matches no chunk anywhere in the file, but the chunker confirms the symbol IS declared (a container — class/struct/enum/… — chunk_file never gives one its own chunk) | silent on this side (never misattributed to a nearby member); direction 2's own "container type" row below still warns |
| an active CONSTRAINT/HOLD link's `path#symbol` ref finds no marker at that symbol | warning |
| an active CONSTRAINT/HOLD link's `path#symbol` ref names a symbol the chunker proves absent | error (the ref itself is dangling) |
| a file in scope whose backend cannot run here (a missing optional grammar wheel, or a genuine chunking failure) | warning (names the reason), never an error |
| a file in scope whose language has no chunker at all | not a failure — scanned for a `decision:` marker by plain regex alone (no chunk-derived window); silent when it holds none. When it does, checked against every marker→store rule above that needs no symbol location (topic/link exist, active, CONSTRAINT/HOLD, code_refs name the file — the code_ref's own `#symbol` fragment, if any, is stripped before this comparison, so it counts exactly like a bare path or glob); a violation there is still the matching error, and clean is ONE warning that the marker cannot be attributed to a symbol — never the old blanket per-file warning fired regardless of whether the file held a marker at all |
| a `path#symbol` naming a container type (class/struct/enum/…) the chunker reports no definition line for | warning (uncheckable, not "no marker") |

Cited-commit / file:line citation rules (`--code-root`; SCHEMA §7 "Cited
commits and paths are verified, not merely written"). Checked in a link's
`ruling.source`/`evidence`, or a standalone record's top-level
`source:`/`evidence:`. `--strict-citations` (rejected together with
`--against-ref`, same as `--code-root`) is the one flag that changes a
severity below rather than adding a new rule:

| rule | severity |
|---|---|
| a recognized commit citation does not resolve under any code root that is a git repository | warning by default (`unverifiable, not necessarily wrong`); error under `--strict-citations` |
| a recognized `path:line` citation's file is not found under any code root | warning by default (`unverifiable, not necessarily wrong`); error under `--strict-citations` |
| a `commit `/`merge `-triggered citation resolves, but a same-line quoted subject after it does not match `git log -1 --format=%s` (whitespace-normalized) | error, unconditionally — a substantiated mismatch, not affected by `--strict-citations`. `at `/`as `-triggered and backtick-wrapped citations are never subject-eligible at all — a quote following one of those is never read as a claimed subject, checked or not |
| a `path:line` citation's file exists but has fewer lines than cited | error, unconditionally — substantiated, not affected by `--strict-citations` |
| a `path:line` citation's path is absolute | error, unconditionally — a shape violation, not a repository question |
| `--strict-citations` given with no `--code-root` | no effect (the whole citation check is already skipped) |
| `--strict-citations` given together with `--against-ref` | exit 2 — same rejection as `--code-root` under `--against-ref` |

Corpus-wide identity rules, applied to every record regardless of type:

| rule | severity |
|---|---|
| the same explicit `id:` is claimed by more than one record | error |
| several records with no explicit `id:` share a file stem — `chain <stem>` is then ambiguous | warning (the durable fix is an explicit id on each) |

Exit 1 on any error anywhere under `ROOT`; warnings alone exit 0. Standalone
(non-topic, non-concept) records get `lint_record`'s own check — enum
validity on whatever `status`/`authority` fields they carry — plus, like every
other record type, the two corpus-wide identity rules above: any record with
an explicit `id:` is checked against every other record's `id:` for a
duplicate, and a standalone record with a `type:` but no explicit `id:` is
checked for a stem collision. The one exemption from the stem check is
untyped plain markdown with no `id:`, no `type:`, and no `links:` (a README,
an inbox drop) — nobody chains those by stem, and two files sharing one is
the normal state of the tree.

### Append-only history (`--against-ref`)

`memlint.py --against-ref REF [--staged] ROOT` is a second, independent check
— when given, it replaces the schema-rule pass above entirely (never both in
one invocation): an adopted store may carry pre-existing schema findings the
installer already tolerates, and this check must never fail a commit over a
condition nobody ruled on.

For every topic file present at `REF` and now, it parses both sides with the
same typed parser (`parse_record`/`parse_record_text`) and compares links by
id:

| rule | severity |
|---|---|
| a link present at `REF` has a BODY field changed (`ruling`, `rationale`, `alternatives`, `evidence`, `revisit_if`, `edges`, `assumptions`, `invariant`, `date`, `kind`, `reverses`, `reason_for_change`, `recorded_by`, `recorded_at`) | error naming the field, `<path>:<link>: <field>: link field changed after being recorded` |
| `status` changes other than `active`/`provisional` → `superseded`/`historical`/`declined` (backward, or between the three terminal values) | error, `<path>:<link>: status: changed from … to … after being recorded` |
| `superseded_by` changes after already being set, or is added while `status` is not `superseded` | error, `<path>:<link>: superseded_by: …` |
| `promoted_by` changes after already being set | error, `<path>:<link>: promoted_by: …` (no status coupling — it may be added regardless of the link's own status) |
| a lifecycle move (`status` and/or `superseded_by` and/or `promoted_by`, otherwise valid) bundled with any body-field edit | error on the lifecycle field too, naming it, in addition to the body field's own error |
| a link present at `REF` is missing now | error, `<path>:<link>: link removed after being recorded` |
| a topic-like record present at `REF` is deleted or renamed | error naming the path and its real kind (`topic`/`incident`/`investigation`/`concept`, or `record` when the kind itself could not be recovered) — `--no-renames` means a rename is a plain delete + a plain add, so one rule covers both |
| a duplicate link id within one topic on the NEW side | error naming the id, `<path>: duplicate link id '<id>' used <n> times` — the same `memidx.validate_record_shape` diagnostic every consumer shares |
| a valid forward `status` move (with `superseded_by` added when the new status is `superseded`), `promoted_by` added, alone on the link; new links; changes to `current`, `title`, `tags`, `code_refs`, or the body text | free |
| a link present at `REF` has its PARSED fields unchanged but its RAW TEXT SPAN changed (a YAML dumper re-render: different quoting, key order, wrapping, or a whitespace-only edit, with identical parsed content) | error, `<path>:<link>: link reformatted, not appended` — the field-level comparison above never fires for these (every field is equal), so the span is compared byte-for-byte separately; a span that cannot be safely attributed to its link on the `REF` side (structural extraction inconclusive — see below) skips this comparison with a NOTE, never an error — the field-level rules above still apply |
| the same, but the span cannot be attributed on the NEW side (structural extraction inconclusive there instead) while the `REF` side extracted fine | error, `<path>:<link>: link's span could not be extracted on the new side; treated as changed` — a deliberately different message from the row above: this is a proven extraction FAILURE treated as a change (fail closed), never a claim that the bytes were actually compared and found to differ |
| a new link (present now, absent at `REF`) sorts BELOW some link that already existed at `REF`, in the new file's own link order | error, `<path>:<link>: link inserted out of order` — every new link must sort above every `REF`-side link, newest-first; reordering AMONG `REF`-side links themselves is not separately checked |
| the `REF`-side blob failed full validation (a shape error, or a duplicate link id) but its `links` field itself still recovers at least one usable entry | not a repair — those recovered links (a duplicate id keeps its FIRST occurrence, file order) are still compared against the new side by every rule above, exactly as if the REF blob had parsed cleanly, with a note naming the REF-side diagnostic that made the blob invalid |
| the `REF`-side blob never parsed at all (or parsed but recovered no usable `links`), and the new blob now parses cleanly | a note, not an error (`repaired — the blob at REF could not be safely parsed …`) — nothing here was ever recorded link history to freeze, so fixing it is a repair, never an append-only violation |
| frontmatter that does not parse on the new side, or on both sides | error, the typed-parse diagnostic — never a traceback |
| a record whose recoverable kind is not topic-like (`type: incident`/`investigation`/`concept` with no `links:`) | out of scope for this check entirely — there is no recorded link history to protect, so neither an error nor a note |
| `--code-root` given together with `--against-ref` | exit 2 — append-only mode never uses a code root, and the flag is rejected rather than silently ignored |
| a `REF` argument that starts with `-` (would otherwise swallow the next flag, e.g. `--staged`, as if it were the ref) | exit 2 naming the flag-shaped token that was refused |
| `ROOT` is not inside a git repository, or `REF` does not resolve to a commit | exit 2 with a message (not exit 1 — this is an infrastructure/usage failure, not a content finding) |

The three lifecycle fields exist because a link is not always closed the
instant it stops being current: marking one `superseded`/`historical`/
`declined` — and naming its successor once that exists — is bookkeeping, not
a change of mind, and forcing a whole new link for it would just move the
same edit into a place this check cannot see it happen. Promotion (§5) still
appends a NEW link rather than editing `status` to `active`/`provisional` in
place — those two directions were never legal moves for that field; what
promotion DOES edit on the OLD link is `promoted_by`, once, naming the new
link, with no requirement that the old link's own status move at all.

**Residual gap (documented, not closed here):** a link making a legitimate
lifecycle move (say `active` → `historical`) has PARSED fields that differ
by definition, so the byte-span comparison never runs on it at all — only
the field-level rules judge it. A commit that both promotes a link's status
AND re-renders that same link's bytes in whatever way it likes is invisible
to the byte layer for that one link; every OTHER link in the same file still
guards a whole-file regeneration (their parsed fields stay equal, so their
spans are compared), so the exposure is narrow — one link, the one commit
that moves it — not a whole-file regeneration.

**Structural extraction, and when it gives up.** The byte-span comparison
locates each link's raw text by INDENTATION, never by regexing an id back
out of a `- link:`-style line — a `yaml.safe_dump` re-render sorts an
item's keys and may start it with any field (`- alternatives:`, `- date:`),
not necessarily `link:`. It tolerates a trailing space after `links:` and a
`#`-comment line at any indent (attributed to whichever span is already
open, never mistaken for a boundary). It still gives up — a NOTE on the
`REF` side, an error on the new side (see the table) — on a flow-style
`links: [...]` (no real store file does this today), or when the number of
structurally-found items does not match the parsed `links` list (a `REF`-
side duplicate id `_recovered_old_links` already resolved to one entry is
the one case seen in practice).

A comment line's own position still matters, even though its indent never
does: one added directly under `links:`, before the first item, belongs to
no span at all and is free (attributed to nothing, the same as any other
text outside every item's own range). One added AFTER the last recorded
item, by contrast, becomes part of THAT item's span — a genuinely new
trailing byte inside the last link's own range — and correctly flags that
link as reformatted; the message is literally accurate, not a false
positive, because the file at that link's position really did change. And
because the newest-first order check (above) matches a new link to a `REF`-
side one by id rather than by position, it refuses an in-place repair that
merely ADDS a missing `link:` key to a `REF`-side entry that never had one
(a shape error distinct from a duplicate id): the repaired entry now
carries an id `REF` never associated with that position, which the check
reads as a new link landing below an existing one. The only way through is
`--no-verify` — narrow, and the honest cost of matching by id rather than
by position.

`--staged` compares `REF` to the INDEX (`git show :path`, what `git commit`
would actually commit); the default compares `REF` to the working tree
(`git diff REF` — the standard "everything you'd get if you staged
everything" comparison). The summary line
(`memlint: append-only against <ref>: changed=<n> errors=<n> notes=<n>`) is
what `hooks/pre-commit-append-only.sh` parses `changed=` and `notes=` off of
for its own `hook.log` line; `changed` counts topic files the diff actually
concerned, independent of whether any produced an error; `notes` counts
informational lines (a repair, or a structural-extraction bail-out) — never
an error, but worth seeing: a NOTE used to print to stdout with no count on
the summary line, so a clean (`rc=0`) run — the only shape the hook never
echoes its captured output for — could carry one and nobody would know.
NOTE lines now print to stderr and the count rides the summary line
`hooks/pre-commit-append-only.sh` already parses, so this is visible in
`hook.log` on every run, clean or not.

The store's own git `pre-commit` hook (`hooks/pre-commit-append-only.sh`,
wired by `scripts/repo-init.sh` step 6b the same way `post-commit-reindex.sh`
is wired at step 6) runs `--against-ref HEAD --staged` on every commit and
BLOCKS the ones that fail it — see "Fail-open is the contract, not a
fallback" above for the one deliberate exception this makes to every other
hook's fail-open rule, and "The watchdog"'s `Unguarded:` list for why no
timeout wraps it. The same hook also runs the plain schema check
(`memlint.py ROOT`, no `--against-ref`, no `--code-root`) and blocks on any
ERROR from it (warnings pass) — a fenceless record committed under
`topics/`, `incidents/`, or `investigations/` (§7) is exactly the kind of
problem this catches that the append-only diff alone never would, since a
brand-new file has no history at `REF` to compare. The schema pass reads the
WORKING TREE, not the staged index (unlike the append-only diff, it has no
`--staged` form), a narrow gap of its own. `git commit --no-verify` bypasses
both, same as any git hook; the same checks run again in CI against a wider
range, plus a protected branch, are the real guarantee against a rewritten
or unfenced history for a store other machines also touch — not this hook
alone, which only ever sees one commit at a time on one machine.

## Storage and index

Markdown is canonical; SQLite is a disposable cache, rebuildable with `reindex`.

**Walker pruning.** `reindex`/`check`/`unmapped` — and memlint, which imports the
same walker — walk every non-hidden `.md` file under `--root` but prune
dot-directories, dotfiles and `node_modules` at every depth. The root itself is
never pruned, so a store that legitimately lives at `~/.memory/` still indexes
in full. A `.gitignore` cannot express this pruning, because this is a
filesystem walk rather than a git one. The walker also does not follow
symlinks: a symlinked directory is not descended and a symlinked file is
skipped rather than read, each skip warned on stderr and counted by `check` as
`symlinks_skipped`. A `--root` that is itself a symlink to the store is
resolved first and still works. The code index walker likewise does not
descend a symlinked directory, but — the deliberate difference — it does
index a symlinked source file through the link.

Every `.md` file the walk does not prune IS indexed as a record — including
one with no frontmatter at all (`parse_frontmatter` is tolerant of that, see
below, and `infer_type` falls back to the containing directory name), so
"put arbitrary markdown under the store root" is not a safe way to keep it out
of `search`/`chain`/`for-path` results. The directory-name pruning above (a
`.remember/now.md` session buffer, a project's `.claude/`) is the only thing
that keeps non-record markdown out of the walk. One directory IS a
deliberate exception to "everything under the root is first-class": a path
under `inbox/` indexes as `type: inbox` unconditionally (`infer_type` checks
the directory before it ever reads frontmatter, so a drop that carries its
own conflicting `type:` field still indexes as `inbox`), and `search`
excludes `type: inbox` rows unless `--include-inbox` (or an explicit
`--type inbox`) is given -- a freeform consult drop stays out of the way of
`search`'s default results without being invisible to the index (`check`/
`reindex` counts, `chain`/`for-path`/`why` are all unaffected, since none of
them ever return an inbox record in the first place).

**Two databases.** `<project>.sqlite` (decisions) and `<project>-code.sqlite`
(Anatomy's code index) are separate physical files by default, each with its own
schema, its own content-hash incremental rebuild, and its own embeddings.
Nothing enforces the split as a hard rule: `--db` (code) and `--decision-db`
(decision, on `code-search`) are independent flags. `open_code_db` carries no
ownership check at all.

Never place either db under a synced or cloud drive — keep the index on a local
POSIX filesystem.

### Decision index provenance and embedding lifecycle

The decision index (`<project>.sqlite`) carries the same provenance discipline
`code_index_report` already gave the code index (see
[Root-scoped indexing](#root-scoped-indexing)), computed by
`decision_index_state(db_path, project, root=None|Path)`:

| state | means | a reader does |
|---|---|---|
| `missing` | no db file at that path | refuse (no query attempted) |
| `uninitialized` | file exists, has a `db_meta` row for this project, but no `last_reindexed_at` stamp and no `records` rows | refuse (no query attempted) |
| `upgrade-required` | no `last_reindexed_at` stamp but `records` rows already exist (an older engine's db), or the stamped `index_generation` is behind the engine's current one | proceed, warn on stderr |
| `stale` | current generation, stamped, but the store's on-disk drift comparison finds added/changed/removed files — only computed when a `root` is given. For the five metadata-only readers (`search`/`chain`/`for-path`/`why`/`drift`, given `--root`) this is a plain mtime/size comparison against what `reindex` last recorded — **metadata moved, content unverified**. For `check` and `unmapped` it is content-PROVEN: every walked record with a stored row is hashed and compared to `records.sha256`, so a same-size, same-`mtime_ns` rewrite a metadata comparison alone would miss is still caught (a sha match whose metadata moved is bookkeeping-refreshed in place, not drift) | proceed, warn on stderr |
| `quarantined` | current generation, not `stale` (a `root`-taking reader's own drift check passed or was not asked for), and `index_errors` holds at least one row for this project (one or more records could not be safely indexed — see "Tolerant parsing and quarantine" above); needs no `root` — a plain table lookup | proceed, warn on stderr naming the skipped-record count; `unmapped` refuses the negative claim (below) |
| `current` | stamped, current generation, no drift, no quarantined record | proceed silently |

**One reader refuses instead of warning: `standing` (see [standing decisions](#standing-decisions)).**
Every table row above says "proceed, warn on stderr" for `upgrade-required`/`stale`/`quarantined` --
a positive match off a degraded index is still real evidence for a reader that is ANSWERING a
question the caller asked. `standing` is not answering a question; it is asserting, unprompted,
that a ruling still holds -- so it refuses on every state but `current`, `missing` and
`uninitialized` included. A degraded index there is not a caveat to surface alongside the answer;
it is a reason to say nothing at all.

**Readers never hash; `check` and `unmapped` are the proof.** Hashing every
record on every reader call would put a full store read on the pre-edit hot
path — `for-path` runs once per pre-edit candidate under its own watchdog, on a
store whose stat-only walk already costs over a second on a slow (drvfs/9P)
filesystem — so the five metadata-only readers keep the plain mtime/size
comparison unconditionally; only the two commands that may WRITE the cache
(`check`'s own report, `unmapped`'s self-heal gate) hash. A reader's `stale`
therefore means "metadata moved, not yet proven"; `check --json`'s `changed`
list means content changed, proven by a hash, and a bare `touch` (mtime
moves, content identical) is bookkeeping-refreshed and reported neither
`changed` nor `drift`. This is a documented boundary, not an oversight: a
same-size, same-`mtime_ns` content rewrite reads `current` off `search
--root` and every other reader until the next `check` or `unmapped` call
proves otherwise.

Every reader opens the file with `open_db_noncreating` — a genuinely
non-creating SQLite URI (`mode=rw`) — never `open_db`'s create-on-connect path,
which closes a TOCTOU window a plain `exists()` check followed by `connect()`
still had (the file could be created by a race between the two calls). `root`
is REQUIRED on `reindex`, `check`, and `unmapped` (they walk the store's own
markdown tree by design); `unmapped` branches on `stale` to self-heal (below).
`search`, `chain`, `for-path`, `why`, and `drift` take `root` as an OPTIONAL
`--root DIR`: omitted (the default, and unchanged from before),
`decision_index_state` can still report `upgrade-required` or `quarantined`
for them but never `stale` — they have nothing to walk without it. Given, the
same five readers CAN see `stale`: a positive match off it is still returned
(see the next paragraph), with one stderr line naming the cause (`"<cmd>:
index is stale (store changed since the last reindex); results may be
outdated"`). `for-path`'s own stale positive match, reached through
`hooks/pre-edit-chain.sh` (which passes `--root "$MEMCONTINUUM_ROOT"` on the
one `for-path` call it makes per candidate, whenever that env var is set), is
logged under its own hook.log outcome name, `index-stale-served`, distinct
from a plain `matched` — the staleness caveat itself reaches `hook.log` only
via that call's own stderr (the hook's existing redirect), never the injected
`additionalContext` payload. `for-path` also takes an opt-in `--with-chain-text`
(added alongside this), which folds the plain-text chain rendering into the
same `--json` envelope as a `chain_text` field — the hook passes it on every
candidate call so the SAME call that finds the matching candidate and its
state also returns the text `additionalContext` is built from; there is no
second `for-path` call fetching that text separately, so one hook run walks
the store at most once per candidate, and makes exactly one `for-path`
invocation for whichever candidate matches.

**A `git worktree` of a wired code root is remapped, not indexed.**
`MEMCONTINUUM_CODE_ROOTS`/`MEMCONTINUUM_STRIP_PREFIX` name absolute paths fixed
at repo-init time, so a `git worktree add` checkout is a different absolute
directory nothing wired — every candidate above (raw path, cwd-relative,
STRIP_PREFIX forms) comes up empty for a file edited there, the same way a
genuinely unbound file does. `mc_remap_worktree_path` (`hooks/mc-path-lib.sh`)
is tried once every other candidate has already failed: if the edited file's
`git rev-parse --git-common-dir` matches a configured code root's OWN
common-dir (asked of git directly — `git -C "$root" rev-parse
--git-common-dir` — rather than assumed from `"$root/.git"` being a
directory, so this also matches when the configured root is ITSELF a linked
worktree or a `--separate-git-dir` checkout), the file is treated as though
it were the same path in the main checkout (fed back through the same
candidate list above) — the worktree's own content is never read into the
index; only the path is remapped. Both git calls run with
GIT_DIR/GIT_WORK_TREE/GIT_COMMON_DIR explicitly unset for that one
invocation, so an ambient value from the hook's own environment can never
redirect the identity check onto an unrelated repo. A worktree of some
OTHER, unwired repo is never remapped onto this project's records (`git
rev-parse` still confirms a worktree, but no configured root's common-dir
matches) — logged as `worktree-unwired`; when git itself cannot settle the
question, `worktree-unresolved`.

The same remap fires in `hooks/ledger-post-edit.sh`'s own root-containment
check — not only once that loop has found nothing (a SIBLING worktree, or a
path outside every git checkout entirely), but ALSO when the loop already
matched a root, if a cheap bash-only ancestor walk (`mc_nested_worktree_gitfile`)
finds a `.git` FILE strictly between the edited path and that root: a linked
worktree checked out INSIDE the root (e.g. `<root>/.worktrees/feat/`) is
physically "under" the root by plain containment, but is a different git
checkout from the root's own, so the pre-remap path/root annotation would
otherwise silently name the worktree instead of the main checkout. On a
successful remap the ledger row is recorded under the main-checkout
path/root (so `memidx.py unmapped --code-root` sees it), with
`content_sha256` still read from the file that was actually edited and the
real path kept visible on the row as `worktree_path`. `pre-edit-chain.sh`
gates the same in-root case the same way before trying the remap.

Cost: an ORDINARY file — already under a configured root, with no nested
worktree on its ancestor chain, or not in any git checkout whatsoever — still
never spawns git (the bash-only `.git`-entry / `mc_nested_worktree_gitfile`
gates rule it out first). A worktree edit that DOES need remapping pays one
`git rev-parse` to resolve the edited file's own identity, plus one more per
configured root while searching for the match (bounded by the number of
configured roots, typically one or a few) — no longer "a single `git
rev-parse`" as this section previously (incorrectly) claimed for every case.

This closes the gap in each hook's own logic; it does not change which paths
the harness LAUNCHES either hook for at all. `pre-edit-chain.sh`'s
settings-level `"if"` filter is still a literal, per-code-root path glob
(`templates/code-root-filter-pair.json.tmpl`): a worktree checked out as a
SIBLING of the wired root never reaches the script at all under the current
settings rendering, so it gets **ledger coverage only** (`ledger-post-edit.sh`
has no such filter — design R6, "fires for EVERY tool" — and covers it).
A worktree checked out **INSIDE** the wired root (e.g. `<root>/.worktrees/x`)
DOES reach `pre-edit-chain.sh` and gets full **retrieval and ledger**
coverage — this is the layout this fix actually closes end-to-end. A project
that wants retrieval for its own worktrees should therefore keep them inside
the code root (e.g. `<root>/.worktrees/`, gitignored by that project) rather
than as siblings; widening the settings-level glob to also launch
`pre-edit-chain.sh` for a sibling worktree is a separate, not-yet-done
follow-up.

**A positive match off a non-`current` index stays usable; a negative claim
does not.** `upgrade-required`, `stale`, and `quarantined` all warn and
proceed — a hit found there is real evidence, not withheld just because the
index is not perfectly fresh or is skipping some other, unrelated malformed
record. What must never be trusted off anything but `current` is the
*absence* of a match: `unmapped`'s `coverage_status` collapses
`missing`/`uninitialized` to `"uninitialized"`, `upgrade-required` to its own
`"upgrade-required"` state, and `quarantined` to its own `"quarantined"`
state, and in every case returns `unmapped: []` rather than asserting
"nothing governs this file" from an index that cannot back that claim (a
quarantined store never self-heals for `unmapped` either — a malformed
record does not clear itself by reindexing again). Only
`coverage_status == "ok"` (state was `current`, or `stale` and the self-heal
below cleared it) actually populates `unmapped`.

**Exit codes.** `for-path` is the one reader with its own distinct codes (used
directly by `pre-edit-chain.sh`): `0` success, `2` is argparse's own reserved
usage-error code (untouched), `3` is `missing`/`uninitialized` — collapsed,
since neither has a positive match worth attempting, and it also covers the
TOCTOU race where the file vanishes between the state check and the open —
and `4` is `index-error`, a belt-and-suspenders catch for a
`sqlite3.OperationalError`/`IndexError` that a migration guard should already
have prevented but didn't (a raw query naming a column that no longer exists,
or a `sqlite3.Row` access on a renamed column). `upgrade-required`/`stale`/
`current` all proceed to exit `0` normally. Every other reader (`search`,
`chain`, `drift`, `check`) shares one `_decision_reply` refusal on
`missing`/`uninitialized` (exit `1`, `--json` still emits a real `{"state":
..., "results": []}` envelope on stdout so a caller piping stdout still learns
why) and one `_decision_warn` (stderr only) on `upgrade-required`/`stale`.
`unmapped` itself exits `1` whenever `coverage_status != "ok"` (a state alone
is enough — no candidate path even needs to be unmapped); the hooks that call
it (`userprompt-remind.sh`'s coverage check, `precompact-persist.sh`) treat
either exit `0` or `1` as a normal, fail-open answer — only an actual
exception invoking it would make them fail open on the hook's own contract —
and log the `coverage_status` they got as their own `index-uninitialized`/
`index-upgrade-required`/`index-error` outcome line; see
[Hooks and the fail-open contract](#hooks-and-the-fail-open-contract) above.

**Embeddings are never silently stale.** `embeddings.embed_sha` records the
exact record `sha256` a vector was actually computed from (a pre-existing row
predating the column reads `embed_sha IS NULL`, backfilled on the project's
first embedding-capable reindex after upgrade). Every vector-search query — in
`vector_ranked`, and the same `records r JOIN embeddings e ON e.path=r.path AND
e.embed_sha=r.sha256` shape in `check`'s own vector-coverage count — joins on
`embed_sha == sha256`, so a vector kept physically stale (a `--no-embed`/
`--auto` edit changes a record's content but deliberately skips recomputing
its vector) is invisible to ranking entirely, not merely scored lower, until a
real reindex refreshes it. The row is never deleted on a stale edit — deleting
it would lose the "this needs a backfill" signal the sha mismatch itself
carries — only replaced on the next embedding-capable pass.

**`embed_sha` alone answers "which source text"; `embed_fp` answers "which
model".** Every `embeddings` row also carries `embed_fp`, a flat
`model=<name>;dim=<n>;pipeline=<v>;prefix=none;norm=l2;fastembed=<version>;
revision=<snapshot or "unknown">` string identifying the model/pipeline that
produced that specific vector (`embedding_fingerprint()`; the code index's
`embeddings.embed_fp`/`code_project.embedding_fingerprint` carry the same
shape). `revision` is the loaded model's HF snapshot directory basename when
a real model was loaded, `"unknown"` otherwise; two fingerprints are
considered equal (`fingerprints_match`) key-for-key except `revision`, which
is compared only when NEITHER side is `"unknown"` — a command that never
loaded a model (`check`, a `--no-embed`/`--auto` reindex) cannot verify
revision and must never call a healthy db `mismatch` over it. `vector_ranked`/
`code_hits_vector`'s freshness join adds `AND e.embed_fp = ? AND e.dim = ?`
against the CURRENT model's real fingerprint and the query vector's length —
a NULL `embed_fp` (a pre-fingerprint row) or a foreign one (a model swap)
is therefore excluded from ranking exactly like a stale `embed_sha`, never
mixed with same-project vectors from a different model or dimension. `cosine`
itself raises a typed `VectorDimensionMismatch` (a `ValueError` subclass) on
unequal-length vectors and a plain `ValueError` on a non-finite component
(`zip` used to silently truncate a length mismatch instead); the scoring
loops catch either, per row, skip it, and count it into the JSON envelope's
`dimension_mismatch_rows` when > 0 — a defensive path (the SQL `dim` gate
already excludes the common case), reached only by a corrupted blob whose
stored `dim` column lies. Scoring itself (`_score_rows_by_cosine`, the one site both
searches share) is vectorised with numpy when it is installed — one matrix product
instead of a per-row loop — and falls back to the original per-row loop
(`_score_rows_by_cosine_loop`) when it is not, or when the matrix block raises
anything (recorded as `vector_scoring_fallback` in the stats); the query is cast to
plain floats first so both paths accumulate in float64; the two give the same ranking, scores
and `dimension_mismatch_rows` count. Before either embedding zip (decision or code
index), `len(vectors) != len(texts)` is treated as a full embedding failure —
nothing is written, stderr names both counts — rather than silently
mis-zipping a short/long/mis-ordered backend response; `reembeds` (code
index) and `backfilled` (decision index) both count vectors *actually
written*, never texts merely sent.

At query time (`search`/`code-search`, vector and hybrid modes), the model is
loaded once and its fingerprint compared against the stored one — ONLY when
the project already has at least one embeddings row (a project that has
never been embedded has no stored fingerprint for an unremarkable reason, not
a model swap). On a mismatch the vector query is skipped entirely (never
calls `vector_ranked`/`code_hits_vector`): stderr names
`search: embeddings were made by a different model (<stored> vs <current>);
using FTS only -- run reindex to re-embed` (code: `run code-reindex`), the
`--json` envelope's `"embedding"` field reads `"fingerprint-mismatch"` (the
same slot that reads `"unavailable"` on a broken backend), hybrid returns the
FTS-only list, and the command exits 0.

At reindex time (embedding enabled), the model is loaded once, up front —
before deciding what needs re-embedding — specifically so a fingerprint
mismatch can be caught even when no row's `sha256` moved: on a mismatch (or a
NULL stored fingerprint while the project already has embedding rows) every
row is treated as due for re-embed, and the new fingerprint lands in the same
transaction as the vectors of the run that wrote it. Under `--no-embed`
(decision side: `--no-embed`/`--auto`) the mismatch is reported once on
stderr, using only the STATIC fingerprint (no model load — a `--no-embed`
run never touches fastembed), and is deliberately NOT repaired.

**`embedding_mode`** (`db_meta['embedding_mode']`, one of `none` / `partial` /
`full`) mirrors the code index's own field and is recomputed from *actual
fresh coverage* — a row counts as fresh only when its `embed_sha` AND its
`embed_fp` (matched against the current fingerprint's static fields — the
model was loaded this run whenever embedding was enabled; a `--no-embed`/
`--auto` run compares only the static prefix, since no model was loaded)
both match — `fresh == 0` → `none`, `fresh == total` → `full`, otherwise
`partial` — on every reindex except a no-op `--auto` pass (nothing added,
changed, or removed): an internal heal must never announce, or force, a mode
change for a change that did not happen. A run that does change rows under
`--auto` (self-heal `--no-embed --auto` still forces `no_embed=True`, so
backfills never happen there) still recomputes the mode from real coverage, so
`full` can never keep standing over vectors that very pass just made stale.

**`--auto`** is the internal/hook-facing reindex mode: content is rewritten
(so a schema migration or a genuine markdown edit is still picked up) but
nothing is embedded, and — per the paragraph above — the run never claims a
fuller `embedding_mode` than the coverage it actually produced. It is what
`unmapped`'s self-heal (`reindex --no-embed --auto`, on state `stale` only —
never on `upgrade-required`, which is the rollout's job, not an ad-hoc
hook-triggered one), `precompact-persist.sh`, and the store's own
`post-commit-reindex.sh` (its bounded content pass, on every commit) use to
keep a hook-triggered repair honest about what it did and did not refresh.
Every reindex pass -- `--auto` or not -- also now prints, at the end of its
summary line, `N record(s) awaiting embedding`: rows whose vector is missing,
stale (`embed_sha` mismatch), or from a different model fingerprint,
computed after the writes. `post-commit-reindex.sh` parses this off the end
of the line to decide `embed=pending|clean` (see the embed-worker section
below); it is `0` on a fully-embedded store, never omitted.

**The background embed-worker** (`memidx.py embed-worker`): when the
post-commit hook's content pass leaves `N > 0`, it calls
`embed-worker --spawn --python "$PY" --root … --project … --db …`, which touches
`$MEMCONTINUUM_HOME/<project>.embed-pending` and spawns the worker detached
(`subprocess.Popen(start_new_session=True)` from Python -- never bash `&`,
never a `setsid` binary, which macOS does not ship; stdio redirected to
`<project>.embed.log`, never the shared `hook.log`). `embed-worker` takes a
non-blocking `fcntl.flock` on `<project>.embed.lock` first -- a second
worker finding it already held exits 0 immediately, so two quick commits
never run two overlapping embedding passes. It then loops: while the marker
exists, claim it by renaming it to `<project>.embed-pending.claim.<pid>`, run
the embedding backfill in-process (`cmd_reindex` with embedding enabled), and
on success delete the claim -- a commit landing mid-pass raises a NEW marker
(the producer's touch is path-based, so it can never land on the claimed
file unnoticed) and the loop runs again rather than declaring victory over
content it never saw. On any exception from the backfill (a locked db,
a genuine bug), the request is re-raised by touching a fresh marker (nothing is ever renamed
onto the marker path) and the verified claim is deleted, the traceback is appended to
`<project>.embed.log`, and the process exits 3 -- a caught exception
therefore always puts the request back. A hard kill or a claim that cannot be inspected leaves
`<project>.embed-pending.claim.<pid>` (swept after an hour); the request is
re-raised by the next store commit's awaiting count
(`hooks/post-commit-reindex.sh`) or, for code, by the next `code-refresh`
preflight. A manual `embed-worker` run with no marker does nothing. `check --json` and `stats --json` both report
`embedding_backlog: {pending_marker, worker_lock_held,
rows_without_fresh_vector}` (fail-open; the row count uses the same
static-fingerprint SQL comparison `vector_index_state` does, never loading
the model) -- `vector_index_state` itself keeps its four-value enum
unchanged; "pending" is not one of its values.

**Project isolation, on the decision db.** The default per-project filename does
the work in the normal case. Beyond that, the guarantee is a *refusal*, not a
partition: `records`, `embeddings`, `concepts` and `links` key rows by `path`
alone, so two projects sharing one physical file could evict each other's rows
on a colliding path — reachable only through an explicit `--db`. Rather than
migrate every table and query to a composite key, `open_db` stamps the owning
`--project` into a `db_meta` table the first time a file is opened and raises
`DbProjectMismatchError` on any later open under a different project. Two
`--project`s therefore cannot share one decision db at all.

An unstamped file (built before the stamp existed, or one that lost its row) is
only stamped automatically when the data agrees it is safe: no rows at all, or
rows for exactly one project and that is the project asking. Rows for a
different project, or rows spanning several, refuse instead of guessing.
`open_db` skips the whole check when no project is given, so a tool can still
inspect a db file directly.

Queries that select by project do filter on the `project` column, which keeps a
same-project reopen honest; that filter is a second layer, not the thing that
makes sharing a file safe — path-keyed lookups such as `record_row_by_path` do
not carry it.

**A topic's own embedding text is `title + "\n\n" + body[:1500]`** — no
frontmatter YAML, no ruling text. That formula was measured at
10/10 top-1 paraphrase retrieval on real records, and
`test_paraphrase_top1_at_least_9_of_10` reruns the measurement as a permanent
regression check. (The corpus and probe set behind this measurement are
private, untracked files — the tests that use them skip automatically when
absent; see [Test conventions](#test-conventions) for the full disclosure.)
That formula is unchanged, but it is no longer the whole story: **every link
with ruling or rationale text also gets its own,
separate embedded row** — `title + "\n\n" + ruling.text + "\n" + rationale.text`,
keyed by a hashed `link:<sha256>` surrogate (`link_record_key`, never a real
walked path) rather than the topic's own path. A ruling's actual text is
therefore retrievable through vector search too, not only through the FTS
`ruling_text` column/BM25 ranking it has always gone into — in every search
mode, since a link row is an ordinary candidate on both the FTS and vector
sides.

**RRF, not score blending**, for hybrid search (the store search uses `k=60`; code-search uses
`CODE_RRF_K` = 30, provisional, chosen on the graded intent set and re-measured
at the next relevance re-check): bm25 scores and cosine
similarities live on incomparable scales, so any weighted sum of the two is
arbitrary.

**Hybrid steps aside to vector-only, before fusing, when FTS's own top-1
candidate doesn't share real vocabulary with the query** (content-term
coverage below `FTS_STEP_ASIDE_COVERAGE`, `_fts_top_hit_is_confident`) — a
plain "the FTS channel returned nothing" check can't catch this: `fts_escape`
ORs every raw token including stopwords, so a query with zero real overlap
with its target can still match most of a store, and RRF would otherwise fuse
that noise into an accurate vector ranking. `--mode fts`/`--mode vector` are
unaffected; only hybrid's own hit set can equal `--mode vector`'s exactly.

**`current` is derived from list order, not from `date:`.** Links are defined to
be stored newest-first, so "the newest active link" is "the first link with
`status: active`". The linter's `current` check and the `chain`/`for-path`
header line agree on this, which keeps a file whose dates are out of order but
whose positions are correct handled predictably. Authors should still keep dates
and positions in agreement.

**Tolerant parsing and quarantine.** `parse_record()` returns a typed
`ParseResult` (`frontmatter`, `body`, `diagnostics` — a list of `(field,
message)` pairs, `valid`, `fallback`) and never raises: a read failure
(`OSError`/`UnicodeDecodeError`), a malformed or non-mapping frontmatter block,
and a wrongly-shaped typed field are all diagnostics, never exceptions.
`parse_frontmatter()` stays as a thin `(dict, str)` wrapper over it for callers
that only need the untyped shape.

A record is *canonical* — it carries the append-only chain the rest of this
document describes — when its frontmatter has a schema `id` (`TOP-`/`INC-`/
`INV-`/`CON-`), a `links` list, or a schema `type` (`topic`/`incident`/
`investigation`/`concept`). Everything else is a *note*: pre-existing markdown a
project wants indexed as-is, with no chain to validate. `validate_record_shape()`
runs after a successful YAML parse and checks every typed field is its declared
shape or absent — `links` a list of mappings each with a scalar `link` id,
`ruling`/`rationale`/`invariant` mappings, `edges`/`assumptions`/`alternatives`
lists of mappings, `tags`/`code_refs`/`implemented_by`/`tested_by`/
`governed_by`/`involved_in` lists of scalars, `metadata` a mapping. A violation
on a *canonical* record makes it invalid; the same violation on a note is
recorded but never blocks it — a note has no chain shape to protect.

**Canonicity is checked against the raw frontmatter text whenever no usable
parsed dict exists** — an unterminated frontmatter block (an opening `---`
with no closing one), frontmatter that parses to something other than a
mapping (a top-level YAML list, say), and the lenient regex fallback below (a
complex field like `links` is never recovered into the parsed dict at all, so
a record canonical only via a bare `links:` key would otherwise never be
recognized as canonical). In each of these, the raw lines are scanned for the
same three signals — a schema `id:` prefix, a `links:` key, a schema `type:` —
so a fully schema-conformant record undone by only one of these failures is
still quarantined, not silently reduced to a filename-derived note with its
real `id`/`type`/`links` thrown away.

On a YAML parse error, the lenient regex fallback pulls `id`/`title`/`name`/
`type`/`area`/`topic`/`date`/`status`/`authority`/`current`/`project`/
`description`/`permalink` — scalar fields only — out of unindented `key: value`
lines, logging one warning to stderr, so a note's `title`/`name`/`type` survive
and it stays indexed. A complex field (`links`, `tags`, `code_refs`, `edges`,
`assumptions`, `invariant`, `metadata`, `ruling`, `rationale`, `alternatives`,
`evidence`, `implemented_by`, `tested_by`, `governed_by`, `involved_in`) is
never recovered this way — regex text can't tell "no value" from "an unclosed
flow collection" (`links: [` is the reproduction that motivated this: recovered
blindly, the literal string `"["` would be handed to code expecting a list of
mappings and crash several calls deep) — and gets its own diagnostic naming the
field, EXCEPT a blank header line (`links:` with nothing after the colon) on a
*note*, which is silently dropped instead (neither recovered nor diagnosed;
this is what keeps a bare `metadata:` line harmless on ordinary pre-existing
markdown). A canonical record's own blank complex-field line still gets its
diagnostic — the carve-out is for notes only. A canonical record whose
frontmatter took this fallback path is invalid.

An invalid record is quarantined by `reindex`, not built: its previous rows (if
any) are purged, one row is written to `index_errors` (path, project, sha256,
mtime, size, its diagnostics as JSON, and when it was last seen), one stderr
line names the file and the first diagnostic
(`quarantined (<field>: <message>)`), and the run continues and exits 0 — its
neighbours index normally. A record that parses cleanly on a later run has its
`index_errors` row deleted in the same run; a quarantined path whose file has
since been deleted has its row deleted too. `decision_index_state` reports
`quarantined` for a project with rows in `index_errors` (see the state table
below); `check` lists each one under its own `quarantined` key alongside
`added`/`changed`/`removed`.

A record that parsed CLEANLY (so it never reaches the quarantine path above)
but whose own database write then fails — a DB-level fault, not a parse/shape
problem — is a different, honestly-reported case: each record's write runs
under its own savepoint, released on success and rolled back on failure, so
one record's write fault can never touch its neighbours. `reindex` prints
`cannot index <path> (<Type>: <msg>); index integrity not guaranteed`, counts
it as an integrity failure (never as quarantined — that word is reserved for
a genuine parse/shape problem), and continues; a run with one or more
integrity failures exits **5**, after every record that DID write cleanly is
committed.

**Lazy imports.** `fastembed` (and, transitively, numpy) is imported only inside
`compute_embeddings`, `compute_query_embedding`, `load_embedding_model`, and
the branches of `cmd_search`/`cmd_code_search` that call them. `reindex
--no-embed`, `chain`, `for-path` and `search --mode fts` never trigger those
imports — asserted by a subprocess-isolated test
(`test_for_path_does_not_import_fastembed`), because `for-path` runs in a
pre-edit hook and must not pay a numpy import. `embedding_fingerprint()`'s
STATIC part (everything but `revision`, which needs a loaded model) reads
`importlib.metadata.version("fastembed")` — the installed package's dist-info,
without ever executing `fastembed/__init__.py` — so `check`'s
`vector_index_state` reports `mismatch`/`none`/`partial`/`full` without
importing fastembed either, verified the same way.

## Standing decisions

A ruling true of the whole project, always, has historically reached an agent
only by luck (editing a file that happens to sit in a topic's `code_refs`) or
by hand-copying it into a rules file nothing keeps in sync with the store.
The standing-decisions digest is the store's own answer: a topic marks which
of its own links are standing (§2/§7 of `docs/SCHEMA.md`), and
`sessionstart-remind.sh` hands the whole store's standing set to every
session, unprompted, before any edit.

**Marker.** `standing:` is a plain frontmatter list of the topic's OWN link
ids, written by whoever records the ruling — never derived from a topic's
area, its authority, or the presence or absence of `code_refs` (a topic with
no `code_refs` is not thereby standing, and one with `code_refs` is not
thereby excluded; membership is exactly the list, nothing else). `memlint.py`
enforces the eligibility gate at lint time (the memlint tables above): a
pointed link must exist in that topic, be `status: active`, carry
`ruling.authority` `owner-verbatim` or `owner-ratified` — the declared label,
the same one the pre-edit chain already cites verbatim; this digest does not
wait for a derived-authority scheme — and carry a single-line `ruling.text`
(a CR/LF is a lint error, not flattened for you at lint time; `standing_line`
flattens defensively at read time regardless, so a store that predates this
rule, or a `--no-verify` commit, still projects one physical line per
pointer). A store-wide cap (24 links, or 4,800 bytes, UTF-8, of the COMPLETE
digest below — header, every line, and the newlines joining them, never
characters of the lines alone — whichever is reached first, counted in the
projection's own order) keeps the set from growing into a second store
nobody can hold in their head; memlint's error names every topic whose
pointer sits past the line that was crossed. `cmd_standing` enforces the
identical ceiling again at runtime (`reason: over-cap`, refuses rather than
truncates) — a store committed with `--no-verify`, or never linted with
`--code-root` at all, must never have this reached a session unmeasured.

**Accepted v0 risk.** The eligibility gate is the DECLARED authority label,
not a derived one: a link mis-labelled `owner-verbatim` whose `source:` is
empty, or does not actually quote the owner, is delivered as standing exactly
like a genuine one, until a derived-authority scheme lands.

**Read-only, always.** `cmd_standing` opens the index through
`open_db_readonly`/`decision_index_state_readonly` (mode=`ro`) — NEVER
`open_db_noncreating`/`decision_index_state` (mode=`rw`), which runs every
migration guard on open (see "decision index provenance" above). A generation
gap is exactly the case this matters for: an rw open would ALTER the new
columns into existence before its own generation check could return
`upgrade-required`, so "no write path" would be false at the one moment a
version bump makes it most necessary — every project a newer engine has not
yet reindexed is in precisely that state. A `standing` refusal is therefore
never a courtesy; a read-only connection cannot write a byte regardless.

**Old-engine ping-pong guard.** `_classify_index_state` (the body every
reader's `decision_index_state`/`_readonly` shares) refuses `upgrade-required`
on a generation AHEAD of `CURRENT_INDEX_GENERATION` exactly as it already did
on one behind — a generation ahead used to read as plain `current` and would
let an older engine's own `reindex` re-stamp the OLDER number and silently
drop whatever column only the newer generation populates. This protects a
given engine build going forward against an even-newer stamp; it cannot
retroactively patch an already-deployed OLDER build that predates the check.
**Two engine versions must never be pointed at the same `MEMCONTINUUM_HOME`**
— on this project's own machine every wired hook calls one checkout by
absolute path, so this is a standing guard, not a live bug, but a second
checkout (a stale worktree, a pinned older clone) sharing the same
`MEMCONTINUUM_HOME` would trigger exactly the ping-pong this guard exists to
stop.

**Upgrading.** A `CURRENT_INDEX_GENERATION` bump (adding `standing` itself
bumped it) is not self-healing: `unmapped`'s self-heal fires only on
`stale`, `memcontinuum-update.sh` never reindexed on its own before this, a
hook or template change stales no render fingerprint, and the only things
that actually reindex a store are a store commit (`post-commit-reindex.sh`,
content-only) or a manual `reindex --full`. A project with no store commit
after the bump would otherwise sit at `standing=skipped-upgrade-required`
forever, silently. Three things now close that gap: `memcontinuum-update.sh`
(check mode) reports `index: upgrade-required (generation G < C)` for every
wired project whose stamp is behind, and `--apply` reindexes it
(`memidx.py reindex --root STORE --project P`); `sessionstart-remind.sh`
injects one short line into context, once per session, the first time a
session hits `skipped-upgrade-required` (a state flag, not a repeat nag); and
a generation bump's own release notes must say a wired project needs one
reindex to pick it up.

**Projection.** `memidx.py standing [--root DIR] [--json]` reads the index —
never the markdown directly; `standing` is indexed at reindex exactly like
`code_refs`/`tags`, on the topic's own `records` row — and re-applies the
SAME eligibility gate memlint enforces, rather than merely trusting a prior
lint run: this reader has no dependency on one having just happened. One
line per eligible pointer, ordered by topic id then link id (numeric-aware,
so a topic's tenth link sorts after its second, not before it):
`<topic id> <link id> (<authority>): <the ruling sentence>` — the sentence
alone, flattened to one physical line, never the rationale, never the chain
lines a human-facing `chain` render would add. A fixed header states the
store is the source of truth and this is a citation of it. `--json`'s `lines`
count always equals the number of physical lines the plain rendering would
print — never more, regardless of what a ruling's raw text once contained.
`--json` also adds a hash of the complete digest text (for the dedupe rule
below) and the byte count. Deterministic: two runs against an unchanged index
are byte-identical. Refuses — exits non-zero, names the state or
`reason: over-cap` — on every index state but `current`, and over the byte
cap; see the deviation note above.

**Delivery, one call.** `sessionstart-remind.sh` attempts the digest on every
`SessionStart`, computed strictly AFTER whatever state work that source
already does (session init on `startup`/`resume`/`clear`; pending
consumption on `compact`) — a kill mid-digest then loses only the digest,
never that already-committed bookkeeping. It calls `memidx.py standing --json`
exactly ONCE (a prior draft called it twice — once `--json`, once plain, to
get both the structured fields and the exact text — but a store commit
landing between the two calls could make the logged hash disagree with the
injected text; `--json`'s own `lines` already carry everything needed to
reconstruct the identical text: `HEADER + "\n" + "\n".join(lines)`, HEADER
being the same fixed, versioned literal `memidx.py` itself renders, so hash
and text now always come from the same snapshot, and the round trip costs one
process, not two). A refusal or an empty set injects nothing (logged
`standing=skipped-<state>` / `standing=empty`); otherwise the text becomes —
or, on `compact`, is appended after — the SessionStart `additionalContext`
that source already builds, coverage's own text staying first when it has
one.

**Dedupe.** `startup`/`resume` compare the digest's hash against
`state["standing_hash"]` inside the SAME locked state-update transform that
conditionally sets it (never a plain read followed by a separate write — two
concurrent `SessionStart` fires for one session could otherwise both read the
old hash and both decide to inject), logging `standing=dedup` on a match and
`standing=injected links=N bytes=B hash=H` otherwise. `clear` and `compact`
never dedupe — the context is gone either way, so both always inject when
there is something to inject, refreshing the stored hash so a LATER `resume`
in the same session dedupes against what was actually last shown. `compact`
persists that hash even when no session state file existed yet (it creates
one with just the hash) — otherwise a `compact` with no prior state could
never dedupe a following `resume`, and would re-inject the same digest twice
in a row for no reason.

**No write path.** The digest reads the index and, on an inject or a dedup
decision, writes into the session's own state file — never the store, never
a rendered file. An inject writes both `standing_hash` and `standing_ids` in
that same locked transform; a dedup writes nothing, except that when
`standing_ids` is absent from a pre-existing session state it backfills
`standing_ids` alone (the hash stays untouched). A hook run that computes
and injects a digest leaves the store's git status clean and its index
untouched — including against a generation-behind index, which the
read-only opener above makes structural, not merely tested-for.

**Subagents.** Whether Claude Code fires `SessionStart` for a subagent
session at all is not established one way or the other by anything in this
engine; this hook makes no claim either way and adds no `fork`-specific
handling. What is deliberate: a dispatch brief template is the channel that
carries the digest to a subagent ("subagents do not inherit a session's
context; the digest goes into every dispatch brief template so they do") —
not this hook.

Loaded is not applied — this raises the odds a standing ruling is honored, it
is not a guarantee.

## CLI semantics

`--help` on each subcommand is the flag reference. The semantics worth writing
down:

- **`search`** — `--mode fts` and `--mode vector` never both run; `hybrid` (the
  default) runs both and fuses ranks with RRF, unless FTS's own top-1 pick's
  content-term coverage of the query is below `FTS_STEP_ASIDE_COVERAGE`
  (0.25 — not zero overlap), in which case hybrid returns exactly `--mode
  vector`'s own result instead of fusing. `--json`'s envelope names which
  happened, every hybrid search: `"fusion": "rrf"` or `"vector-only"`; a
  step-aside also prints a one-line stderr note naming the measured
  coverage. `--status` defaults to
  active-only when omitted entirely (`_resolve_search_status`, applied inside
  `cmd_search` on a shallow copy of `args` -- `build_filter_clause` itself,
  and every other caller of it, keeps "no status given" meaning "no filter");
  `--status any` widens back to every status (superseded/historical/
  declined/provisional included), and any other explicit value (or several,
  repeated) passes through unchanged, exactly as before this default existed.
  `type: inbox` rows (search-inbox-downrank) are excluded the same way,
  independent of `--status`: `--include-inbox`, or an explicit
  `--type inbox`, widens back to include them.
  Filters (`--status`, `--type`,
  `--area`, `--topic`, `--authority`) are always ANDed, and are applied
  **inside** `fts_ranked`/`vector_ranked`'s own query, before either channel's
  cap and before RRF fusion — a status a caller filtered out can never occupy
  a rank position that starves out a real match, in any mode. `fts_ranked`
  runs its filtered `ORDER BY bm25(fts)` query with NO raw row limit,
  walking the ranked cursor in batches of 200 (`fetchmany`), resolving each
  batch's family in one query and collapsing same-topic-family duplicates
  (see link rows below) as it goes — stopping the instant 200 DISTINCT
  families have been collected, never a raw row cap before collapse can see
  them (a single family that alone contributes over 1000 matching rows used
  to be able to push a second family's own lone matching row past a raw
  `LIMIT 1000` cap, starving it out even though both matched and the true
  family count was nowhere near 200). The tradeoff: a match set with fewer
  than 200 distinct families is read to its end — every matching row, not a
  fixed raw ceiling — before the query returns. `vector_ranked` carries no
  cap at all — it scores every fresh embedded
  row for the project matching the filter — and joins `embeddings` to
  `records` on `embed_sha == sha256`, PLUS `embed_fp`/`dim` against the
  current model's real fingerprint and the query vector's length (see
  [Decision index provenance](#decision-index-provenance-and-embedding-lifecycle)),
  so a vector kept stale across a `--no-embed`/`--auto` edit, or one made by
  a different model/dimension entirely, is never a candidate at all. Each
  `--json` hit reports the topic's real path (never a
  link row's own synthetic surrogate path); a hit whose fused winner was a
  link row also carries `matched_link_id`/`link_status`/`link_authority` (that
  link's own `status`, checked independently of its parent topic's — `--status`
  filters each row's own status, with no OR against the topic), and
  `contributing_link_ids` names, per channel, which specific link (if any) that
  channel's own ranked list picked for this topic family, whenever hybrid
  mode's two channels disagreed. `check --json` reports `source_topic_count`
  (real topic files), `searchable_row_count` (topics *and* their link rows —
  every row `search` can return), and `searchable_vector_count` (rows with a
  fresh embedding) separately, so a store's growth from link rows is visible,
  not folded into one number that reads like topic growth alone.

  **No ANN index; a linear scan over every searchable vector.**
  `vector_ranked` scores every fresh row for the project one at a time —
  practical while that count stays small, a real cost once it does not. Run
  `memidx.py check --project <name> --json` on a real store for the actual,
  current numbers rather than guessing; this engine's own store measured
  42 topics, 72 total searchable rows (topics plus link rows), 72 fresh
  vectors. A real approximate-nearest-neighbor index is worth revisiting once
  a project's searchable vector count runs into the low thousands — comfortably
  above what any store here has reached yet.
- **`chain`** — one line per link, newest first: `kind`,
  `reverses`/`reason_for_change` when present, ruling (quoted for
  owner-verbatim/owner-ratified) and rationale, plus one indented edge line per
  typed cross-reference and a trailing `broken assumptions:` block. It is a
  deterministic adaptation of `docs/SCHEMA.md`'s illustrative chain view, not a
  byte-for-byte reproduction.

  **`edges_for_topic` is presentation, not reasoning.** It exists to feed
  `chain`'s own indented edge lines: its only three callers — `cmd_chain`
  (which passes the result into `chain_lines`/`chain_json`), `topic_chain_json`,
  and `print_topic_chain` — all exist to display a chain, never to decide
  anything from it. Nothing in `search`, `drift`'s HOLD/CONSTRAINT
  classification, or hybrid ranking ever reads the `edges` table — a typed
  cross-reference is shown to a reader, never consulted by the engine to decide
  anything. A dedicated traversal view over the edge graph itself (following
  `supersedes`/`led_to`/`challenged_by` chains across topics, rather than one
  topic's own edge lines) is backlogged, not built — a shape like `search
  --expand-edges` is the natural place for it if it is ever built.
- **`for-path`** — plain SQLite lookup, no embedding imports, safe on a hot
  path. Matches the queried path against every *topic's* `code_refs` (the part
  before `#`) — never a standalone incident/investigation record's, see
  `docs/SCHEMA.md` §9 — by exact match, segment-aware prefix match in either
  direction (a `/`-boundary check, so `src/foo.py.bak` never matches
  `src/foo.py`), or `fnmatch` glob; concept records add the same matching
  against `implemented_by`/`tested_by`. An empty or fragment-only `code_ref`
  (`""`, `"#Foo"`) names no path and matches nothing — `code_ref_matches` is
  the one path-matching helper every governance lookup here shares (`for-path`,
  concept attachment, `drift`'s `allowed` exemption), so this guard, and the
  segment-awareness above, apply uniformly everywhere a `code_ref` is
  consulted. See [Decision index provenance](#decision-index-provenance-and-embedding-lifecycle)
  above for its `2`/`3`/`4` exit codes.
- **`check`** — the one command that hashes every record (it walks the store
  ONCE, never loading the embedding model): every walked path with a stored
  row is read and its sha256 compared against `records.sha256`, so a
  same-size, same-`mtime_ns` content rewrite a metadata comparison alone
  could not see is reported as drift. A sha match whose mtime/size moved
  (a bare `touch`) is bookkeeping-refreshed in place — one `UPDATE` reaching
  both the topic/note row and every link row derived from it, since a link
  row's own `path` differs from its parent's but shares the parent's stat —
  and is reported neither `added`, `changed`, nor `drift`; a genuine
  sha mismatch IS `changed` (`changed` means content changed, not metadata
  moved). Exits 1 on any drift. Its `--json` report also carries
  `source_topic_count`/`searchable_row_count`/`searchable_vector_count` —
  see the `search` bullet above — `vector_index_state` (`none` / `partial` /
  `full` / `mismatch`, computed WITHOUT loading a model: `mismatch` when a
  stored `db_meta['embedding_fingerprint']` exists and does not match the
  STATIC current fingerprint — `revision` is always `"unknown"` on the
  no-model-loaded side, so a healthy db, embedded by any revision of the
  same model/pipeline, never reads `mismatch` here; otherwise the fresh-join
  count, gated on the row's `embed_fp` matching the static fingerprint's
  prefix) — and `symlinks_skipped`, the count of
  symlinked directories/files the walker skipped this run; and `state`
  (the same word `decision_index_state` reports, computed from THIS same
  walk rather than a second one) and `quarantined` — one `{path,
  diagnostics}` entry per row in `index_errors`, hashed (only these files)
  and compared against the sha `reindex` stored: unchanged stays reported
  under `quarantined`, changed content counts as `changed` instead (it
  will be re-parsed on the next reindex), and a quarantined file whose path
  has vanished counts as `removed`. Text mode adds `check: N record(s)
  quarantined` and one `! <path>: <field>: <message>` line per entry.
- **`unmapped PATH...`** — classifies each path as `mapped_topic`,
  `mapped_concept_only`, or `unmapped` without walking the code tree.
  `coverage_status` mirrors the decision index's states, collapsed for a
  *negative* claim's purposes: `"ok"` (state was `current`, queried normally —
  the only state that actually populates `unmapped`), `"uninitialized"`
  (`missing`/`uninitialized`, no query attempted), `"upgrade-required"` (no
  self-heal — that is the rollout's job, not an ad-hoc hook-triggered one),
  `"quarantined"` (`index_errors` holds rows for this project — no self-heal;
  a malformed record does not clear itself by reindexing again),
  `"index-error"` (a `sqlite3.OperationalError` while reading), or `"unknown"`
  (state was `stale`, self-heal ran, and drift still persisted afterward — the
  `sqlite3.OperationalError` path above is unchanged; a broader failure here
  additionally attaches an optional `"degraded": {reason_code:
  "internal-error", exception_type, safe_message}` object to the JSON and
  prints one `unmapped: degraded reason=internal-error type=<Type>: <msg>`
  stderr line, so a genuine read failure is distinguishable from a code
  defect without ever changing the collapsed `coverage_status` itself).
  `unmapped`'s own `stale` check, like `check`'s, is
  content-proven (hashed, not metadata-only) — a negative claim is exactly
  where a same-size, same-`mtime_ns` rewrite must not slip through as
  `current`. Self-healing only fires on `stale`: one `reindex --no-embed
  --auto` pass, then a recheck. This is what `userprompt-remind.sh`'s
  coverage signal and `precompact-persist.sh` call.
- **`why`** — resolves a symbol or path to its concept(s), then prints those
  concepts' `governed_by` chains in full, including any `kind: declined` link
  (there is no separate "rejected alternative" field; a declined link *is* that
  record). A bare symbol (no `/`) resolves to its defining file through the same
  registry-served declared-symbol scan the chunker and memlint use, in two
  steps: it first checks the code index's `chunks` table for this project — a
  stale index skips this fast path entirely (some root has unindexed drift, so
  a chunk any root reports as current could still be a false hit); a
  `--code-root` that is not one of the report's own recorded roots is skipped
  too; otherwise a matching chunk in a `degraded` or `current` index returns
  immediately, with no per-file freshness check (an index built from a
  now-edited file is trusted exactly like a fresh one), and `why` itself never
  heals — one call, one answer, never a side-effecting repair. A miss there
  (member symbols only — container names like class/struct/enum are never
  chunks themselves, so a miss is never conclusive) falls back to scanning
  `--code-root` directly; the disk scan dispatches by file language (each
  candidate's language is resolved the same way the indexer resolves it —
  extension first, then a shebang sniff for an extensionless file — against
  the full chunker registry, not just this project's stored language set),
  and a file with no resolvable language is skipped outright, so a bare
  Python (or any other registered language's) symbol resolves exactly like a
  Swift one.
- **`drift`** — checks every active or provisional link's checkable
  `invariant:` against a code tree, and classifies each into one of four
  buckets (`docs/SCHEMA.md` §4): an active `owner-verbatim`/`owner-ratified`
  invariant that fails is a CONSTRAINT `violation` — always fails the run; an
  active `reviewer-finding`/`code-derived`/`agent-inference` invariant that
  fails, *with validated evidence* (a real, non-blank list — a HOLD-eligible
  authority with no validated evidence is CONTEXT, not a HOLD), is a
  `hold_violation` — reported always, but only fails the run under
  `--strict-holds`; everything else that carries no failure, or is CONTEXT, is
  named in `skipped` with its reason (`"authority"`, a `status=…` other than
  active, or `check_invariant`'s own refusal for a bad `kind`/regex/scope —
  never a silent pass or a traceback). A `provisional` link's invariant is a
  fourth, orthogonal bucket: never classified, never checked, always reported
  in `revalidate` (plain output: `revalidate (provisional): <topic>/<link>
  <kind>`) for a human to re-confirm against live source — never a failure,
  even under `--strict-holds`.
- **`code-search`** — the same RRF formula as `search`, but with its own
  constant `CODE_RRF_K` (30, provisional) where the store `search` uses k=60.
  Each hit optionally carries a
  `concept_id` when a concept's `implemented_by`/`tested_by` claims that exact
  symbol (preferred) or its containing file; attachment always reads the
  *decision* db, overridable with `--decision-db`, and only for a hit whose
  file still exists under the root it was indexed from — a hit whose file is
  gone from its root (moved, deleted, or one relative path indexed under two
  roots with only one still holding the file) never attaches a concept keyed
  on that path alone, single- or multi-root alike; two roots sharing a
  relative path is only the case that makes the guard visible, since a
  single-root project can drift the same way. Every call resolves an
  index-provenance state first (see [Root-scoped indexing](#root-scoped-indexing)
  for the full state table) and, unless `--no-heal`, attempts the one-shot heal
  described there before answering. **uninitialized** refuses outright (exit
  1, error on stderr, `results` is `[]` in `--json` too, since an empty list
  would otherwise read as a real "nothing found"); **stale** and **degraded**
  both warn on stderr and still search, naming the failed-file count and any
  missing root separately when they apply. Text mode prints nothing extra for
  **metadata-current**, the normal state — only **stale**/**degraded** get a
  line. `--verify-content` hashes every indexed file (in every recorded root)
  before answering, so a clean report proves **current** rather than the
  metadata-only default; it is passed into the heal's after-report too, so a
  healed index reads **current** exactly when the caller asked for proof.
  There is no automatic verification on an empty result — a nothing-found
  answer stays honest uncertainty under **metadata-current**, evidence only
  under **current**, never silently upgraded. `--json` wraps hits in
  `{"state", "code_root", "code_roots", "indexed_at", "head_sha", "changed",
  "failed", "not_indexed", "embedding_mode", "embedding_fingerprint",
  "results"}` — `embedding_fingerprint` is the STORED `code_project`
  fingerprint (`None` on a project never embedded), always present
  regardless of mode — where each
  `code_roots` entry also carries its own `git_delta` (`"unavailable"` — no
  stored `head_sha` to compare, or git failed/timed out; `"unchanged"` — HEAD
  hasn't moved; `"verified"` — HEAD moved and every path the diff touched in
  this root proved clean; `"changed"` — HEAD moved and the diff hashed a real
  change) and `verified` (whether THIS call hashed the whole root — never
  `True` for a root that does not `exist`). A vector/hybrid call whose
  fingerprint check finds a mismatch (only checked when the project already
  has embedding rows) skips the vector query entirely: `"embedding":
  "fingerprint-mismatch"` (the same slot that reads `"unavailable"` on a
  broken backend), stderr names `<stored> vs <current>`, and results are the
  FTS-only list; a per-row dimension mismatch adds `dimension_mismatch_rows`.
  A db written by a newer engine (see `CodeIndexTooNew` above) never reaches
  `code_index_report` at all — `code-search` exits 0 with the refusal on
  stderr and, in `--json`, a minimal `{"state": "unavailable", "code_root":
  null, "indexed_at": null, "head_sha": null, "results": []}` (no
  `code_roots`/`changed`/`failed`/`not_indexed`/`embedding_mode`/
  `embedding_fingerprint`, since none of those were ever computed).

## Test conventions

Canonical run, from the checkout root:

```bash
export MEMCONTINUUM_PYTHON="$PWD/.venv/bin/python"
PYTHONPATH= "$MEMCONTINUUM_PYTHON" -m unittest discover -s tests
bash tests/run_bash32.sh
```

- Tests do not fall back to `<checkout>/.venv/bin/python` the way
  `repo-init.sh` does: they read `$MEMCONTINUUM_PYTHON` and skip with a clear
  message when the tests that need a real venv cannot get one.
- **The venv gate itself is guarded.** `tests/test_env_gate.py` fails the run
  (naming `$MEMCONTINUUM_PYTHON` and the live count of test classes that
  would silently skip -- computed by re-walking the discovered suite, not a
  hardcoded number) unless the variable is set or `$MEMCONTINUUM_ALLOW_UNGATED=1`
  is, so a machine without the pinned venv gets one loud failure instead of a
  quiet "OK (skipped=N)" that reads like full coverage. CI already sets
  `$MEMCONTINUUM_PYTHON` (`.github/workflows/tests.yml`), so this never fires
  there.
- Nearly every test builds its own temp directory and passes an explicit `--db`
  (or sets `MEMCONTINUUM_HOME`), so a run never touches a real
  `~/.memcontinuum/` index. The gated real-corpus tests are the exception,
  described below.
- **Privacy is a test.** `TestNoMachineIdentifyingContent` walks `git ls-files`
  and refuses this development machine's username, its checkout path, and the
  private codebase this engine is benchmarked against — its name and its module
  prefixes — anywhere in tracked content, `tests/` included. `LICENSE`'s
  copyright line is the one exception. Anything project-specific (corpus roots,
  probe queries, expected symbol names) therefore lives in untracked files.
- **Gated tests.** Anything that asserts real hits in the untracked incident
  corpus (`fixtures/records/incidents/`) is gated on it and skips with a message
  naming the directory — `test_memidx.py`'s D1, whose two other queries only
  resolve there; `$MEMCONTINUUM_TEST_INCIDENTS` points the gate elsewhere. D2
  also builds from that corpus but asserts only against the tracked schema
  fixture, so it passes on a fresh clone. D5 reads
  `fixtures/records/queries.json` and skips cleanly when absent; D8's timing test
  skips without `$MEMCONTINUUM_TEST_SANDBOX_SYNTH`.
  `test_code_index.py`'s `TestGoldProbesRealCorpus` is double-gated on
  `$MEMCONTINUUM_TEST_REAL_CORPUS` **and** an untracked probe file
  (`$MEMCONTINUUM_TEST_PROBES`, default `docs/internal/gold-probes.tsv`).
- **Two deliberate exceptions to "everything stays in a temp dir",** both for
  cost: the real-corpus probe test reindexes into a reused cache at
  `~/.cache/codanna-bench/bench-code.sqlite` (a full embed of a real corpus runs
  20+ minutes), and `scripts/codanna-bench.sh` — a manual benchmark, not part of
  the suite — caches its comparison binary in the same directory.
- Every tracked fixture is synthetic: invented dates, invented rulings, a
  fictional example app. See `fixtures/payloads/README.md` for the same
  guarantee about hook payload fixtures.
- **Hook payload fixtures are synthetic and say so.** `fixtures/payloads/` holds
  hand-written `UserPromptSubmit` payloads built from the documented event
  shape — no live payload has been captured into this repo. A synthetic fixture
  that has drifted from the real event shape fails silently, which is the worst
  failure mode a fail-open hook can have, so the real shape is observed at
  runtime instead: `hooks/userprompt-remind.sh` writes a `payload_keys=…` line
  (sorted top-level key names only, never values) to `hook.log` on a session's
  first qualifying turn. Diff a captured payload against that line rather than
  assuming the fixtures are it.
