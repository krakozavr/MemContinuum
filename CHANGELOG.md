# Changelog

## [Unreleased]

- `code-search` now separates code from tests. A chunk's role is worked out at query time from its path and language (`tests/`, `test_*.py`, `*Tests.swift`, `*.spec.ts`, `src/test`, and so on), so there is no reindex. By default only code is returned; `--tests` returns tests only and `--all` returns both with tests marked `[test]`. Each code hit also gets a `tested by:` line (JSON: `tested_by`) naming up to three test chunks whose name, signature or doc mentions its symbol. JSON hits gain `role`, and the envelope gains `role_filter`; existing keys are unchanged.

- Code-search hybrid ranking now fuses with RRF constant k=30 instead of 60 (provisional: on the 45 graded intent queries top-10 hits rose from 20 to 23 of 35 with no loss; it is re-measured at the next relevance re-check). The store search keeps k=60.
- A code root you removed from a repo's wiring no longer comes back. The code
  index remembered every root it had ever indexed, and the new background
  worker (and the heal) processed all of them, so an old whole-repo root sat
  beside a new narrower one and every symbol appeared twice. `code-embed-worker`
  now requires `--root DIR` (repeatable; the installer and the refresh hook pass
  the wired roots) and skips any recorded root not named; the heal skips them
  too; `code-refresh.sh` drops them on `Stop` (`dropped=<n>` in `hook.log`) and
  only marks them on `SessionStart` (`stale-root=<n>`). A relative or unresolvable wired root, or a wiring that names no recorded root, disables the
  drop (`detail=stale-check-skipped:...`), and the worker's text pass refuses a
  root that is no longer recorded.
- Vector search scores faster. The cosine scoring that both `search` and
  `code-search` share now runs as one numpy matrix product when numpy is
  installed (it is, wherever embeddings work), about ten times faster on a
  3,400-chunk code index; without numpy it uses the previous per-row loop.
  Rankings and scores are unchanged.
- Code embeddings now get built, in the background. Until now every code index
  had zero vectors: the first `code-reindex` at install is `--no-embed` to keep
  the install fast, and nothing ever filled them in afterwards, so `code-search`
  was text-only in every mode. A new `code-embed-worker` (detached, one at a time,
  same marker/lock/log pattern as the store's `embed-worker`) now does it. The
  installer starts it after the first index, and `code-refresh.sh` starts it on
  `Stop` and `SessionStart` whenever chunks lack a vector, so an existing index
  fills itself the first time you open or finish a session after upgrading;
  `embed=spawned|clean|off` in the `code-refresh` `hook.log` line says
  which. Vectors are committed in batches of 128, so a killed worker keeps its
  progress. `embedding_mode` is now `none`, `partial` or `full`, recomputed from
  what is really embedded after every pass; the `code-reindex` summary ends with
  `N chunk(s) awaiting embedding`, and `code-search` says how many chunks have no
  vector yet (and still finds them by text). The Stop-time refresh stays
  text-only, so a refresh of a fully embedded index reads `partial` until the
  worker catches up; set `MEMCONTINUUM_EMBED_WORKER=0` to disable the worker.
- Embedding is about 8 times faster on CPU. The engine now loads the plain
  float32 ONNX file of the same model (`BAAI/bge-small-en-v1.5`) instead of
  the quantised build fastembed picks by default, whose main matrix multiply
  does not use more than one core: roughly 40 ms instead of 0.3-0.4 s per
  chunk, so a 3,300-chunk index takes about 2.5 minutes instead of 22. The
  vectors are the same (cosine 1.0000 against the old ones on real chunks), but
  the index records a new model fingerprint, so after upgrading the background
  workers re-embed existing vectors once (store records and code chunks both
  count as awaiting embedding until then). The first embed downloads about
  133 MB of model files; CI's model cache key moved to `v2`.

- Every store commit can now write a verified `git bundle` backup of the
  store: set `memcontinuum-setup.sh --backup-dir DIR [--backup-keep N]` and
  the post-commit hook keeps the newest N bundles per store in `DIR/<project>/`
  with a manifest line each. `memcontinuum-update.sh` shows the setting on the
  machine line and each store's state (off, none, current, behind,
  unreadable, unknown) in a new `backup` column. The bundle is never written
  outside `DIR` (project names `.` and `..` are refused, a symlinked project
  folder pointing elsewhere is refused), the bundle just written is never
  pruned, a `--backup-keep` such as `00` or `08` is rejected or normalised
  instead of being read as octal, and a half-written file left by a killed
  hook (`<name>.part.<pid>`) is swept by a later run once it is older than twice
  the hook budget (60 s by default), so a live overlapping hook's file is left alone.
- The post-commit hook now runs the commit checks again, so a store commit
  made with `git commit --no-verify` that would have been refused prints a
  warning, is logged, and is named at the next session start until a clean
  commit follows. An engine crash (or a lint that prints nothing) never clears
  that warning; a warning left by another store with the same project name is
  neither cleared nor overwritten (the second store's own finding is then only
  printed and logged, `marker=foreign`); and without the watchdog python
  neither the re-lint nor the backup runs. A project name that could leave
  its folder (`..`, a slash, a newline) makes the hook skip both the re-lint
  marker and the backup; a bad backup directory skips only the backup, the
  re-lint still runs. Every file the hook creates (bundle part, marker temp)
  is created exclusively (the bundle is written through the exclusive open itself) and never through a planted symlink, and a bundle or
  marker path that is a symlink or a directory is refused instead of being
  moved into.

- The code index now refreshes itself. A new `code-refresh.sh` hook, wired on
  `Stop` and `SessionStart` for any project with a code root, checks the index
  with the same staleness test `code-search` uses (file stat signals, a content
  hash on drift, the git HEAD trigger). At the end of a turn it re-indexes what
  changed, including files deleted since, in the same incremental text pass
  `code-search` already makes; embeddings are not part of it. At session start
  it only checks and, if the index is behind, writes
  `<project>.code-stale`, which the next session start names in one line (the
  first turn end repairs it). The marker is honest about failure: `refreshing`
  stays behind if the hook is killed mid-run, and a failed pass, a pass refused
  by the heal limit (`MEMCONTINUUM_CODE_REFRESH_LIMIT`, default 500) or an
  exception records its reason there. The hook prints nothing, always exits 0,
  logs one `hook.log` line per run, runs under the watchdog (8 s on `Stop`,
  `MEMCONTINUUM_CODE_REFRESH_BUDGET`; 2 s on `SessionStart`) and never creates a
  code index: a root that was never indexed is only counted. A lock keeps two
  refreshes, or a refresh and a `code-search` heal, from writing one index at
  once; the search skips its heal with a one-line notice instead. No git hook is
  installed in your code repositories: a commit made during a session is caught
  at the next turn end, one made outside any session at the next session start.
  Re-run `memcontinuum-update.sh --apply` to wire it into existing projects.
- The code index no longer reads files your `.gitignore` excludes. Inside a git
  work tree the walk now takes the file list from `git ls-files` (tracked plus
  untracked, minus ignored); outside one, or without git, it walks the
  directory as before and says so on stderr. Ignored content an earlier walk
  had indexed (a cloned corpus under `docs/`, build output) is dropped by the
  next refresh. The language census counts the same files.
- `heal_code_index` now names its caller in its messages, can hand them back
  instead of printing, and reports why it did not heal (over the limit, a
  failure, or a held lock).

## [0.3.0] — 2026-10-15

Upgrading: the decision index schema generation moves from 5 to 6 (it now
stores which links are standing). A wired project's index is not migrated by
the hooks that only read it, so run one `memidx.py reindex --root STORE
--project NAME` per project after updating, or let
`memcontinuum-update.sh --apply` do it (see "Standing decisions" below).
Until then the session-start digest reports itself as skipped.

### Standing decisions
- A topic can now name which of its own links are true of the whole project,
  always, with an optional `standing:` list in its frontmatter. Membership is
  exactly that list: never inferred from a topic's area, its authority, or
  whether it has `code_refs`.
- Every session now starts with those rulings, unprompted, before any edit:
  `sessionstart-remind.sh` injects a short digest of citations (each ruling's
  own sentence, never a summary or rationale). A session that resumes with an
  unchanged digest is not told twice; after a `clear` or a compaction it is
  delivered again because the context is gone.
- `memidx.py standing [--root DIR] [--json]` prints the same digest by hand.
  It reads the index only, opens it strictly read-only, and refuses (rather
  than serving something possibly out of date) when the index is not
  current, including an index written by a newer engine than the one asked.
  It also refuses rather than truncates when the set is over the cap.
- The set is capped store-wide at 24 links or 4,800 bytes of the complete
  digest, whichever is reached first. `memlint.py` rejects a store over the
  cap, a `standing:` entry that points at a missing, non-active, or
  non-owner-authority link, and a standing ruling whose text contains a raw
  line break.
- A topic already delivered in the digest is not repeated by the
  prompt-derived search guess later in the same session.
- The generation bump is not self-healing, so it is now surfaced:
  `memcontinuum-update.sh` reports `index: upgrade-required` for every wired
  project whose index is behind, and `--apply` reindexes it. An index that is
  ahead of the running engine is reported but never touched. The session
  start also tells the agent once, in one line, when it had to skip the
  digest for this reason.

### Search fallback
- When an edit lands on a file no decision is bound to, the pre-edit hook now
  searches the store on the path's own words and hands the agent the nearest
  decisions, labelled plainly as a guess ("may be unrelated"), never as a
  match. A brand-new file gets the same guess appended to its existing
  reminder. Nothing is bound by it; the agent still decides whether it
  applies.
- The search is read-only: it opens the index strictly read-only, never
  migrates a schema to answer a guess, and a schema gap refuses by name.
  `memidx.py search` gains `--read-only`, `--hydrate` (include each hit's
  rendered chain), `--hydrate-max-bytes`, and `--decisions-only`.
- Caps keep it small: at most two hits, and each hit's chain is limited to
  `MEMCONTINUUM_FALLBACK_MAX_BYTES` (default 2000 bytes), with older links
  replaced by an explicit "omitted" marker. The path query is at most 12
  words. A path under the store itself is skipped.
- Results are decision records only (topics, their links, incidents,
  investigations): the store README, source records, inbox drops, and
  concepts are never offered as a guess.
- The new-file hook now queries the same remapped path the edit hook does,
  so a file written inside a worktree is searched on its real project path,
  not on the worktree's directory name. A strip-prefix containing `*`, `?` or
  `[` is matched literally, never expanded against directories on disk.
- The second hit now has to earn its place. A hit after the first is shown
  only if its fused (RRF) score is at least `MEMCONTINUUM_FALLBACK_SECOND_FLOOR`
  (default 0.0314, a provisional value due a re-check on fresh data); `0`
  disables the floor, and an unparseable or negative value falls back to the
  default. The floor applies only when the search proves RRF scores: it stays
  off when the vector channel was missing or partial. With the default floor,
  a run where one channel returned nothing always hides the second hit
  (single-channel scores cannot reach it); with `0` or a floor of 0.0161 or
  less it is shown. A dropped hit is
  logged as `floor_dropped=<id>` (`fb_floor_dropped=<id>` on the new-file
  hook's line) and is not remembered as shown.
- `MEMCONTINUUM_FALLBACK_MODE` (`hybrid`, `vector`, `fts`) overrides the
  search mode; the default stays `hybrid`.
- New `hook.log` outcomes: `search-fallback` (hits, ids, mode, query,
  timing) and `search-fallback-empty` (with a `reason=`). A search killed by
  the watchdog is recorded with `fb_started=1`. `memidx.py stats` reports all
  of it in a new `fallback` block, including timing percentiles.

### Prompt-derived queries
- An opt-in channel on the prompt hook can search the store on the words of
  your own prompt and hand over at most one nearest decision per prompt,
  labelled as a guess. It is off by default and is switched on one project at
  a time: a line with the project's name in `prompt-query.projects` under
  `MEMCONTINUUM_HOME`, or `MEMCONTINUUM_PROMPT_QUERY=1`. `MEMCONTINUUM_PROMPT_QUERY=0`
  forces it off and wins over everything else.
- The prompt text is never stored. It is tokenized in the one process that
  reads the payload and only the derived terms leave it; the raw text is
  never written to a file, session state, or `hook.log`. The log carries the
  terms (`q=`) only. A secret pasted into an opted-in project's prompt can
  therefore leave a fragment in `q=`.
- A prompt with fewer than four content words does not search at all, and
  conversational filler ("please", "explain", "need" and the like) does not
  count towards that. The search is full-text only, and a topic already shown
  this session is never repeated. A prompt that is redelivered after an
  interrupted run never yields a second topic.
- Machine-delivered messages are skipped, not searched: subagent reports,
  background-task notifications, cross-session messages, and harness framing
  are recognised by a fixed list of opening markers (`reason=non-user`). A
  message whose framing the list does not know is treated as a typed prompt.
- With the channel off, the prompt hook still never reads the prompt text.
  `memidx.py stats` gains a `prompt_query` block.

### Search ranking
- Hybrid search now steps aside to vector-only ranking when the full-text
  channel's top hit shares almost no vocabulary with the query (content-term
  coverage below 0.25), so noise from the full-text side is not fused into an
  otherwise accurate vector ranking. `--mode fts` and
  `--mode vector` are unchanged.
- `memidx.py search --json` now says what happened on every hybrid search:
  `"fusion": "rrf"` or `"vector-only"`, and a step-aside prints a one-line
  note on stderr naming the measured coverage.

### Store integrity
- The append-only check now freezes a recorded link's exact bytes, not only
  its parsed fields. A link rewritten through a YAML dumper, which can keep
  every field while changing quoting, key order, or wrapping, is refused
  as `link reformatted, not appended`. A link's valid lifecycle move is still
  judged by the field rules alone.
- Newest-first order is enforced: a new link inserted below one that was
  already recorded is refused as `link inserted out of order`.
- A file under `topics/`, `incidents/`, or `investigations/` whose first line
  is not `---`, or whose frontmatter has no closing `---`, is now a lint
  error, since without the fence the file would parse as an empty note with no
  schema check and no append-only protection.
- The store's `pre-commit` hook now also runs the plain schema check and
  blocks the commit on any error (warnings pass), which catches a brand-new
  malformed file that the append-only comparison alone cannot see.
- Cited commits and `file:line` citations are verified when `memlint.py` is
  given `--code-root`. A citation that resolves to something wrong (the
  commit exists but its quoted subject differs, or the file has fewer lines
  than cited) is an error; one that resolves to nothing is a warning,
  because it may cite another repository; an absolute-path citation is an
  error. `--strict-citations` promotes the unresolved warnings to errors, for
  a store that cites only the wired repository.
- Two checks judge new links only, so records written before them do not
  block commits: the allowed `kind` vocabulary, and the rule that an
  `owner-verbatim` ruling is not a question. Under `--against-ref` a
  violating new link is an error; in a plain run it is a warning. An unknown
  `kind` keeps warning whatever the link's status; the question-form warning
  applies only while the link is `active` or `provisional`, so superseding
  such a link clears it.

### Hooks
- `hook.log` is now rotated with bounded retention. Once a session starts
  with the file over `MEMCONTINUUM_LOG_MAX_BYTES` (default 5242880 bytes), it
  becomes `hook.log.1`, older files shift up, and nothing beyond
  `hook.log.$MEMCONTINUUM_LOG_KEEP` (default 12) is kept, so a collection window
  survives more than one rotation. Lowering `MEMCONTINUUM_LOG_KEEP` takes effect on the next
  rotation. A rotation interrupted by the session-start watchdog is
  recovered on the next one. `memidx.py stats` reads every retained file, so
  a `--days` window that spans rotations still sees all of it.

### Benchmark
- The retrieval benchmark gains 12 long exact-term queries (a literal anchor
  term inside a 12-20 word agent-shaped question) and reports how often
  hybrid's step-aside fired on them and whether it helped or hurt
  (`hybrid_stepaside` in `--json` output). It measures the risk and gates
  nothing.

### Documentation
- The README describes the search fallback, the opt-in prompt-derived
  search, and the session-start standing digest, and lists `memidx.py
  standing` in the command reference. The schema and internals documents
  cover `standing:`, the citation checks, the byte-level append-only rules,
  and every channel, log key, and default above.

## [0.2.0] — 2026-09-15

### Tests
- The suite no longer builds a virtual environment in its own checkout: a
  bare `--apply` in an updater test (the one proving the apply-row walk
  fails on every unfinished row) reached the real installer script once
  the machine layer became default-on, silently leaving a `.venv` behind
  next to the code. Six tests that depend on no engine `.venv` being
  present now skip with a stated reason when one exists,
  instead of failing on a precondition they do not control, and a loud
  precondition check names the requirement directly.

### Documentation
- The README and the pre-edit hook's own header comment now say what
  actually happens: the governing decision chain reaches the model
  together with that edit's own tool result, in the same turn, and shapes
  what the agent does next -- not the content of the edit that triggered
  the lookup. The hook still runs before the edit lands on disk, though it
  fails open -- it never blocks or alters the edit -- and retrieval is
  still automatic.

### Repository hygiene
- `.claude/` is per-machine rendered wiring and is now ignored
  root-anchored, rather than only its `skills/` subdirectory -- the rest of
  the directory sat untracked and un-ignored, one `git add -A` away from a
  public repository. The machine-identifying-content guard's failure
  message now names what a flagged file risks and the two legitimate
  fixes, instead of reporting only that something was found.

### Hooks
- An edit inside a worktree of a wired repository is no longer invisible
  to the hooks: a worktree checked out inside the code root gets the same
  retrieval and the same ledger entry a main-checkout edit gets. A
  worktree checked out as a sibling of the root gets the ledger entry
  only, because the hook that matches on the edited path never fires
  there under current settings. Two new hook.log outcomes replace the old
  silent fallback into an ordinary no-match or out-of-scope line, which
  looked the same as a genuine one: `worktree-unwired` names a confirmed
  worktree whose own main repository is not itself a configured root, and
  `worktree-unresolved` names one git itself could not settle. A project
  that wants retrieval inside its own worktrees should keep them under the
  code root (`<root>/.worktrees/`, ignored by the project), rather than as
  siblings of it.

### Code index
- The code-tree walk that feeds `code-reindex` no longer descends into a
  nested linked worktree checked out inside a code root and indexes its
  content as the root's own: any directory whose own top holds a `.git`
  file, rather than a directory, is pruned from the walk, which also
  excludes an ordinary git submodule's content under the same rule.

### Benchmark
- Three measurement harnesses now live under `bench/`, run against real
  Claude Code sessions rather than derived from documentation. One
  establishes when a PreToolUse hook's context actually reaches the model
  relative to the tool call it gates, reading the answer off the CLI's
  own ordered event stream instead of the model's self-report. A small
  scenario-driven stand-in hook and a second harness together establish
  what a hook's rewrite and denial mechanisms actually do: what a rewrite
  can change, what an invalid rewrite produces, whether a denial's reason
  text changes what the model writes on retry, and whether a hook that
  always denies can hang a session. Each harness builds and tears down its
  own disposable fixture, with a per-run unguessable marker and a
  contamination check confirming it appears in neither the prompt nor the
  project tree.

## [0.2.0rc5] — 2026-09-10

### Decision index
- `pre-edit-chain.sh` now names which topics a matched edit actually
  injected: a `matched`/`index-stale-served` hook.log line carries a new
  `topics=` field (sorted, comma-separated topic ids, capped at 10 with a
  trailing `+N` for the rest) between `elapsed=` and `project=`, turning a
  matched edit into a gradeable retrieval-quality sample. `memidx.py stats`
  reports it under the `pre_edit` block as `topics_named`, `topics_distinct`,
  and `top_topics` (top 5 by injection count). Absent entirely on any outcome
  other than a real match, and on an older log line from before this field
  existed — both parse and contribute nothing.
- hook.log now rotates instead of growing forever: `sessionstart-remind.sh`
  moves it to `hook.log.1` (replacing any previous one) once it exceeds
  `MEMCONTINUUM_LOG_MAX_BYTES` (default 5 MiB), once per session, never on
  the append path itself. At most two files ever exist; `memidx.py stats`
  reads both so a `--days` window spanning a rotation still sees the
  rotated-out side, and data older than the previous rotation is gone by
  design.

### Hooks
- A supported language that appears unwired is now a decision point put to
  the human, not a log-only notice: the new-file hook offers a numbered
  choice -- wire it now, or never mention this extension again, the second
  answer recorded permanently through the existing `--never-ext` mechanism
  rather than a second one.
- Fixed: the new-file hook used to offer to wire a language for a file the
  code index refuses to index in the first place -- a `.d.ts`, `.blade.php`,
  or `.min.js` file matched the hook's plain extension glob even though the
  chunker excludes exactly those compound extensions outright, so wiring
  the named language would never have caused the file to be indexed.
  Harmless while the hook only logged an outcome; a wrong question once
  that outcome became a decision point placed in front of a human.

### Install and update
- The updater now reports the machine layer (the user-level skill copy and
  the detector hook `memcontinuum-setup.sh` installed) by default instead
  of only under a flag -- evaluated only on request, it let a user-level
  skill copy drift for nine days while every routine run printed "ok" for
  the layers it did check. `--machine` is kept, now an accepted no-op;
  `--no-machine` is the new opt-out, for a caller that cannot afford the
  extra line or must never shell out to `memcontinuum-setup.sh`. Every
  artifact the installer renders is now accounted for in the health table:
  the store's two git hook wrappers (`post-commit`, `pre-commit`) gained
  their own column, and the store's `README.md`, `.gitignore`, and
  directory tree -- write-once files this command never re-renders -- are
  named in a `not-checked:` footer line beneath the table instead of being
  silently absent from it. A test derives the full artifact list from a
  real install's own before/after filesystem diff, so a newly rendered
  artifact the health table never mentions now fails the suite.
- Fixed: the README's manual `--uninstall` steps used to identify hook
  entries and a shared skill/rules copy by basename alone -- safe only when
  one project's wiring lives in a `<claude-dir>`, and wrong the moment a
  second project shares it, since every project's entries use the same
  basenames. The steps now filter hook removal on the project name each
  entry's own command string carries, and warn before deleting a shared
  skill or rules copy another wired project still reads.

### Documentation
- Manual wiring -- running the `memcontinuum` skill -- is now documented as
  the official, supported way to set up a repository. The SessionStart
  detector that flags an undecided repo speaks only through a channel the
  assistant sees, never the human directly, and nothing compels an
  assistant to surface it, so the documentation no longer promises a user
  will be asked. The detector itself is unchanged and stays in place, now
  carrying a comment at its own source saying it must not be promoted, in
  any user-facing doc, as a way users get asked.
- Corrected documentation claims that no longer matched behavior: the
  installer's closing message and `--help` text, the updater's `--help`
  text, the claim that "whatever you answer is recorded" (untrue of
  declining to answer, which records nothing and leaves the repo
  undecided), the README's lookup-latency figure (now a dated sample --
  105 logged lookups across two projects, zero or one second on the log's
  own resolution, the deadline never firing in that window -- naming store
  location as what actually governs the number, with the slow case, a
  store on a Windows drive, already the installer's default to move off
  of), and a related headroom figure in `docs/INTERNALS.md` that had
  compared hook.log rows from before a store's own move off a Windows
  drive against where it lives today -- withdrawn rather than restated as
  some other multiplier.

### Benchmark
- A first retrieval benchmark now lives under `bench/`: a synthetic corpus,
  a query set, and three runners -- this project, a keyword baseline, and a
  no-memory control -- behind one scoring interface. It is additive only;
  no engine file changed to produce it.
- A negative control makes the benchmark's own numbers checkable: every
  runner is also scored against a deterministic, kind-preserving shuffle of
  the query list, and must beat that shuffled twin by more than the run's
  own sampling noise, or the run prints inconclusive and names the runners
  rather than a flattering number. What it catches is exactly what is
  claimed -- a runner that is stateless and blind to the query text -- and
  it does not catch a runner that persists state across calls; the code,
  the README, and the printed output all say so.
- Evaluation data is still accumulating everywhere in this release: the
  benchmark's own numbers, and the real-machine figures corrected above,
  are all early, short-window reads. Treat them as provisional and expect
  revisions as more runs land.

## [0.2.0rc4] — 2026-09-07

### Decision index
- The store walker no longer follows symlinks -- a symlinked directory or
  file inside the store is skipped with a warning instead of walked, and
  `check` counts the skips.
- A malformed or wrongly-shaped record no longer crashes `reindex` or
  `memlint` -- it is quarantined (its previous rows purged, one row written
  to a new `index_errors` table, one stderr line naming the file and field),
  its neighbours index normally, and the run exits 0. The index reports a new
  `quarantined` state until the record is fixed or removed; `check --json`
  lists each one, and `unmapped` refuses the negative claim off a quarantined
  store the same way it already does for an uninitialized one.
- `check` and `unmapped` now hash every record instead of trusting mtime/size
  alone -- a same-size, same-mtime content rewrite (a metadata-preserving
  restore, a coarse-timestamp filesystem, some sync tools) used to read as
  no drift; it is now reported under `changed`, `check` exits 1, and
  `unmapped` refuses the negative claim off it. A bare `touch` (mtime moves,
  content unchanged) is bookkeeping-refreshed in place and is no longer
  reported as drift -- the inverse of `check`'s old behaviour. `search`,
  `chain`, `for-path`, `why`, and `drift` keep the metadata-only comparison
  (unchanged) -- `check`/`unmapped` are what prove content, not every reader.
- Every vector now carries a fingerprint of the model that produced it
  (model name, dimension, pipeline version, normalization, the installed
  fastembed version, and the loaded model's revision) -- a vector made by a
  different model or dimension is never mixed into a ranking. A stale or
  foreign-model vector is excluded from ranking the same way a stale
  `embed_sha` already was, not merely scored lower. `search`'s vector/hybrid
  modes detect a model swap at query time and fall back to FTS-only with a
  named stderr line and `"embedding": "fingerprint-mismatch"` in `--json`,
  rather than silently degrading; a reindex with embedding enabled detects
  the same mismatch and re-embeds every row, once. `cosine` now rejects a
  dimension mismatch and a non-finite vector component with a typed error
  instead of silently truncating; a backend that returns the wrong number
  of vectors for a batch writes nothing rather than mis-assigning them.
  `check --json` gains `vector_index_state` (`none`/`partial`/`full`/
  `mismatch`), computed without loading the embedding model.
- `unmapped` no longer folds a genuine programmer bug into the same silent
  `unknown` a real read failure gets: a broader internal error now attaches a
  typed `degraded` object (`reason_code`, `exception_type`, a short safe
  message) to the JSON, names it on stderr, and appends the traceback to a new
  `memidx-debug.log` -- `coverage_status` itself is unchanged. `stats`'s own
  fail-open catch-all is named the same way. A new `--debug` flag re-raises
  instead of degrading, for local debugging. Each record's write in `reindex`
  now runs under its own savepoint -- one record's write failure can no longer
  affect its neighbours, is reported honestly (never mislabeled as a
  quarantine), and a run with one or more such failures exits 5 after every
  other record is still committed.
- `memidx.py`'s per-database companion files -- the embed-worker's marker,
  lock and log, and `memidx-debug.log` -- now land beside the database a
  command is actually serving (`Path(db_path).parent`) rather than always
  under `$MEMCONTINUUM_HOME`; a custom `--db` moves them with it. The one
  caller that always builds `--db` under `$MEMCONTINUUM_HOME`
  (`post-commit-reindex.sh`) is unaffected; `backend-preflight`, which has no
  database in scope, still falls back to `$MEMCONTINUUM_HOME` for its own
  debug log.
- Append-only history is now enforced, not only documented: `memlint.py
  --against-ref REF [--staged] ROOT` compares every topic file's links now
  against what they were at `REF` and freezes a recorded link's BODY
  (`ruling`, `rationale`, `alternatives`, `evidence`, `revisit_if`, `edges`,
  `assumptions`, `invariant`, `date`, `kind`, `reverses`,
  `reason_for_change`, `recorded_by`, `recorded_at` -- any diff there is an
  error naming the field). Three lifecycle fields may move forward only,
  once: `status` from `active`/`provisional` to `superseded`/`historical`/
  `declined` (never back, never between the three terminal values),
  `superseded_by` may be added in that same move (never changed afterwards,
  never present without that status), and `promoted_by` may be added with
  no such status coupling (the promotion procedure names the new link on
  the old one, whatever the old link's own status); a lifecycle move
  bundled with any body edit is an error too, on every lifecycle field
  involved. A link removed, or a topic file deleted or renamed, is also an
  error (new links, and changes to `current`, `title`, `tags`, `code_refs`,
  or the body text, stay free). A new store git `pre-commit` hook
  (`hooks/pre-commit-append-only.sh`, wired by `scripts/repo-init.sh`
  alongside `post-commit`, both refusing to overwrite a foreign hook they
  did not render) runs this on every commit and blocks the ones that fail
  it -- fail-open on an unborn HEAD, a missing python, or an engine
  failure, `--no-verify` bypasses it locally, and the same check can run
  again in CI for a guarantee local bypasses cannot reach.
- `code_refs` now documents its three forms explicitly (a repo-relative path
  prefix, an fnmatch glob, or `path#symbol`, a qualified symbol name as the
  chunkers report it) -- retrieval matching is unchanged. A constraint or
  hold link may be mirrored at its bound symbol with a `decision:
  TOP-xxxx Ln` comment -- located through the chunker registry, so the rule
  is language-agnostic; a file whose backend cannot run here is skipped
  with a warning naming the reason, while a file whose language has no
  chunker at all is instead scanned for the marker by plain regex alone --
  silent when it carries none, and, when it does, checked against every
  rule below that needs no symbol location, with a warning (not an error)
  that the marker cannot be attributed to a symbol.
  `memlint.py --code-root DIR` now checks the pair both ways: in a file
  WITH a chunker, every marker under a code root must name a topic and
  link that exist, are `active`, and are a CONSTRAINT or HOLD (never a
  glob or bare-path match -- only an exact `path#symbol` entry counts),
  else an error naming the file and line; a no-chunker file's marker is
  held to the same topic/link/status/tier rules, but any code_refs form
  naming the file satisfies it, since there is no symbol to verify
  against; every active CONSTRAINT/HOLD link whose topic carries a `path#symbol`
  ref must find the marker at that symbol, else a warning (existing stores
  carry none yet) -- a `path#symbol` whose symbol NAME is genuinely absent
  from the file's text is an error instead (the ref itself is dangling), not
  merely a missing marker. When the name IS present but the chunker reports
  no declaration for it (a container type, or a language whose chunker
  cannot verify a declaration it can still see named), that stays a warning,
  same as an ordinary missing marker, rather than escalating to an error the
  chunker itself cannot actually support.
- `search` now defaults to `status: active` when no `--status` is given at
  all -- matching every other reader's assumption that "the decision"
  means the current one -- and drops any other status (`superseded`,
  `historical`, `declined`, `provisional`) from an unfiltered search;
  `--status any` widens back to every status, and any other explicit
  `--status` value is unchanged. A record that carries no status of its
  own at all (never topic-shaped -- no `links:` -- and no explicit
  `status:` field: a `sources/` record, the store README, a frontmatter-
  less `inbox/` drop) stays findable by this default too -- the default
  filter is `status: active` OR no status at all, never narrowed to
  exclude a record that was never a ruling to begin with.
- A record under `inbox/` (a reviewer's freeform consult drop, not yet
  promoted) now indexes as `type: inbox` unconditionally -- even one that
  carries its own conflicting frontmatter `type:` -- and `search` excludes
  it from results by default, a topic's own derived link rows included (a
  proposed topic's link, itself its own searchable row, is excluded
  through its parent's inbox classification, not just the topic row
  itself); `--include-inbox` (or an explicit `--type inbox`) widens back,
  and works at the default status (above) with no second flag needed.
  `check`/`reindex` counts, and `chain`/`for-path`/`why`, are unaffected.
  Reindexing an existing index picks up this classification automatically
  on its next run (a bumped index generation forces one full content pass,
  even for an unchanged file).
- `memlint.py` now errors on an `owner-verbatim` ruling whose text ends in
  a question mark, after trimming trailing quote/bracket characters left by
  a copy-paste -- a question is not a ruling, whoever asked it. The check
  applies only to `active`/`provisional` links: a superseded link is
  history the append-only guard already protects, so flagging it there
  could never be cleared by superseding it. `owner-ratified` (the
  orchestrator's own paraphrase, never a literal transcript) is not
  covered.
- A `kind: reversed` link whose `reverses:` target's `status` is still
  `active` or `provisional` is now a linter error, naming the target and
  its current status. `kind: amended` leaves its predecessor active on
  purpose and is not covered by this check.

### Code index
- The code index's freshness check now compares five stat signals per file
  (size, mtime, ctime, inode, device) instead of mtime/size alone, catching a
  same-size, same-mtime content rewrite the old comparison could not; when a
  root's stored git HEAD has moved, the commit's own changed files are hashed
  too, as a trigger (not proof on its own). `code-search`'s reported state
  splits `current` (this call hashed every file and proved it, only under the
  new `--verify-content` flag) from `metadata-current` (the honest default --
  nothing looks changed, but nothing was proven by a hash either); a
  nothing-found result is real evidence only under `current`. Each
  `code_roots` entry in `--json` also carries `git_delta`. The code index
  schema bumps to version 3 (a rebuild on first use, same as any schema
  bump -- roots and languages survive, embeddings do not).
- The code index's vectors now carry the same model fingerprint as the
  decision index's (`embeddings.embed_fp`, `code_project.embedding_fingerprint`
  -- reserved by the schema-3 bump above, wired here): an old-model or
  foreign-dimension vector is invisible to ranking, `code-search --json`
  gains `embedding_fingerprint` and, on a query-time mismatch,
  `"embedding": "fingerprint-mismatch"` with an FTS-only fallback;
  `code-reindex` re-embeds every chunk on a mismatch, and `reembeds` in its
  summary line now counts vectors actually written, not chunks merely sent
  to the backend.
- Each file's write in `code-reindex` now runs under its own savepoint;
  a failure that leaves the purge-and-stamp step itself unable to complete
  (rather than a normal chunker failure, already handled) rolls back that
  file's attempt instead of committing a half-updated row, prints
  `cannot purge stale rows for <path> ...; index integrity not guaranteed`,
  and the run exits 5 with an `N integrity failure(s)` token in its summary
  -- every other file is still committed. `code-search`'s heal never prints
  "index healed" over a `code-reindex` exit it did not get a clean 0 from;
  it prints `heal did not complete (code-reindex exit N)` instead and still
  answers from the current index.

### Hooks
- The five write-side hooks (edit ledger, coverage/look-back nudges,
  session-start/-end) now see every configured code root, not just the
  first -- an edit under a second or third `--code-root` is ledgered and
  classified for coverage exactly like one under the first. Each ledger row
  now records which physical root it matched (or none, for a store edit);
  "code HEAD changed" is true when any configured root's git HEAD moved.
  `unmapped --code-root` is now repeatable, picking the most specific
  (longest) matching root when roots nest. Existing installs pick this up
  on their next `memcontinuum-update.sh --apply` -- no re-install needed.
- The store's `post-commit` hook no longer runs a full (embedding) reindex
  synchronously inside `git commit` -- it now runs a bounded, content-only
  pass (`--no-embed --auto`, under the same watchdog every write-side hook
  uses) so a hung or slow embedding backend can never delay a commit; text
  is searchable the instant the hook returns. When records are left
  without a fresh vector, the hook spawns a detached, coalescing background
  worker (`memidx.py embed-worker`, safe to run twice) that backfills them;
  `check --json` and `stats --json` both gain `embedding_backlog` so the
  catch-up is visible. Existing installs pick this up automatically on
  their next commit -- no re-install needed.
- The edit ledger now sees every tool call, not only `Edit`/`Write`/
  `MultiEdit`/`NotebookEdit` -- a cheap prefilter still skips the read-only
  built-ins before the watchdog even starts, but a file changed from the
  shell (or by any tool this hook has no dedicated branch for) is now
  caught by a tree diff against the last-seen state of every configured
  code root and the store root, and appended to the same ledger; an
  unrecognized or missing tool name is logged by name and still diffed
  rather than silently skipped. `stats` reports how often each of those
  two paths fired.
- The pre-edit chain hook now makes exactly one `for-path` call per
  candidate, not two, and parses its JSON answer with one small python
  script, not three. `for-path --json` gained an opt-in
  `--with-chain-text` flag that folds the plain-text chain rendering into
  the same JSON answer; the hook's single call -- carrying both `--root`
  (the flag that triggers `for-path`'s on-disk drift check) and
  `--with-chain-text` -- now finds the matching candidate, determines the
  index state, and returns its chain text all at once, so the store is
  walked at most once per run either way. Two separate CI measurements on
  the macOS runner motivated this: 1.003s against the hook's own 1.0s
  timing bar, and later 1.003-1.022s against that same bar (runner speed
  alone swings by roughly a quarter between runs) -- this collapse
  removes a full process start's worth of margin without loosening the
  bar itself.
- Commit messages now name the decision they land under, and the prompt
  hook nudges once when they do not. The check runs on every prompt --
  independent of the coverage signal's own ledger-growth/cooldown
  candidacy -- so a commit made between coverage reminders is still
  examined, not silently skipped until the next unrelated edit happens to
  re-arm coverage. When a configured code root's HEAD moves since the
  last prompt and the new commit names no decision id, and an `unmapped`
  call finds at least one file edited under that root during the session
  with no topic, one more fact line is added and the nudge is logged, once
  per commit -- a commit that already names a decision, or whose edits are
  all covered, gets no line. On a turn that is also a coverage candidate,
  the fact line joins the same reminder; otherwise it is the whole
  reminder on its own, with no coverage content and no store-record
  question attached, and coverage's own delivery/cooldown bookkeeping is
  left untouched. State gains `last_seen_heads` (the per-prompt baseline
  this compares against) and `nudged_commits` (bounded to the last 20);
  both re-seed from the current HEADs on a session clear. The nudge never
  reads the diff or the prompt. `stats` counts it under
  `nudges.commit_nudges`, excluded from `user_prompts` (it is supplemental
  to the turn's own outcome line, not a second prompt).

### Install and update
- `scripts/repo-init.sh`'s default `--store` (no `--store` given, cwd inside
  a git repo) now lands on the WSL-native disk instead of beside the repo
  when the checkout itself is physically on a Windows-mounted drive under
  WSL -- `$HOME/dev/<repo>-MemContinuum-Store` when `$HOME/dev` is a
  directory, else bare `$HOME/<repo>-MemContinuum-Store` -- because a store
  walk over a Windows-mounted drive costs seconds, not milliseconds. The
  installer prints one line naming why; an explicit `--store` is unaffected
  and still wins outright, and `--claude-dir` still defaults from the
  checkout's own `.claude`, never from wherever the store itself lands. The
  detection (a shared helper, `mc_default_store_for` in
  `scripts/mc-registry-lib.sh`) is the one place this default is computed --
  `memcontinuum-decide.sh` and the SessionStart detector never propose a
  store path of their own.
- That WSL-disk default name keyed only on the checkout's basename, so two
  different checkouts sharing one (`client-a/app`, `client-b/app`) used to
  collapse onto the identical store and silently share it. The installer
  now checks whether the plain name already belongs to a different checkout
  or project before handing it out a second time: `decisions.tsv`'s own
  `store=` field when a row exists, else the store's own rendered
  `<!-- memcontinuum-checkout: PATH -->` marker (stamped at store-creation
  time) compared against the checkout's physical path, else, only when
  neither has anything to say, the rendered `README.md`'s project name. When
  it does, the checkout's own parent directory name disambiguates it instead
  (`<parent>-<repo>-MemContinuum-Store`), and when even that name is already
  taken, the installer refuses (exit 17) rather than guess a third name or
  adopt a foreign store. A same-checkout re-run, even under a renamed
  `--project`, still lands on the plain name unchanged -- now true even with
  no `--record-decision` row at all, via the checkout marker.

### Documentation
- The README, `docs/DESIGN.md`, and `docs/INTERNALS.md` now say plainly
  where the automatic retrieval-before-an-edit boundary sits: it only
  covers edits made with the Edit and Write tools. A file changed from the
  shell gets no lookup beforehand -- only an after-the-fact entry in the
  edit ledger, once a tree diff notices it.

### Release process
- The test suite now fails loudly, not silently, when `$MEMCONTINUUM_PYTHON`
  is unset: a guard test names the variable and how many test classes
  (tree-sitter chunkers, embeddings, dependency reconciliation, the
  real-bash write-hook suites) would otherwise skip every one of their
  tests while the run still reads as clean. Set `$MEMCONTINUUM_PYTHON` to a
  venv python with the pinned dependencies installed, or set
  `$MEMCONTINUUM_ALLOW_UNGATED=1` to run without it anyway, accepting the
  skipped coverage; continuous integration already sets the variable, so
  this never fires there.

## [0.2.0rc3] — 2026-09-04

### Languages
- The code index chunks JavaScript, TypeScript, TSX, Java, PHP, Rust, and
  Lua, alongside the existing native Swift and Python support -- one shared
  tree-sitter backend, a grammar and a query file per language, so search,
  `why`, and the linter's symbol check cover them the same way they cover
  Swift and Python. TypeScript and TSX are two separate `--lang` values
  sharing one grammar: `--lang typescript` covers `.ts`, `.tsx` files need
  `--lang tsx`, and the census proposes each on its own.
  `backend-preflight` reports each language backend by name in one of three
  states -- `ok`, `pin-mismatch` (the backend runs, but its grammar wheel or
  the tree-sitter runtime is installed at a version the pins do not name,
  and the report gives both versions), or `missing`. A file a backend cannot
  chunk is recorded not-indexed rather than dropped, and is retried
  automatically once the missing grammar is installed.
- The linter's `#symbol` check separates "not there" from "I cannot tell".
  Three things leave a file unreadable -- a grammar wheel that is not
  installed, a file over the per-file byte cap, and a file that does not
  parse, the last two including Python, where no grammar wheel is involved
  at all. Each is a warning that names the reason and the remedy for that
  reason, and the record stays valid; a hard error is reserved for a symbol
  a readable file proves absent.

## [0.2.0rc2] — 2026-09-03

### Install and update
- The installer and the decide script now record a project's store,
  claude-dir, and code-root paths in physical form (symlinks resolved),
  so a project reached through a symlinked path -- a macOS `/var` mount,
  a symlinked checkout -- gets one registry row that matches what the
  hooks themselves see, instead of a second row for each spelling.
- The updater's walk now reports `store-form-stale` when a recorded
  store path is a symlinked spelling of the same directory, and
  `--apply` rewrites only that field -- reported as
  `store-form-updated` -- in the same pass as any other re-render.
- The ledger hook's containment check and the new-file nudge now share
  one symlink-safe path helper, `hooks/mc-path-lib.sh`, so an edit
  under a symlinked store or code root is classified correctly by
  both.

### Release
- The declared minimum Python is now 3.12 everywhere a floor is named --
  `pyproject.toml`'s `requires-python`, the README requirement line, and
  the machine-setup version gate -- matching what the dependency lockfile
  has required all along: its pinned numpy release only ships wheels for
  3.12 and newer, so every environment this project actually supports
  already runs 3.12+.
- CI now runs the unit test suite across a Python version matrix (3.12
  and 3.13) on Ubuntu, alongside the existing bash 3.2 verification job,
  and adds a real macOS runner: it installs the same lockfile, runs the
  full suite, and re-runs the shell-driving suites under the actual bash
  3.2.57 macOS ships, rather than only a bash-3.2-on-Ubuntu simulation.

### Hooks
- A session that begins with `/clear` now initializes session state the
  same way a fresh session does, so the coverage and look-back nudges keep
  firing for the rest of that session instead of going silent. A file
  edited before the `/clear` that is still unmapped to a decision stays
  visible to the coverage nudge; turn counts and injection cooldowns reset
  to a fresh baseline.

## [0.2.0rc1] — unreleased

### Code index
- Root-scoped code indexing with several code roots per project, per-file
  provenance status (ok/partial/failed/not-indexed), a preflight report
  and a one-shot heal on search, dropping a root, and a linter that
  checks a reference against every configured root.

### Decision index
- A missing decision-index file is never created by a read; only an
  explicit reindex creates one, and every read names the problem instead
  of silently answering "no decisions" from an empty store.
- An embedding-capable reindex now backfills any record whose stored
  vector does not match its current content, independent of the index's
  recorded embedding coverage; an edit made without embedding keeps the
  record's previous vector rather than discarding it, so search never
  goes blind on a path mid-edit. Vector search now excludes a stale
  vector from ranking until a real reindex refreshes it.
- The trust model's authority tiers and invariant kinds are now
  executed, not partially silent: an invariant only blocks when its
  authority is confirmed by the owner or backed by validated evidence;
  every other case is named and skipped rather than either enforced
  wrongly or silently ignored, and a malformed invariant is reported as
  a linter error instead of crashing a drift check.
- A code reference now matches by path segment, so a same-prefix
  sibling file or a backup copy is never mistaken for the file a
  decision actually names.
- An empty or fragment-only code reference now matches no path at all,
  instead of matching every absolute path; the linter reports such an
  entry as an error naming the topic and the entry.
- Individual links inside a topic are now retrievable on their own —
  found by search, carrying their own status and authority, and
  reported back by the real topic path they belong to — not only as
  part of the whole topic's text.

### Hooks
- The pre-edit retrieval hook now has the same bounded-time guarantee
  the other write-side hooks already had, so a hung lookup can never
  block an edit indefinitely; on a timeout it now returns a minimal
  answer saying retrieval did not complete, instead of staying silent.

### Documentation
- Search-recipe, authority-wording, and record-shape documentation
  corrected across the README, the search skill, the store templates,
  and the schema reference to match current behavior.

### Release process
- Continuous integration now runs the test suite and the shell
  compatibility harness on every change; dependencies are pinned in a
  lockfile; this file starts tracking releases from here.

## [0.1.0]

First tracked release. An append-only markdown decision store (topics,
incidents, concepts), schema-validated before a record lands, indexed by
SQLite FTS5 with optional vector search; a pre-edit hook hands over the
governing decision automatically, and committing the store reindexes it.
A backend-neutral code index (Swift and Python natively) ties a code hit
back to its concept and decision chain, with consent-driven setup per
repository. `scripts/repo-init.sh` wires one store and this repository's
hooks per project; `memcontinuum-setup.sh` sets up the venv and machine
config once per machine.
