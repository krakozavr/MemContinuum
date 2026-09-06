# Changelog

## [0.2.0] — unreleased

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
  error naming the field). Two lifecycle fields may move forward only, once:
  `status` from `active`/`provisional` to `superseded`/`historical`/
  `declined` (never back, never between the three terminal values), and
  `superseded_by` may be added in that same move (never changed afterwards,
  never present without that status); a lifecycle move bundled with any
  body edit is an error too, on both fields. A link removed, or a topic
  file deleted or renamed, is also an error (new links, and changes to
  `current`, `title`, `tags`, `code_refs`, or the body text, stay free). A
  new store git `pre-commit` hook (`hooks/pre-commit-append-only.sh`, wired
  by `scripts/repo-init.sh` alongside `post-commit`, both refusing to
  overwrite a foreign hook they did not render) runs this on every commit
  and blocks the ones that fail it -- fail-open on an unborn HEAD, a missing
  python, or an engine failure, `--no-verify` bypasses it locally, and the
  same check can run again in CI for a guarantee local bypasses cannot
  reach.

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

### Documentation
- The README, `docs/DESIGN.md`, and `docs/INTERNALS.md` now say plainly
  where the automatic retrieval-before-an-edit boundary sits: it only
  covers edits made with the Edit and Write tools. A file changed from the
  shell gets no lookup beforehand -- only an after-the-fact entry in the
  edit ledger, once a tree diff notices it.

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
