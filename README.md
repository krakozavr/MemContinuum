# MemContinuum

**Long-term memory for software projects: what was decided, why, and where it lives in the code.**

## The problem

Projects that run for a long time under agent-driven development forget
things. A decision gets made, the conversation that made it scrolls out of
context, and three weeks later someone (human or agent) re-litigates the same
question from scratch — sometimes landing on the same answer, sometimes
reinventing the thing that was already tried and rejected, sometimes
reintroducing the bug an incident already taught everyone about. Code alone
doesn't answer "why is this written this way?" or "didn't we already try the
obvious alternative?" — and chat history isn't searchable, isn't structured,
and isn't there once the session ends.

## How it solves it

MemContinuum keeps decisions as **append-only chains**: every ruling on one
question, newest first, dated, and tagged with *who actually said it* — the
project owner's own words, a ratified summary they confirmed, an agent's
inference, a reviewer's finding, or something derived from code and tests. A
changed mind is a new entry in the chain, never an edit to the old one, so
the history of "we tried X, it didn't work because Y, so we do Z instead"
stays intact and citable.

Retrieval is **forced at the moment it matters**, not left to an agent's
memory or discipline. Before an edit touches a file, a hook looks up whatever
decision governs that file and hands it over automatically — the agent
doesn't have to remember to ask. The same idea runs in the other direction:
at natural checkpoints (a session starting, a compaction, a prompt that reads
like a ruling), a hook nudges the session that something worth recording just
happened.

Nothing is captured automatically. An agent has to deliberately write a
record, and anything cited as the project owner's own words passes through
an explicit step where the owner sees the exact text before it counts as a
constraint. A store that silently guesses at what someone meant is worse than
no store, because it gets trusted the same as one that didn't guess.

## Who this is for

MemContinuum is built for Claude Code workflows where one lead model acts as
the orchestrator and system engineer: it plans the work, deploys its own
subagents to write code, and — optionally — consults independent external
reviewers (for example, Codex or Grok CLIs) as gates and advisors. The
orchestrator is the memory's only canonical writer. Subagents get the
relevant decision history handed to them automatically before they touch a
file — they don't have to go looking for it. Reviewers read whatever a brief
hands them and may propose new records into an inbox for the orchestrator to
write up; they never write into the store directly.

The engine and the store are plain CLI tools and markdown files, so nothing
here is locked to Claude Code specifically — other agent stacks can adopt the
same store. The automatic-reminder hooks, though, are written against Claude
Code's own hook events today, and would need porting to fire the same way
under a different harness.

## The parts

- **Rationale** — the decision graph: topics, each an append-only chain of
  rulings with per-field authority, typed relationships to other rulings, the
  assumptions a ruling rests on, and invariants that can be checked against
  the code.
- **Anatomy** — the code graph: concepts, the symbols that implement them,
  the tests that guard them, and the decisions that govern them.
- **The engine** (`memidx.py`, `memlint.py`) — indexes the markdown into a
  searchable database and validates that every record follows the schema.
- **The hooks** — ask the engine a question at the moment it matters (before
  an edit) and inject the answer; remind a session, at natural checkpoints,
  that something might be worth recording.
- **The skill** (`memory-search`) — for a deliberate, on-demand search rather
  than the automatic per-edit lookup.

## How they work together

A short walk-through. You (or an agent) are about to edit a file that a past
decision governs. A hook fires first, looks up that file, and injects the
relevant chain as context — so the edit happens with the history already in
view, not after the fact. Later, a natural checkpoint arrives — a compaction,
a session start, a prompt that sounds like a ruling — and another hook
reminds the session that this might be worth writing down as a new link in
the chain. Separately, at any time, you can search by meaning rather than by
file — "why don't we count hidden files in the total?" — and get back the
chain that answers it, ranked by a hybrid of full-text and semantic search.

Everything below this point is the technical reference: requirements,
installation, the storage model, the CLI, and the schema.

---

## Requirements

- Linux or WSL, `bash`, `git`.
- Python 3.10+.
- `flock` and a `sqlite3` new enough for FTS5 (≥3.40, via Python's own
  `sqlite3` module — nothing to install separately).
- ~100 MB of disk for the embedding model, downloaded once by `fastembed` the
  first time a vector search actually runs. No network is needed after that;
  `--no-embed` / `--mode fts` never trigger the download at all.

If your shell exports a `PYTHONPATH` that shadows the venv's own site-packages
(e.g. from something a `.bashrc` sets globally), prefix any command below with
`PYTHONPATH=` — every hook already does this defensively on its own, so it
only matters when you're running `memidx.py`/`memlint.py` directly.

## Installing into a new project

```bash
bash install.sh --project NAME --store DIR [--code-root DIR ...] \
                 [--claude-dir DIR] [--python PATH] [--bootstrap-venv [DIR]] \
                 [--dry-run] [--force]
```

One command, run from this checkout, sets up a project's Rationale store and wires it into
Claude Code. `--project` and `--store` are the only required flags.

- `--project NAME` — the project namespace passed to every `memidx.py --project`; also the
  index db's filename (`<NAME>.sqlite`). Must not contain `/`.
- `--store DIR` — the markdown store root to create (or adopt, if `DIR` already exists as its
  own git repo).
- `--code-root DIR` — a code checkout the PreToolUse retrieval hook should watch for Edit/Write,
  and the write-side hooks should scope the edit ledger to. Repeatable. Omit entirely for a
  rationale-only install with no associated code tree (no PreToolUse hook is wired in that
  case). The five write-side hooks only support **one** `MEMCONTINUUM_CODE_ROOT` each (that is
  a limitation of `hooks/memlib.sh`, not of this installer) — with multiple `--code-root`s the
  first one given is what they get; every `--code-root` still gets its own PreToolUse
  `if`-filtered pair (`Edit(DIR/**)` / `Write(DIR/**)`).
- `--claude-dir DIR` — where to merge hook wiring and install the skill. Defaults to
  `<dirname of --store>/.claude`.
- `--python PATH` — absolute path to the python to run the engine with. Overrides every other
  resolution below.
- `--bootstrap-venv [DIR]` — create a venv (prefer `uv venv` + `uv pip` when `uv` is on `PATH`,
  else `python3 -m venv` + `pip`), install `requirements.txt` into it, and use it as the python
  for the rest of this install (unless `--python` was also given). `DIR` defaults to
  `<this checkout>/.venv`. Runs immediately, even under `--dry-run`, since later steps need a
  real python to resolve paths with.
- `--dry-run` — print the full plan (every path, every hook command line, the exact reindex/lint
  commands) and write nothing at all: no directories, no git init, no settings file, no backup,
  no skill copy, no index db (`--bootstrap-venv`'s venv is the one exception — see above).
- `--force` — allow `--store` to sit inside another git repo's already-tracked working tree
  (normally refused, so a store never gets silently absorbed into an unrelated repo's history).

**Python resolution**, when neither `--python` nor `--bootstrap-venv` is given:
`$MEMCONTINUUM_PYTHON` (env) → `<this checkout>/.venv/bin/python` → a clear error naming
`--bootstrap-venv`. The five write-side hooks and the PreToolUse retrieval hook resolve their own
python the same way at runtime (`$MEMCONTINUUM_PYTHON` → `<engine>/.venv/bin/python`), except
they never hard-error — every hook fails open (logs the problem, changes nothing, never blocks
an edit or a commit) rather than blocking on a missing python.

**What it creates**, under `--store`: `topics/ incidents/ investigations/ concepts/ sources/
inbox/{codex,grok,audit}` (each with a `.gitkeep`), a store `README.md` (the six-line citation
rule + engine commands, rendered from `templates/store-README.md.tmpl`), and a `.gitignore`
(`*.sqlite`). If `--store` isn't already a git repo, `install.sh` runs `git init` and one
initial commit (author from git config, falling back to `memcontinuum-install
<install@memcontinuum.invalid>` when none is set) — then writes `.git/hooks/post-commit` as a
small wrapper that exports `MEMCONTINUUM_ROOT`/`MEMCONTINUUM_PROJECT`/`MEMCONTINUUM_PYTHON` and
`exec`s `hooks/post-commit-reindex.sh` by its absolute path (not a bare symlink — see
`hooks/install-hooks.md` §2: a symlinked git hook carries no environment of its own, and
`post-commit-reindex.sh` silently no-ops without `MEMCONTINUUM_ROOT` set; the wrapper still
picks up future edits to the canonical script automatically, since it `exec`s the file rather
than copying it). Under `--claude-dir`: `skills/memory-search/SKILL.md` (copied verbatim) and
the hook wiring, merged into `settings.local.json`. Finally it runs `memidx.py reindex --root
DIR --project NAME --no-embed` (see "Design choices" below) and `memlint.py DIR`, and prints a
verification summary plus next steps.

**Idempotency.** Re-running with the same `--project`/`--store`/`--claude-dir` is safe: the
merge step identifies "its own" hook entries by the six script basenames
(`pre-edit-chain.sh`, `ledger-post-edit.sh`, `precompact-persist.sh`, `sessionstart-remind.sh`,
`userprompt-remind.sh`, `sessionend-stamp.sh`) appearing in a hook item's `command`, drops only
those items (per item, not per group — a foreign hook sharing a matcher group with one of ours
survives), removes any group left empty, and appends freshly rendered groups. Every other
top-level key in `settings.local.json` (`permissions`, unrelated hooks, …) is left untouched.
`settings.local.json` is backed up to `settings.local.json.bak-memcontinuum` before every write
that touches an existing file. Store tree creation, the README/`.gitignore` render, and the
skill copy are all overwrite-safe; `git init`/the initial commit are skipped once `--store` is
already a git repo.

**Uninstall.** Remove the hook items whose `command` mentions one of the six script basenames
above from `settings.local.json` (or restore `settings.local.json.bak-memcontinuum`), delete
`<claude-dir>/skills/memory-search/`, delete `<store>/.git/hooks/post-commit`, and delete
`~/.memcontinuum/<project>.sqlite` (or wherever `MEMCONTINUUM_HOME` points). Leave `<store>`
itself alone — it is the store's own git history, not an installer artifact.

**Deliberate deviations from a literal reading of the brief** (flagged here per the build
task's "report ambiguities explicitly"):
- The install-time `reindex` passes `--no-embed`. The freshly seeded store holds only
  `README.md`/`.gitkeep` stubs — nothing worth embedding yet — and running the real embedder
  here would make a first install depend on network access (or a pre-warmed fastembed cache) it
  otherwise wouldn't need. The store's post-commit hook runs a full (embedding) reindex
  automatically on the first real commit of content; run `memidx.py reindex --root DIR
  --project NAME` (no `--no-embed`) by hand any time to force one sooner.
- `.git/hooks/post-commit` is a small generated wrapper, not a literal symlink — see "What it
  creates" above for why a bare symlink can't work here.
- `--force` is documented above but wasn't in an earlier one-line usage signature this project
  worked from; it's the necessary escape hatch for the "store dir inside another git repo's
  tracked tree" refusal.

See `hooks/install-hooks.md` for what each generated hook line actually does at runtime, and
`templates/` for the generalised JSON/Markdown templates this command renders
(`{{PROJECT}}`, `{{STORE}}`, `{{CODE_ROOT}}`/`{{CODE_ROOT_FILTERS}}`/`{{CODE_ROOT_ENV}}`,
`{{PYTHON}}`, `{{HOOKS_DIR}}`, `{{ENGINE_DIR}}`, `{{STRIP_PREFIX}}` placeholders).

## Storage model

Markdown is canonical; SQLite is a disposable cache, rebuildable at any time
with `memidx.py reindex`. Search runs SQLite FTS5 (keyword) fused with cosine
similarity over whole-record embeddings (`BAAI/bge-small-en-v1.5` via
`fastembed`) using Reciprocal Rank Fusion — never score blending, since bm25
scores and cosine similarities live on incomparable scales. See
`docs/DESIGN.md` for the reasoning behind this and the engine's other central
choices (chains over notes, forced retrieval, no auto-capture).

## Record shapes

Two kinds of markdown record, distinguished by frontmatter. The full schema —
every field, every enum, the linter rules, and the typed-edges/assumptions/
invariants/concepts extensions — is `docs/SCHEMA.md`. Summary:

**Topic** — an append-only chain of rulings on one question. Has a `links:` list
(or `type: topic`). Each element of `links:` is one *link* (one ruling), newest
first:

```yaml
type: topic
id: TOP-0042
title: Hidden files in the processed count
area: processing/status
current: L4                 # the newest link with status: active
code_refs:
  - src/core/scan/scan_plan.py#hidden_count
links:
  - link: L4
    date: 2024-04-15
    status: active           # active | provisional | superseded | historical | declined
    kind: restored            # adopted | declined | reversed | amended | restored
    reverses: L3              # requires reason_for_change when set
    reason_for_change: new-evidence
    ruling:
      text: "..."
      authority: owner-verbatim   # owner-verbatim | owner-ratified | agent-inference | reviewer-finding | code-derived
      source: "..."              # required when authority is owner-verbatim/owner-ratified
    rationale:
      text: "..."
      authority: agent-inference
    superseded_by: null          # required when status: superseded
    revisit_if: ["..."]
    recorded_by: agent
    recorded_at: 2024-04-15
  - link: L3
    ...
```

A topic's derived `status`/`authority` (used by `search` filters) come from its
*current* link — the newest link with `status: active` (falling back to the
newest link if none is active).

**Standalone record** (incident, investigation, ...) — no `links:`, a single set
of frontmatter fields including, optionally, a record-level `status:` and
`authority:`.

Any file may also have loose/partial frontmatter (e.g. real-world notes that
predate this schema) — `memidx.py` indexes it best-effort (see "Tolerant
parsing" below); `memlint.py` only enforces the topic-chain rules on files
that actually have a `links:` chain.

## `memidx.py`

```
memidx.py reindex --root DIR [--project NAME] [--db PATH] [--full] [--no-embed]
memidx.py search QUERY [--project NAME] [--db PATH] [--mode fts|vector|hybrid]
                 [--status S ...] [--type T ...] [--area A] [--topic X]
                 [--authority AUTH] [--limit N] [--json]
memidx.py chain TOPIC_ID_OR_SLUG [--project NAME] [--db PATH] [--json]
memidx.py for-path FILE_PATH [--project NAME] [--db PATH] [--json]
memidx.py check --root DIR [--project NAME] [--db PATH] [--json]
memidx.py why SYMBOL_OR_PATH [--project NAME] [--db PATH] [--code-root DIR] [--json]
memidx.py drift --code-root DIR [--project NAME] [--db PATH] [--json]
```

`why` and `drift` are extensions (schema §8) — resolve a symbol/path to the
concept(s) it belongs to, then that concept's `governed_by` topic chains
(`why`), or check every active link's checkable `invariant:` against a code
tree and report drift (`drift`). The rest of this section describes the base
commands.

- `--project` defaults to `default`.
- `--db` overrides the index database path. Without it, the database lives at
  `$MEMCONTINUUM_HOME/<project>.sqlite`, and `MEMCONTINUUM_HOME` itself defaults to
  `~/.memcontinuum`. **Never point either at a location under a synced/cloud drive** —
  keep the index on a local, POSIX filesystem.
- `reindex` is incremental by sha256 (unchanged files are skipped) unless
  `--full` is given. Files removed from `--root` since the last reindex are
  removed from the index. `--no-embed` skips embedding entirely (fast, FTS-only
  — used for timing tests and for corpora too big to embed on every run).
- `search --mode fts` and `--mode vector` never both run; `--mode hybrid`
  (the default) runs both and fuses ranks with Reciprocal Rank Fusion (`k=60`).
  Filters (`--status`, `--type`, `--area`, `--topic`, `--authority`) are always
  ANDed together and applied before ranking.
- `chain` prints the compressed chain view: one line per link, newest first,
  each showing its `kind`, its `reverses`/`reason_for_change` when it has one,
  its ruling (quoted when the authority is owner-verbatim/owner-ratified) and
  its rationale, plus (schema §8.1/8.2) one indented edge line per typed
  cross-reference and a trailing `broken assumptions:` block. This is a
  *deterministic adaptation* of `docs/SCHEMA.md`'s illustrative chain-view
  example, not a byte-for-byte reproduction of it — see "Design choices" below.
- `for-path` never imports `fastembed` (or, transitively, numpy) — it is a
  plain SQLite lookup and is safe to call from a hot path such as a
  pre-edit hook. It matches a queried file path against every topic's
  `code_refs` entries (the part before `#`) by exact match, prefix match in
  either direction, or glob (`fnmatch`); schema §8.4 concept records add the
  same matching against `implemented_by`/`tested_by`.
- `check` compares the current mtime/size of every file under `--root` against
  what was stored at the last `reindex`, **without** re-hashing or touching the
  embedding model. It reports added/changed/removed files and exits 1 if any
  drift exists, 0 if the index is current. (`reindex` uses sha256 to decide
  whether content actually changed and needs re-embedding; `check` uses the
  cheaper mtime/size pair so a `touch` alone — no content change — is still
  correctly reported as drift.)

### Tolerant parsing

Real notes are messy. `parse_frontmatter()` never raises on malformed YAML: on
a parse error it logs a warning to stderr and falls back to pulling simple
top-level `key: value` lines out of the frontmatter block by regex, so at
least `title`/`name`/`type` survive and the file still gets indexed and stays
searchable. Hand-authored records (the topic chains this tool exists for)
never hit that path — it exists for pre-existing markdown a project may want
indexed as-is.

### Lazy imports

`fastembed` (and `numpy`, pulled in only inside vector-search code) is
imported **only** inside `compute_embeddings`, `compute_query_embedding`, and
the branches of `cmd_search` that call them. `reindex --no-embed`, `chain`,
`for-path`, and `search --mode fts` never trigger those imports — verified by
a subprocess-isolated test (`test_for_path_does_not_import_fastembed`).

## `memlint.py`

```
memlint.py ROOT [--code-root DIR]
```

`--code-root` is a schema §8.4 addition (optional; omit it and `memlint.py
ROOT` behaves exactly as before) — it enables the concept-path existence
checks described there.

Walks `ROOT` for `.md` files, validates every topic-chain file against the
rules in `docs/SCHEMA.md` §7, and prints one `ERROR:`/`WARNING:` line per
finding:

| rule | severity |
|---|---|
| a link's `ruling.authority` is `owner-verbatim`/`owner-ratified` but `ruling.text` and/or `ruling.source` is missing | error |
| a link has `status: superseded` with no `superseded_by` | error |
| a link has `reverses:` set with no `reason_for_change` | error |
| frontmatter `current:`, if present, does not equal the newest link with `status: active` | error (names the correct value) |
| a topic in area `processing/*` or `deletion/*` has no `code_refs` | warning |
| any `status` / `authority` / `kind` value is outside the five/five/five enumerated in the schema | error |
| an edge's `rel` is not one of the seven enumerated relations (schema §8.1) | error |
| (with `--code-root`) a concept's `implemented_by`/`tested_by` path doesn't exist under it | error |
| (with `--code-root`) a concept has no `tested_by` at all | warning |

Exit code is 1 if any error was found anywhere under `ROOT`; warnings alone
exit 0. Standalone (non-topic) records are only checked for enum validity on
whatever `status`/`authority` fields they happen to carry — the other rules
are about link chains and don't apply to them.

**Deliberately not implemented:** "a link edited after being recorded (hash
mismatch vs git) → reject" — see `docs/SCHEMA.md` §7 for why this belongs at
the point where a canonical store's commits are made, not inside the linter.

## Hooks

Six hook scripts under `hooks/`, wired into a project's `.claude/settings.local.json`
by `install.sh`. All of them fail open (never block an edit, never block a commit
on a missing python or a lookup failure) and log one line per run to
`$MEMCONTINUUM_HOME/hook.log`. `hooks/memlib.sh` is the shared implementation
the five write-side hooks source; `hooks/install-hooks.md` documents the exact
wiring each one gets.

| script | event | does |
|---|---|---|
| `pre-edit-chain.sh` | `PreToolUse` (Edit/Write, filtered to `--code-root`) | looks up the file being edited via `for-path`, injects the matching chain(s) as `additionalContext` |
| `ledger-post-edit.sh` | `PostToolUse` | appends the edit to a per-session ledger, scoped to `--code-root` |
| `precompact-persist.sh` | `PreCompact` | persists session state before context is compacted away |
| `sessionstart-remind.sh` | `SessionStart` | reminds a fresh session that a store exists and how to query it |
| `userprompt-remind.sh` | `UserPromptSubmit` | on a prompt that looks like a ruling, reminds the session to record it |
| `sessionend-stamp.sh` | `SessionEnd` | stamps session end into state |
| `post-commit-reindex.sh` | store's own git `post-commit` (not a Claude Code hook) | reindexes the store after every commit to it |

Session state lives at `$MEMCONTINUUM_HOME/sessions/<project>/<id>.json`,
lock-guarded (`flock`, 2s timeout) and updated by atomic rename; the write-side
hooks' only writable surface is that directory plus `hook.log` — never the
store or the code root.

## Design choices worth knowing

- **`current` is derived from list order, not from `date:`.** A topic's links
  are defined to be stored newest-first; "the newest active link" is simply
  "the first link in the list with `status: active`". This matches how the
  linter's `current` check and the `chain`/`for-path` header line both work,
  and it means a file whose links are out of date order (but whose *positions*
  are still newest-first) is still handled consistently and predictably —
  though authors should keep dates and positions in agreement.
- **Isolation is enforced twice.** By default each `--project` gets its own
  database file (`$MEMCONTINUUM_HOME/<project>.sqlite`), which isolates trivially.
  Every query additionally filters by a `project` column, so isolation holds
  even if two projects are pointed at the *same* `--db` file (exercised by
  `TestD2ProjectIsolation`, which does exactly that).
- **Embedding text is `title + "\n\n" + body[:1500]`**, nothing else — no
  frontmatter YAML, no ruling text. This matches the formula that was
  independently measured at 10/10 top-1 paraphrase retrieval on the same real
  records this repo's `test_paraphrase_top1_at_least_9_of_10` test reruns as a
  permanent regression check. Ruling/rationale text is still searchable — it
  goes into the FTS `ruling_text` column and into the `search` BM25 ranking —
  it's just not embedded.
- **RRF, not score blending, for hybrid search.** See "Storage model" above.

## Running the tests

```bash
cd memcontinuum
export MEMCONTINUUM_PYTHON="$PWD/.venv/bin/python"   # or wherever --bootstrap-venv put it
$MEMCONTINUUM_PYTHON -m unittest discover -s tests -v
```

Unlike `install.sh`, the tests don't fall back to `<checkout>/.venv/bin/python`
on their own — they read `$MEMCONTINUUM_PYTHON` and, for the tests that need a
real venv to drive the hooks/installer through, skip with a clear message if
it isn't set (a handful of others need extra machine-local test data of their
own — see below — and skip the same way without it).

All tests create their own temp directories and pass an explicit `--db`
(or set `MEMCONTINUUM_HOME`), so nothing here ever touches a real
`~/.memcontinuum/` index. The full suite takes about a minute once
`$MEMCONTINUUM_PYTHON` is set (most of that in the write-hooks tests, which
spawn a real subprocess per hook invocation; the vector/hybrid tests add one
warm load of the `bge-small-en-v1.5` embedding model on top of that).

`fixtures/records/incidents/` (gitignored, not part of this repo) is where a
project's own real incident notes can be dropped in locally to re-run the
paraphrase-retrieval regression test against real data; every fixture
actually **tracked** in this repo is synthetic — invented dates, invented
rulings, a fictional example app — never a real project's decision history.
The tests that read that directory (`test_memidx.py`'s D1/D2/D5) don't skip
gracefully on a fresh clone with nothing dropped in; they fail (weak or empty
results) on the missing data instead. This is a pre-existing property of
those tests, not something this pass changed. D8's timing test is the
exception: it additionally needs its own `$MEMCONTINUUM_TEST_SANDBOX_SYNTH`
directory of synthetic markdown for a fixed file-count assertion, and does
skip cleanly without that variable set.
See `fixtures/payloads/README.md` for the same guarantee about the hook
payload fixtures specifically.

## Acknowledgements & prior art

- [fastembed](https://github.com/qdrant/fastembed) — the embedding runtime this engine calls for vector search.
- [BAAI/bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5) — the embedding model `fastembed` loads by default here.
- [SQLite FTS5](https://www.sqlite.org/fts5.html) — the full-text index half of hybrid search.
- [Claude Code hooks](https://code.claude.com/docs/en/hooks) — the mechanism the retrieval and reminder hooks are built on.
- [Basic Memory](https://github.com/basicmachines-co/basic-memory) — evaluated as a substrate for this project before building a dedicated
  engine. It was set aside for this specific use case: per-invocation latency
  matters in a pre-edit hook that has to return well under a second, its
  defaults lean toward auto-capture where this project wanted every record
  deliberately authored, and this project needed chain-shaped retrieval
  (a ranked sequence of rulings on one question, not just similar notes).
  Its local-markdown-first philosophy — files as the real store, a database
  as a derived index — directly influenced the storage model above.

## License

MIT — see `LICENSE`.
