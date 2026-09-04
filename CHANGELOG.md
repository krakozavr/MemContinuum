# Changelog

## [0.2.0rc3] — unreleased

### Languages
- The code index now chunks JavaScript, TypeScript (TSX included), Java,
  PHP, Rust, and Lua, alongside the existing native Swift and Python
  support -- one shared tree-sitter backend, a grammar and a query file per
  language, so search, `why`, and the linter's symbol check cover these six
  languages exactly as they already did Swift and Python. `backend-preflight`
  reports which language backends import in this python, by name; a file a
  backend cannot chunk is recorded not-indexed rather than dropped, and is
  retried automatically once the missing grammar is installed.

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
