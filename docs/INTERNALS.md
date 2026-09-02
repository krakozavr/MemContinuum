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
[storage and index](#storage-and-index) · [CLI semantics](#cli-semantics) ·
[tests](#test-conventions)

---

## Hooks and the fail-open contract

Nine hook scripts live under `hooks/`, alongside two shared libraries
(`memlib.sh`, sourced by the five write-side hooks, and `mc-watchdog.sh`).
Seven of the nine are wired into a project's
`.claude/settings.local.json` by `scripts/repo-init.sh`; `post-commit-reindex.sh`
is invoked from the store's own git `post-commit`; `memcontinuum-detect.sh` is
wired one level up, into `~/.claude/settings.json`, by `memcontinuum-setup.sh`.

| script | event | does |
|---|---|---|
| `pre-edit-chain.sh` | `PreToolUse` (Edit/Write, filtered to `--code-root`) | `for-path` lookup on the file being edited; injects matching chains as `additionalContext` |
| `newfile-nudge.sh` | `PreToolUse` (Write only, filtered to `--code-root`) | fires only when the write target does not exist yet and its extension is wired for this project; injects one reminder to search the code index first |
| `ledger-post-edit.sh` | `PostToolUse` | appends the edit to a per-session ledger, scoped to `--code-root` and the store root |
| `precompact-persist.sh` | `PreCompact` | persists session state before context is compacted away |
| `sessionstart-remind.sh` | `SessionStart` | on `startup`/`resume`, initializes session state only (captures the code/store roots' git HEAD, prunes state older than 24h); only on `source: compact` does it inject what `precompact-persist.sh` left pending |
| `userprompt-remind.sh` | `UserPromptSubmit` | never reads the prompt text; fires the coverage or look-back nudge |
| `sessionend-stamp.sh` | `SessionEnd` | stamps session end into state |
| `post-commit-reindex.sh` | store's git `post-commit` | reindexes the store after every commit to it |
| `memcontinuum-detect.sh` | `SessionStart`, user level | classifies an un-initialized repo and asks once; no python, no watchdog, no logging by default |

**Fail-open is the contract, not a fallback.** No hook may block an edit or a
commit — not on a missing python, not on a stale index, not on a lookup error,
not on its own timeout. A hook that cannot do its job logs and exits 0. The
reason is asymmetric cost: a missed reminder costs one un-recorded ruling; a
hook that blocks an edit costs the user their tool, and the first thing anyone
does with a tool that blocks edits is remove it.

**One OUTCOME line per run** to `$MEMCONTINUUM_HOME/hook.log`. Diagnostic lines
may precede it (`pre-edit-chain.sh` logs a missing-python note before its own
`outcome=` line). A watchdog kill is included in "every run": the guarded hook
cannot write its own outcome line then — it may be mid-call, or may never have
reached that code — so `mc-watchdog.sh` writes `outcome=watchdog-killed
hook=<name>` itself before exiting.

**Writable surface.** The write-side hooks may write
`$MEMCONTINUUM_HOME/sessions/<project>/` and `hook.log`, and nothing else —
never the store, never the code root. The one exception is
`userprompt-remind.sh`'s coverage check, which calls `memidx.py unmapped`; that
command self-heals a drifted decision index by running `reindex --no-embed`,
writing to the decision index's own SQLite cache.

**Session state** lives at `$MEMCONTINUUM_HOME/sessions/<project>/<id>.json`,
written by atomic rename (`os.replace`) and guarded by a real
`fcntl.flock(LOCK_EX)` (retried up to 2s) taken inside the state-update helper
in `memlib.sh` — a Python call, never a shelled-out `flock` binary, which macOS
does not ship.

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
| `undecided` | none | `partial` or `none` | asks, once |

`partial` wiring asks rather than staying silent: a half-wired repo is the
repair path, and silence there would leave it with no route back to health.
`memcontinuum-state.sh` reports decision and wiring as two separate facts
(`decision=` / `wiring=`, plus a backward-compatible `state=` line) because a
hand-edited settings file or an interrupted install can leave them disagreeing.

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

Resolution order, for hooks and for `scripts/repo-init.sh` alike:

    $MEMCONTINUUM_PYTHON → $MEMCONTINUUM_HOME/config.sh → <engine>/.venv/bin/python

`repo-init.sh` ends the chain with a hard error naming `--bootstrap-venv`; hooks
end it by failing open.

The middle step exists because a venv need not live at `<engine>/.venv`. Without
it, every hook line in every project has to carry `MEMCONTINUUM_PYTHON` by hand,
and the one that forgets fails silently behind a log line nobody reads. In
practice it matters mainly for hand-wired or legacy hook lines: `repo-init.sh`
and `memcontinuum-setup.sh` bake `MEMCONTINUUM_PYTHON` into every hook line they
render.

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

It identifies "its own" hook entries by two things at once: one of the seven
script basenames (`pre-edit-chain.sh`, `newfile-nudge.sh`, `ledger-post-edit.sh`,
`precompact-persist.sh`, `sessionstart-remind.sh`, `userprompt-remind.sh`,
`sessionend-stamp.sh`) appearing in a hook item's `command`, **and** a
`MEMCONTINUUM_PROJECT=` marker carried in that command. The marker is what lets
two projects share one `--claude-dir`: an entry naming our scripts but marked
for a different project survives the other project's re-run.

`--project NAME` must match `[A-Za-z0-9._-]+` for exactly this reason — it is
embedded unquoted as that identity marker in every hook command line the merge
step's identity check depends on. The constraint is not merely "no `/`".

An entry naming one of the seven scripts with **no** `MEMCONTINUUM_PROJECT=`
marker at all is treated as legacy pre-identity wiring and stays sweepable by
any project's re-run of a shared `--claude-dir`. Re-running `repo-init.sh` for a
project rewrites that project's entries with the marker.

Merge behaviour: drops only its own items, per item and not per group (a foreign
hook sharing a matcher group with one of ours survives), removes any group left
empty, appends freshly rendered groups, and leaves every other top-level key
(`permissions`, unrelated hooks) untouched. `settings.local.json` is backed up
to `settings.local.json.bak-memcontinuum` before every write that touches an
existing file, and the write itself is a same-directory tmp file plus
`os.replace` with the original mode preserved — never a truncate-in-place.

## repo-init guards

Each of these refuses rather than guesses, with its own exit code, because the
cost of guessing wrong is writing into somebody else's repository.

- **Explicit `--store` with no `--claude-dir` is a hard error** (exit 2). An
  explicit store may legitimately be wired from any cwd — a test harness, a
  script, an unrelated checkout — so the cwd is not a safe signal for where the
  hooks belong, and even a git cwd can be the wrong repo. `--claude-dir`
  defaults from the store's parent *only* when `--store` was also omitted, in
  which case the store defaulted beside the repo the cwd is in and that is a
  reliable signal.
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
  need. The store's own `post-commit` runs a full reindex on the first real
  commit of content.

## Decision registry keying

`$MEMCONTINUUM_HOME/decisions.tsv`, one row per repo:
`key <TAB> decision <TAB> iso-date <TAB> note`.

The key is the `origin` remote URL when there is one and the working tree's
absolute path otherwise. Remote-keyed on purpose: a path key evaporates the
moment a repo moves on disk, and a settled decision then looks unmade. On a
path-key miss after a move the repo reads as `undecided` and is asked once
more — re-ask, never assume.

A decline stops the asking, not an existing installation. Nothing in this system
ever deletes a store: a store is its own git history, not an installer artifact.

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
write-side hooks support one `MEMCONTINUUM_CODE_ROOT` each — a limitation of
`hooks/memlib.sh`, not of the installer — so with several `--code-root`s they
get the first.

## The watchdog

macOS ships neither `flock` nor `timeout`, and both hooks and installer must run
on stock macOS. The lock is solved in Python (above); the deadline is solved by
`hooks/mc-watchdog.sh`: a guarded hook re-execs itself as a child under a small
Python launcher that kills the whole child process group once a budget expires —
2 seconds by default, 1.2 seconds for `sessionend-stamp.sh`.

Guarded: the five write-side hooks, plus `newfile-nudge.sh` (which has no
write-side state of its own but shares the same guard rather than growing a
second bespoke timeout story for the one hook that happens to be fast).
Unguarded: `pre-edit-chain.sh`, `post-commit-reindex.sh`,
`memcontinuum-detect.sh`.

Ordinary coreutils (`cat`, `dirname`, `date`, `mkdir`, …) are used freely — what
is avoided is specifically the two GNU-only binaries macOS lacks.

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
language — `{backend, module, extensions, shebangs, impl_version, skip_dirs}` —
and there is exactly one row per language: exclusivity is structural, with no
API to register a second backend for an existing language. Backends are imported
lazily through `get_chunker(lang)`.

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
`backend:module:impl_version`, stored per file in `file_sha` alongside the
content sha. A file is skipped on reindex only when **both** match. A
source-sha-only skip would serve chunks from a superseded chunker forever after
a backend change — a one-way door. Bumping a row's `impl_version` is therefore
the supported way to force re-chunking of one language's files.

`ensure_file_sha_chunker_version_column` adds the column to a legacy db and
leaves it NULL; NULL never compares equal to a real version, so every
pre-existing row re-chunks once.

### Skip predicate

A language's `skip_dirs` prune only that language's own files. The walk in
`iter_code_source_files` prunes `CODE_SKIP_DIR_NAMES` (global noise: `.git`,
`vendor`, `node_modules`) plus `chunkers.common_skip_dirs(wired)` — the
**intersection** of the wired languages' skip sets, a pure optimization, since
any file under such a directory would be dropped by its own language's rule
anyway. Every other directory is walked, and a file is dropped iff one of its
root-relative ancestor directory names is in **its own** language's set
(`chunkers.path_is_skipped_for_lang`).

Consequence, and the point of the design: with Swift and Python both wired,
`Tests/foo.py` is indexed (Python's skip set has no `Tests`) while
`Tests/Foo.swift` is not. A union rule dropped both, silently losing Python
source the census had just proposed Python on the strength of.

`_census_skip_dirs` is deliberately wider — the global set unioned with *every*
table row's `skip_dirs` — because a census runs before any language is wired, so
there is no wired subset to reason about; without it, an untouched Python
project's census would count thousands of files under `.venv/` as signal.

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
root yields `{}`, not an error.

The JSON always seeds **every** table language at zero, whether or not the tree
holds one of its files, so a consumer can present three categories without
re-deriving the known-language list: `status: "supported"` with `files > 0`
(proposed), `status: "supported"` with `files == 0` (supported but not found),
and `status: "unsupported"` (the key is the extension, or `"(no extension)"`).

`repo-init.sh` runs the census across every `--code-root` given, aggregates, and
offers four answers on a tty: skip (language-less wiring), enable all detected,
select from detected, or never-mention-this-extension. The fourth records
extensions onto the nudge hook line (`MEMCONTINUUM_NEVER_EXTS`) and does not
change which languages are enabled.

`repo-init.sh` verifies each `--code-root` exists itself before running any
census, rather than inferring a missing directory from the census's own
fail-open empty dict; a census that fails, prints non-JSON, or prints JSON that
is not an object is a hard error rather than a silent `{}`.

Non-interactive use: `--langs LIST` (wins over `--non-interactive`; an unknown
name fails with the list of known languages), `--never-ext LIST`,
`--non-interactive` (language-less wiring, initial `code-reindex` skipped). With
no tty and neither flag, `repo-init.sh` exits 12 with a message naming the driven
flow rather than hanging on `/dev/tty` — an agent has no tty, so the skill runs
the census itself, presents it, and re-runs with `--langs`.

`--lang` is required on a project's first `code-reindex` (exit 1) and reused
from `code_meta` afterwards; there is no hardcoded default. The initial
code-reindex in `repo-init.sh` hard-fails the install on a non-zero exit (exit
13, distinct from the decision-store reindex's exit 7): per-file handling already
fails open, so a non-zero exit there is structural.

### Single-root indexing

`code-reindex` is single-root by construction — it deletes every stored path it
did not see under the root it was given, and overwrites `code_meta.code_root`.
Looping it over several roots therefore leaves only the last root indexed, having
quietly deleted the earlier ones' rows on the way. `repo-init.sh` indexes the
first `--code-root` only and prints which roots it skipped. Multi-root code
indexing is a later milestone.

## memlint

`memlint.py ROOT [--code-root DIR]` imports memidx's own walker, so a session
buffer is never linted as a topic, and reuses
`memidx.fragment_declared_in_text` — the same predicate `code-search` uses for
concept attachment at runtime — rather than a from-scratch regex, so a
`#symbol` fragment validates exactly the way attachment accepts it, comments and
string literals already masked out.

Topic-chain rules:

| rule | severity |
|---|---|
| `ruling.authority` is `owner-verbatim`/`owner-ratified` but `ruling.text` and/or `ruling.source` is missing | error |
| `status: superseded` with no `superseded_by` | error |
| `reverses:` set with no `reason_for_change` | error |
| frontmatter `current:` does not equal the newest link with `status: active` | error (names the correct value) |
| a topic in area `processing/*` or `deletion/*` has no `code_refs` | warning |
| a `status`/`authority`/`kind` value outside the schema enums | error |
| an edge `rel` outside the seven enumerated relations | error |

Concept-record rules (`type: concept` files):

| rule | severity |
|---|---|
| (`--code-root`) an `implemented_by`/`tested_by` path does not exist under it | error |
| (`--code-root`) a `#symbol` fragment matches nothing the chunker recognizes in that file | error |
| (`--code-root`) `implemented_by` with no `#symbol` fragment on a file over 400 lines | error |
| `governed_by` names a topic id not in the linted corpus | error (only when the corpus has at least one topic) |
| two concepts claim the same `implemented_by` `path#symbol` | error (corpus-wide; `tested_by` excluded — sharing a test file is fine) |
| a concept has no `tested_by` | warning, unconditional |
| a concept body has no "not this concept" sentence | warning |

Exit 1 on any error anywhere under `ROOT`; warnings alone exit 0. Standalone
records are checked only for enum validity on whatever `status`/`authority`
fields they carry.

**Deliberately not implemented:** "a link edited after being recorded (hash
mismatch vs git) → reject". See `docs/SCHEMA.md` §7 — that check belongs where a
canonical store's commits are made, not inside the linter.

## Storage and index

Markdown is canonical; SQLite is a disposable cache, rebuildable with `reindex`.

**Walker pruning.** `reindex`/`check`/`unmapped` — and memlint, which imports the
same walker — walk every `.md` under `--root` but prune dot-directories,
dotfiles and `node_modules` at every depth. Markdown that merely sits under a
store root is not a record, and a `.gitignore` cannot express that, because this
is a filesystem walk rather than a git one. The root itself is never pruned, so a
store that legitimately lives at `~/.memory/` still indexes in full.

**Two databases.** `<project>.sqlite` (decisions) and `<project>-code.sqlite`
(Anatomy's code index) are separate physical files by default, each with its own
schema, its own content-hash incremental rebuild, and its own embeddings.
Nothing enforces the split as a hard rule: `--db` (code) and `--decision-db`
(decision, on `code-search`) are independent flags. What is guarded is narrower
and lives on the decision side only: `open_db` records the owning `--project` in
a `db_meta` table the first time a physical file is opened, and refuses a later
open of the same file under a different project (`DbProjectMismatchError`)
rather than mixing rows. `open_code_db` carries no equivalent check.

Never place either db under a synced or cloud drive — keep the index on a local
POSIX filesystem.

**Isolation is enforced twice.** Each `--project` gets its own file by default,
which isolates trivially; every query additionally filters on a `project`
column, so isolation holds even when two projects are pointed at one `--db`.

**Embedding text is `title + "\n\n" + body[:1500]`** — no frontmatter YAML, no
ruling text. That formula was measured at 10/10 top-1 paraphrase retrieval on
real records, and `test_paraphrase_top1_at_least_9_of_10` reruns the measurement
as a permanent regression check. Ruling and rationale text stays searchable: it
goes into the FTS `ruling_text` column and into BM25 ranking; it is just not
embedded.

**RRF, not score blending**, for hybrid search (`k=60`): bm25 scores and cosine
similarities live on incomparable scales, so any weighted sum of the two is
arbitrary.

**`current` is derived from list order, not from `date:`.** Links are defined to
be stored newest-first, so "the newest active link" is "the first link with
`status: active`". The linter's `current` check and the `chain`/`for-path`
header line agree on this, which keeps a file whose dates are out of order but
whose positions are correct handled predictably. Authors should still keep dates
and positions in agreement.

**Tolerant parsing.** `parse_frontmatter()` never raises on malformed YAML: it
logs a warning to stderr and falls back to pulling simple top-level `key: value`
lines out of the frontmatter block by regex, so `title`/`name`/`type` survive and
the file stays indexed. Hand-authored records never take that path; it exists for
pre-existing markdown a project wants indexed as-is.

**Lazy imports.** `fastembed` (and, transitively, numpy) is imported only inside
`compute_embeddings`, `compute_query_embedding`, and the branches of
`cmd_search` that call them. `reindex --no-embed`, `chain`, `for-path` and
`search --mode fts` never trigger those imports — asserted by a
subprocess-isolated test (`test_for_path_does_not_import_fastembed`), because
`for-path` runs in a pre-edit hook and must not pay a numpy import.

## CLI semantics

`--help` on each subcommand is the flag reference. The semantics worth writing
down:

- **`search`** — `--mode fts` and `--mode vector` never both run; `hybrid` (the
  default) runs both and fuses ranks with RRF. Filters (`--status`, `--type`,
  `--area`, `--topic`, `--authority`) are always ANDed, but *where* they apply
  differs by mode: for plain `fts`/`vector` the full ranked list is computed and
  then filtered (which cannot change which allowed records place, since nothing
  outside the set was ever a candidate); for `hybrid` each side is filtered
  **before** fusion, so a filtered-out record can never occupy a rank position
  that shifts the fused score of a survivor.
- **`chain`** — one line per link, newest first: `kind`,
  `reverses`/`reason_for_change` when present, ruling (quoted for
  owner-verbatim/owner-ratified) and rationale, plus one indented edge line per
  typed cross-reference and a trailing `broken assumptions:` block. It is a
  deterministic adaptation of `docs/SCHEMA.md`'s illustrative chain view, not a
  byte-for-byte reproduction.
- **`for-path`** — plain SQLite lookup, no embedding imports, safe on a hot
  path. Matches the queried path against every topic's `code_refs` (the part
  before `#`) by exact match, prefix match in either direction, or `fnmatch`
  glob; concept records add the same matching against
  `implemented_by`/`tested_by`.
- **`check`** — compares current mtime/size against what was stored at the last
  `reindex`, without re-hashing or loading the embedding model. Exits 1 on any
  drift. (`reindex` uses sha256 to decide whether content changed and needs
  re-embedding; `check` uses the cheaper pair so a bare `touch` is still
  reported as drift.)
- **`unmapped PATH...`** — classifies each path as `mapped_topic`,
  `mapped_concept_only`, or `unmapped` without walking the code tree.
  Self-healing: if the markdown has drifted it reindexes once (`--no-embed`) and
  rechecks; if drift persists, `coverage_status` is `"unknown"` and nothing is
  reported as `unmapped` against an index of unknown freshness. This is what
  `userprompt-remind.sh`'s coverage signal calls.
- **`why`** — resolves a symbol or path to its concept(s), then prints those
  concepts' `governed_by` chains in full, including any `kind: declined` link
  (there is no separate "rejected alternative" field; a declined link *is* that
  record). A bare symbol (no `/`) resolves to its defining file through the same
  registry-served declared-symbol scan the chunker and memlint use. It tries the
  code index first and always falls back to scanning `--code-root` directly, so
  a missing or stale code index never regresses a resolution the direct scan can
  still make.
- **`drift`** — checks every active link's checkable `invariant:` against a code
  tree.
- **`code-search`** — same RRF fusion as `search`. Each hit optionally carries a
  `concept_id` when a concept's `implemented_by`/`tested_by` claims that exact
  symbol (preferred) or its containing file; attachment always reads the
  *decision* db, overridable with `--decision-db`. Every call resolves an
  index-provenance state first: **uninitialized** (no `code-reindex` for this
  project — refuses, exit 1, error on stderr, and `results` is `[]` in `--json`
  too, since an empty list would otherwise read as a real "nothing found"),
  **stale** (source changed, or a chunker was updated, since the last
  `code-reindex` — warning on stderr, search still runs), or **current**.
  `--json` wraps hits in `{"state", "code_root", "indexed_at", "head_sha",
  "results"}` so a caller can tell these apart without a second call. A "nothing
  found" is only evidence when `state` is `current`.

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
- **Gated tests.** `test_memidx.py`'s D1/D2 build their corpus from
  `fixtures/records/incidents/` (untracked) and fail rather than skip when it is
  empty; D5 reads `fixtures/records/queries.json` and skips cleanly when absent;
  D8's timing test skips without `$MEMCONTINUUM_TEST_SANDBOX_SYNTH`.
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
