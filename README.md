# MemContinuum

**Long-term memory for software projects: what was decided, why, and where it lives in the code.**

## The problem — and it has two sides

Projects that run for a long time under agent-driven development degrade in
two different ways.

**Forgotten decisions.** A decision gets made, the conversation that made it
scrolls out of context, and three weeks later someone (human or agent)
re-litigates the same question from scratch — sometimes landing on the same
answer, sometimes reinventing the thing that was already tried and rejected,
sometimes reintroducing the bug an incident already taught everyone about.
Code alone doesn't answer "why is this written this way?" — and chat history
isn't searchable, isn't structured, and isn't there once the session ends.
This side is well known, and several tools attack it.

**Code fragmentation.** The quieter rot: an agent working file-locally in a
large codebase can't know that the helper it needs already exists two
directories away — so it writes a new one. Solved problems get re-solved,
slightly differently each time. The same job ends up implemented in five
places with five behaviours; the design stops being one design. No decision
log fixes this, because nothing was ever *decided* — knowledge about what the
code already contains simply wasn't in front of the agent at the moment it
started typing. This side is the one most memory tools ignore.

MemContinuum's two layers map onto the two sides: **Rationale** holds the
decisions so they stop being forgotten; **Anatomy** holds what the code
already has — its concepts, owners, and boundaries — so it stops being
reinvented.

## How it works

MemContinuum keeps decisions as **append-only chains**: every ruling on one
question, newest first, dated, and tagged with *who actually said it* — the
project owner's own words, a ratified summary they confirmed, an agent's
inference, a reviewer's finding, or something derived from code and tests. A
changed mind is a new entry in the chain, never an edit to the old one, so
the history of "we tried X, it didn't work because Y, so we do Z instead"
stays intact and citable.

Retrieval is **forced when the index is healthy** — not left to an agent's
memory or discipline, and not something a person has to remember to
trigger. Before an edit touches a file, a hook looks up whatever decision
governs that file and hands it over automatically. It's fail-open by
design, though: a stale index, a missing python, or any lookup failure just
means the hook stays silent that turn, never that the edit gets blocked —
nothing here can stop you from working.

The same idea runs in the other direction, on two triggers, and neither one
reads what you typed. A *coverage* signal fires mid-session when files have
been edited under no decision topic at all, the set of edited files has
grown since the last nudge, and a short cooldown has passed. A *look-back*
signal fires when the conversation has moved on — several user turns or
tens of minutes — while the edit ledger hasn't grown at all, on the theory
that a long quiet stretch right after real edits is exactly when a ruling
that happened only in conversation is most likely to go unrecorded. Either
one nudges the session that this might be worth writing down as a new link
in a chain; if either fired right before a compaction, the nudge survives
into the fresh session on the other side.

Nothing is captured automatically. An agent has to deliberately write a
record, and a ruling cited as the project owner's own words is only
supposed to be written down after the owner has seen and confirmed the
exact text — that's a documented authoring step (`docs/SCHEMA.md` §5), not
a UI or a runtime gate the code enforces. What the engine *does* enforce is
narrower: `memlint` rejects any `owner-verbatim`/`owner-ratified` link
that's missing `ruling.text` or `ruling.source`, which catches an
incomplete record but can't verify the confirmation actually happened. A
store that silently guesses at what someone meant is worse than no store,
because it gets trusted the same as one that didn't guess.

Against fragmentation, **Anatomy** works in two layers. The authored layer
is a small set of *concept* records — "this is one system; here are the
files and symbols that implement it, the tests that guard it, the decisions
that govern it, and where its boundary runs." Ask `why <symbol>` and you get
the concept a strange piece of code belongs to and its full decision chain
— including whatever alternative was tried and declined — before a
well-meaning cleanup deletes it. Ask `for-path` (the pre-edit hook does,
automatically) and an edit to a governed file starts with its concept and
rulings in view. `drift` turns active decisions with a checkable shape
("all deletes go through the one gate") into failing checks when the code
quietly grows a bypass.

The second layer doesn't need anyone to have written a concept record at
all: `code-reindex` chunks a source tree into functions, inits, and
computed properties, and `code-search` finds them by what they do — "a
helper that writes a debug image" — instead of by a name you'd have to
already know to grep for. A hit that falls inside an authored concept's
boundary carries that concept's id, so the trail from "code that does
roughly this" to "the decision that governs it" still closes when one
exists. This is what lets the `memory-search` skill tell agents to run
`code-search` before writing a new helper or file — the case a concept
record alone can't cover, because nobody has to have described this part
of the codebase yet for the code itself to be searchable.

## Who this is for

MemContinuum is built for Claude Code workflows where one lead model acts as
the orchestrator and system engineer: it plans the work, deploys its own
subagents to write code, and — optionally — consults independent external
reviewers (for example, Codex or Grok CLIs) as gates and advisors. By
convention, the orchestrator is the memory's canonical writer — this is a
governance pattern, not an access control the engine enforces: nothing in
the code stops any other role from editing a topic file directly.
`scripts/repo-init.sh` creates `inbox/{codex,grok,audit}` directories under the store
precisely so a reviewer proposing a record and the orchestrator writing it
up stay two different, deliberate steps by habit. Subagents get the
relevant decision history handed to them automatically before they touch a
file — they don't have to go looking for it. Reviewers read whatever a
brief hands them and are meant to propose new records into their inbox
directory for the orchestrator to write up, rather than writing into the
store directly.

The engine and the store are plain CLI tools and markdown files, so nothing
here is locked to Claude Code specifically — other agent stacks can adopt the
same store. The automatic-reminder hooks, though, are written against Claude
Code's own hook events today, and would need porting to fire the same way
under a different harness.

## How this relates to Claude Code's own memory

Claude Code already ships two memory mechanisms of its own: CLAUDE.md (a
person's standing instructions, loaded every session) and auto-memory (notes
the agent writes for itself — a small index loaded every session, with topic
files read on demand). MemContinuum doesn't replace either one; it adds the
layer both are bad at — a durable record of *why*, with a clear answer to
who actually said it.

Four layers, in order of authority. **Code and tests** decide what the
software does today; nothing outranks them. **MemContinuum** records why it
got that way — only a person's own confirmed words can block work; an
agent's guess can inform a decision, never veto one. **CLAUDE.md** holds
standing rules for how to work, not knowledge or history. **Auto-memory**
holds the agent's own working notes — where things stood, preferences,
machine quirks — handy, never authoritative.

They divide the work cleanly. What's true for this session only stays in
auto-memory, loaded for free at session start; MemContinuum is deliberately
never auto-loaded. Once a fact graduates into a real ruling, it moves into
the store, and auto-memory keeps a one-line pointer to it — never a copy,
because copies drift and a drifted copy gets quoted as if it were still
true. The pre-edit hook is the bridge running the other way: it pulls a
store record into the session exactly when a file it governs gets touched.

If the same fact lives in two of these places at once, one of them is
already wrong — pick its one home.

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
view, not after the fact. Later, a natural checkpoint arrives — edits
piling up under no decision topic, or a stretch of turns with no edit at
all, or a compaction relaying either signal into the session on the other
side — and another hook reminds the session that this might be worth
writing down as a new link in the chain. Separately, at any time, you can
search by meaning rather than by file — "why don't we count hidden files in
the total?" — and get back the chain that answers it, ranked by a hybrid of
full-text and semantic search. Before writing something that feels like it
must already exist, `code-search` finds it by what it does rather than a
name you'd have to already know — a separate index built by chunking the
code itself, not the decision chains. Creating a brand-new source file gets
the same nudge automatically: a hook fires the moment you write to a path
that doesn't exist yet, reminding you to check the code index first — the
anti-reinvention trigger, aimed at the one moment a duplicate helper is
most likely to get written instead of found. And when a piece of code
looks strange, `why` walks from the code to its concept (where one has
been authored) to the rulings that shaped it — the anti-reinvention
direction.

Everything below this point is the technical reference: requirements,
installation, the storage model, the CLI, and the schema.

---

## Requirements

- macOS or Linux/WSL, `bash` 3.2+, `git`. The hooks need no `flock` or
  `timeout` binary (macOS ships neither by default): each write-side hook
  re-execs itself as a child under a small Python watchdog launcher
  (`hooks/mc-watchdog.sh`) that enforces its own wall-clock deadline (2s;
  1.2s for `SessionEnd`), and the shared session-state lock is a real
  Python `fcntl.flock(LOCK_EX)` call inside the same state-update helper —
  never a shelled-out `flock` binary. What's avoided is specifically those
  two GNU-only binaries macOS doesn't ship by default — ordinary coreutils
  (`cat`, `dirname`, `date`, `mkdir`, and the like) are used throughout the
  hooks and installer and are fine on both platforms, so there's nothing
  extra to install either way.
  Real-Mac smoke: **done** at the port commit — verified on macOS (arm64):
  124/124 hook tests passed under stock bash 3.2.57 and Python 3.9 at that
  point. The hook suites have grown since (140 tests as of this commit,
  `test_hooks.py` + `test_write_hooks.py` combined) and stay green under
  the same bash-3.2 harness (`tests/run_bash32.sh`), verified on this
  machine; they haven't been re-run on a real Mac since the port commit.
- Python 3.10+ for the engine itself (`memidx.py`/`memlint.py`, and any
  hook subprocess that actually invokes embedding code) — `fastembed`
  requires it. The hook scripts' own Python snippets stick to the
  standard library plus `PyYAML`, which is why they were verified above
  under the older Python macOS ships without needing 3.10 there too.
- A `sqlite3` build with FTS5, via Python's own `sqlite3` module —
  nothing to install separately.
- ~100 MB of disk for the embedding model, downloaded once by `fastembed`
  the first time anything actually needs to embed something — a `reindex`
  or `code-reindex` that has new/changed content to embed, or the first
  `search`/`code-search --mode vector`/`hybrid`. `--no-embed` / `--mode
  fts` never trigger the download.

If your shell exports a `PYTHONPATH` that shadows the venv's own site-packages
(e.g. from something a `.bashrc` sets globally), prefix any command below with
`PYTHONPATH=` — every hook already does this defensively on its own, so it
only matters when you're running `memidx.py`/`memlint.py` directly.

## Installing on a machine

Two layers, and mixing them up is the usual source of confusion:

| | what it sets up | how often | who runs it |
|---|---|---|---|
| `memcontinuum-setup.sh` | the venv, `~/.memcontinuum/config.sh`, and the **user-level** SessionStart detector + `memcontinuum` skill in `~/.claude` | once per machine | a person, deliberately |
| `scripts/repo-init.sh` | one store, and the seven **project-level** hooks in that repo's `.claude/settings.local.json` | once per repository | the `memcontinuum` skill, after a human says yes |

```bash
bash memcontinuum-setup.sh [--venv DIR] [--python PATH] [--claude-dir DIR]
                  [--no-model-warm] [--dry-run] [--uninstall]
```

- `--venv DIR` — where to create the venv. Default `<checkout>/.venv`, which is
  also the path every hook falls back to on its own. Uses `uv venv`/`uv pip`
  when `uv` is on `PATH`, else `python3 -m venv`/`pip`.
- `--python PATH` — use an existing python instead, with `requirements.txt`
  already installed. Version-gated at 3.10+ against the python that will
  actually run the engine, not against whatever `python3` happens to be first
  on `PATH`.
- `--claude-dir DIR` — user-level Claude Code directory. Default `~/.claude`.
- `--no-model-warm` — skip the one-time ~100 MB embedding-model download.
  It is on by default because, left lazy, that download lands inside someone's
  first `reindex` — or inside a hook — where it looks like a hang.
- `--dry-run` — print the plan, write nothing.
- `--uninstall` — remove the user-level hook, the skill, and **both**
  `config.sh` artifacts (fix-round-4 R4): the real one at `$MEMCONTINUUM_HOME`
  and, for a custom-HOME install, the pointer at the fixed default
  `~/.memcontinuum/config.sh` too — resolved the same env → pointer →
  default chain every other consumer uses, so an uninstall run with no
  `MEMCONTINUUM_HOME` in its own environment (the normal case) still finds
  and removes the real one, not just the pointer. Never touches a venv, a
  store, any per-repo wiring, or `decisions.tsv`.

`config.sh` is sourceable shell rather than JSON on purpose: every hook
(`hooks/memlib.sh`, `pre-edit-chain.sh`, `post-commit-reindex.sh`, and — as of
round 4's F6 fix — the watchdog guard every write-side hook runs before it
sources `memlib.sh`) reads it to resolve python, and must not need an
interpreter to do so. Resolution order is `$MEMCONTINUUM_PYTHON` →
`$MEMCONTINUUM_HOME/config.sh` → `<engine>/.venv/bin/python`. The middle step
exists because a venv need not live at `<engine>/.venv`; without it, every hook
line in every project must carry `MEMCONTINUUM_PYTHON` by hand, and the one
that forgets fails silently behind a log line nobody reads. In practice this
step mainly matters for hand-wired or legacy hook lines (see
`hooks/install-hooks.md`): `scripts/repo-init.sh` and `memcontinuum-setup.sh`
bake `MEMCONTINUUM_PYTHON` directly into every hook line they render, so a
freshly installer-wired repo never needs it — it exists for installs that
predate the venv it now points at, or were wired by hand without it.

**Custom `MEMCONTINUUM_HOME` (fix-round-4 R2/R3):** the `config.sh` at the
fixed default path (`~/.memcontinuum/config.sh`) may itself be a POINTER —
`memcontinuum-setup.sh` writes one there, recording only the real
`MEMCONTINUUM_HOME`, whenever setup runs with a non-default `MEMCONTINUUM_HOME`.
All four sites above follow through on it identically: source the
default/env path first, and if that just redefined `MEMCONTINUUM_HOME` to a
different directory, source the REAL `config.sh` there too. This runs
unconditionally — even when `MEMCONTINUUM_PYTHON` is already baked into the
hook line — because `MEMCONTINUUM_HOME` still has to resolve correctly for
session state and `hook.log` to land under the real home rather than the
default one; `config.sh`'s own `if [ -z "$MEMCONTINUUM_PYTHON" ]` guard keeps
env/baked precedence for python either way.

### Being asked, rather than having to remember

`hooks/memcontinuum-detect.sh` is the only hook that runs in repositories which
have never been initialized — that is the whole point of installing it at user
level. On `SessionStart` it classifies the repo and, in exactly one of five
states, emits a single `additionalContext` line asking the assistant to put the
question to a human. Decision (a human's recorded answer) and wiring (what the
repo's `.claude` settings actually reference right now) are separate facts —
see `state.sh`'s `decision=`/`wiring=` output below:

| state | decision row? | wiring | behaviour |
|---|---|---|---|
| `not-a-repo` | — | — | silent — nothing to wire |
| `opted-out` | — | — | silent — `$MEMCONTINUUM_HOME/no-ask` exists (machine-wide "never ask") |
| `decided` | yes (`wired` or `declined`) | any | silent — the recorded answer is authoritative regardless of current wiring; never ask twice |
| `wired-full-no-row` | none | `full` | silent — grandfathered: an install that predates the decisions registry reads as already-wired, never re-asked |
| `undecided` | none | `partial` or `none` | **asks, once** — a half-wired repo (`partial`) is the repair path, not a grandfathered install: silence there would leave it with no route back to health |

`memcontinuum-state.sh` reports these same two facts as `decision=`/`wiring=`
(plus a backward-compatible `state=` line — `wired`, `declined`,
`partial-wired`, `undecided`, `not-a-repo`, `no-config`) so a human "yes" on a
`partial-wired` repo is recognized as a repair (re-run `repo-init.sh` to
complete the wiring, THEN `decide.sh wired`), not confused with a fresh
install.

It is deliberately unlike every other hook here: no python, no `memlib.sh`, no
watchdog, no logging unless `$MEMCONTINUUM_DETECT_LOG` is set — it fires on
every session start on the machine, including in repos that have nothing to do
with this tool, so it must cost near-nothing and depend on nothing. Pure bash
and `git`, ~12 ms, and it fails open on any error.

**A hook reports a state; only the skill records a decision.** The detector
never installs anything and never writes to `decisions.tsv` — a hook must not
write down a consent it did not collect. The human answers in conversation, and
the `memcontinuum` skill acts:

```bash
scripts/memcontinuum-state.sh [REPO]                     # read-only: prints decision=/wiring=/state=...
scripts/memcontinuum-decide.sh declined --repo REPO      # they said no; never asked again
scripts/memcontinuum-decide.sh wired --repo REPO --store DIR --project NAME
scripts/memcontinuum-decide.sh forget --repo REPO        # back to undecided
scripts/memcontinuum-decide.sh never-ask                 # machine-wide; undo: ask-again
```

`--repo` is REQUIRED for `wired`/`declined`/`forget` (fix-round-4 F2): each
silences or unsilences a specific repo permanently, and there is no safe
`$PWD` default for that — a shell sitting in the engine checkout used to
record `wired` against the *engine's* key while the repo actually meant
stayed undecided forever. `memcontinuum-state.sh` (read-only) keeps its
`$PWD` default.

`decisions.tsv` is keyed by the `origin` remote URL when there is one and the
working tree's absolute path otherwise. Remote-keyed on purpose: a path key
evaporates the moment a repo is moved on disk, and a settled decision then
looks unmade. On a path-key miss after a move the repo reads as `undecided` and
is asked once more — re-ask, never assume.

A decline stops the *asking*, not an existing installation: if a repo was wired
and is later declined, its hooks stay until they are removed (see "Uninstall"
below). Nothing here ever deletes a store — a store is its own git history, not
an installer artifact.

## Installing into a new project

```bash
bash scripts/repo-init.sh --project NAME [--store DIR] [--code-root DIR ...] \
                 [--claude-dir DIR] [--python PATH] [--bootstrap-venv [DIR]] \
                 [--dry-run] [--force]
```

One command, run from this checkout, sets up a project's Rationale store and wires it into
Claude Code. `--project` is the only required flag.

- `--project NAME` — the project namespace passed to every `memidx.py --project`; also the
  index db's filename (`<NAME>.sqlite`). Must match `[A-Za-z0-9._-]+` (fix-round-4 F8: not just
  "no `/`" — `NAME` is embedded, unquoted, as a `MEMCONTINUUM_PROJECT=` identity marker in every
  hook command line the merge step's identity check depends on).
- `--store DIR` — the markdown store root to create (or adopt, if `DIR` already exists as its
  own git repo *and* already carries one of this tool's markers — any of a `topics/`,
  `incidents/`, or `concepts/` directory, or a `README.md` mentioning MemContinuum). Optional: the
  default applies the store-naming convention — `<repo>-MemContinuum-Store` as a sibling of the
  git repo the cwd is in, else `MemContinuum-Store` inside the cwd. The name is deliberately
  marked: a generic `memory/` collides with other memory systems' directories, and a bare
  `MemContinuum` reads as the tool itself rather than one project's store. **An existing git repo
  at `DIR` with none of those markers is refused outright** (fix-round-4 F10, exit 9) — a mistyped
  `--store` must never seed store directories and a replacement post-commit hook into someone
  else's repo; there is no `--force` carve-out for this one (`--force` only ever overrides the
  *nesting* check below).
- `--code-root DIR` — a code checkout the two PreToolUse hooks (`pre-edit-chain.sh`,
  `newfile-nudge.sh`) should watch, and the write-side hooks should scope the edit ledger to.
  Repeatable. Omit entirely for a rationale-only install with no associated code tree (neither
  PreToolUse hook is wired in that case — both are gated on there being at least one
  `--code-root`). The five write-side hooks only support **one** `MEMCONTINUUM_CODE_ROOT` each
  (that is a limitation of `hooks/memlib.sh`, not of this installer) — with multiple
  `--code-root`s the first one given is what they get. The two PreToolUse hooks don't share that
  limitation: every `--code-root` gets its own correctly-scoped `if`-filtered entry in each —
  `pre-edit-chain.sh` an `Edit(DIR/**)` / `Write(DIR/**)` pair, `newfile-nudge.sh` a
  `Write(DIR/**)` entry with its own `MEMCONTINUUM_CODE_ROOT` set to that specific `DIR`. (`DIR`
  here is always absolute, and the actually-rendered `if` value carries a second leading slash on
  top of it — `Edit(//abs/path/**)` — per Claude Code's permission-rule path syntax, where one
  leading slash anchors at the settings source rather than the filesystem root; see
  `hooks/install-hooks.md` for the fix-round note.)
- `--claude-dir DIR` — where to merge hook wiring and install the skill. Defaults to
  `<dirname of --store>/.claude` **only when `--store` was also omitted** — the store then
  defaults beside the repo the cwd is in, a reliable signal for where its hooks belong. **An
  explicit `--store` with no `--claude-dir` is a hard error** (fix-round-4 F3, final ruling): an
  explicit `--store` may be run from any cwd (a test harness, a script, an unrelated checkout) to
  wire a project's hooks from elsewhere, so the cwd is never a safe guess for `--claude-dir` —
  even a git cwd can be the wrong repo. Pass `--claude-dir DIR` explicitly whenever `--store` is
  explicit.
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
- `--force` — allow `--store` to be created at any location that falls inside another git
  repo's working tree (normally refused, so a store never gets silently absorbed into an
  unrelated repo's history). The check is by location, not by tracked content: it walks up from
  `--store` to the nearest already-existing ancestor directory and refuses if *that* is inside
  any git working tree at all — whether or not anything at the `--store` path itself is tracked,
  committed, or even exists yet. It's skipped entirely when `--store` is already its own git
  repo (the normal re-run/adopt case) — `--force` only ever matters the first time.

**Python resolution**, when neither `--python` nor `--bootstrap-venv` is given:
`$MEMCONTINUUM_PYTHON` (env) → `<this checkout>/.venv/bin/python` → a clear error naming
`--bootstrap-venv`. The hooks resolve their own python at runtime with one extra middle step —
`$MEMCONTINUUM_PYTHON` → `$MEMCONTINUUM_HOME/config.sh` (written by `memcontinuum-setup.sh`) →
`<engine>/.venv/bin/python` — except
they never hard-error — every hook fails open (logs the problem, changes nothing, never blocks
an edit or a commit) rather than blocking on a missing python.

**What it creates**, under `--store`: `topics/ incidents/ investigations/ concepts/ sources/
inbox/{codex,grok,audit}` (each with a `.gitkeep`), a store `README.md` (the six-line citation
rule + engine commands, rendered from `templates/store-README.md.tmpl`), and a `.gitignore`
(`*.sqlite`). If `--store` isn't already a git repo, `scripts/repo-init.sh` runs `git init` and one
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
verification summary plus next steps. **Adopted vs. fresh-seeded stores are classified before any
of this runs** (fix-round-4 F10): a store install is "adopted" iff `--store` was already a git
repo carrying one of this tool's markers (see `--store` above); everything else is a fresh seed.
On an adopted store, `memlint` findings are printed in the verification summary but do **not**
fail the install (exit 0) — they reflect pre-existing content the adopt, not this installer,
introduced (the motivating case: legacy duplicate ids memlint's duplicate-id check promotes to an
error). A freshly seeded store still hard-fails on any lint error (exit 8) — nothing but this
installer's own templates could have put one there, so a lint error there is an installer bug.

**Idempotency.** Re-running with the same `--project`/`--store`/`--claude-dir` is safe: the
merge step (`scripts/mc_settings_merge.py`, fix-round-4 F8 — the one settings-merge
implementation, shared with `memcontinuum-setup.sh`'s own detector-hook merge) identifies "its
own" hook entries by the seven script basenames (`pre-edit-chain.sh`, `newfile-nudge.sh`,
`ledger-post-edit.sh`, `precompact-persist.sh`, `sessionstart-remind.sh`, `userprompt-remind.sh`,
`sessionend-stamp.sh`) appearing in a hook item's `command`, **further scoped by a
`MEMCONTINUUM_PROJECT=` identity marker** carried in every one of the seven commands (fix-round-4
F1): an entry naming our scripts but marked for a *different* project survives a re-run — this is
what lets two projects share one `--claude-dir` without one's re-run unwiring the other's entries.
Drops only its own items (per item, not per group — a foreign hook sharing a matcher group with
one of ours survives), removes any group left empty, and appends freshly rendered groups. Every
other top-level key in `settings.local.json` (`permissions`, unrelated hooks, …) is left
untouched. `settings.local.json` is backed up to `settings.local.json.bak-memcontinuum` before
every write that touches an existing file (atomically: a same-directory tmp file plus
`os.replace`, original file mode preserved — never a truncate-in-place). Store tree creation, the
README/`.gitignore` render, and the skill copy are all overwrite-safe; `git init`/the initial
commit are skipped once `--store` is already a git repo.

**Migration note (fix-round-4 F1):** an entry naming one of the seven scripts with NO
`MEMCONTINUUM_PROJECT=` marker at all — every `newfile-nudge.sh` entry installed before this fix,
since the template that renders it never carried the marker — is treated as legacy/pre-identity
wiring and stays sweepable by *any* project's re-run of a shared `--claude-dir`, exactly as
before. Re-running `scripts/repo-init.sh` for a given project rewrites that project's entries with
the marker and closes the hole for it; a project that never re-runs stays exposed until it does.

**Uninstall.** Remove the hook items whose `command` mentions one of the seven script basenames
above from `settings.local.json` (or restore `settings.local.json.bak-memcontinuum`), delete
`<claude-dir>/skills/memory-search/`, delete `<store>/.git/hooks/post-commit`, and delete
`~/.memcontinuum/<project>.sqlite` and, if `code-reindex` was ever run against
this project, `~/.memcontinuum/<project>-code.sqlite` too (or wherever
`MEMCONTINUUM_HOME` points) — `scripts/repo-init.sh`'s own printed next-steps currently
name only the first of those two db files, not the code-index cache. Leave
`<store>` itself alone — it is the store's own git history, not an installer
artifact.

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
  working tree" refusal.

See `hooks/install-hooks.md` for what each generated hook line actually does at runtime, and
`templates/` for the generalised JSON/Markdown templates this command renders
(`{{PROJECT}}`, `{{STORE}}`, `{{CODE_ROOT}}`/`{{CODE_ROOT_FILTERS}}`/`{{CODE_ROOT_ENV}}`,
`{{PYTHON}}`, `{{HOOKS_DIR}}`, `{{ENGINE_DIR}}`, `{{STRIP_PREFIX}}` placeholders).

## Storage model

Markdown is canonical; SQLite is a disposable cache, rebuildable at any time
with `memidx.py reindex`. `reindex`/`check`/`unmapped` — and `memlint.py`, which imports the same
walker, so a session buffer is never linted as a topic either — walk every
`.md` under `--root`, but prune dot-directories and dotfiles (`.git`, `.claude`, a `.remember/` session
buffer, …) and `node_modules` at every depth — markdown that merely happens to
sit under a store root is not a record, and a `.gitignore` cannot express that,
since this is a filesystem walk rather than a git one. The root itself is never
pruned, so a store that legitimately lives at e.g. `~/.memory/` still indexes in
full. Search runs SQLite FTS5 (keyword) fused with cosine
similarity over whole-record embeddings (`BAAI/bge-small-en-v1.5` via
`fastembed`) using Reciprocal Rank Fusion — never score blending, since bm25
scores and cosine similarities live on incomparable scales. See
`docs/DESIGN.md` for the reasoning behind this and the engine's other central
choices (chains over notes, forced retrieval, no auto-capture).

A second, entirely separate SQLite cache — `<project>-code.sqlite`, next to
`<project>.sqlite` — holds Anatomy's code intent index (`code-reindex`/
`code-search`, described under `memidx.py` below): its own schema, its own
`file_sha`-by-content incremental rebuild, its own embeddings, kept in a
separate physical file *by default*. Nothing enforces that split as a hard
rule, though: `--db` (code) and `--decision-db` (decision, on `code-search`)
are independent flags and could be pointed at the same file on purpose.
What actually is guarded is narrower and lives on the decision-db side only
(`open_db`'s `db_meta` table): the first
time a physical db file is opened for a given `--project`, that project is
recorded as its owner in `db_meta`; opening the *same file* later under a
**different** `--project` is refused outright (`DbProjectMismatchError`)
rather than silently mixing that other project's rows in. A same-project
open of the same file is still allowed, and `code-search`'s own db
(`open_code_db`) carries no equivalent `db_meta` check at all — so pointing
`--db` and `--decision-db` at one file for the same project is possible and
would leave the two schemas coexisting in it.

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
memidx.py unmapped PATH... --root DIR [--project NAME] [--db PATH] [--code-root DIR] [--json]
memidx.py code-reindex --code-root DIR [--project NAME] [--db PATH] [--lang LANGS] [--no-embed] [--full]
memidx.py code-search QUERY [--project NAME] [--db PATH] [--mode fts|vector|hybrid]
                      [--limit N] [--json] [--decision-db PATH]
```

`why` and `drift` are extensions (schema §8) — resolve a symbol/path to the
concept(s) it belongs to, then print those concepts' `governed_by` topic
chains in full — newest first, every link, including any `kind: declined`
one (`why`; there's no separate "rejected alternative" field, a declined
link in the chain *is* that record) — or check every active link's
checkable `invariant:` against a code tree and report drift (`drift`). A
bare symbol passed to `why` (as opposed to a `path`, detected by the
presence of `/`) resolves to its defining file via the same lexer-aware
scan `code-search`'s chunker and `memlint`'s symbol-vocabulary check
already agree on — functions, `init`, subscripts, computed properties,
operators, and backtick-quoted names, including container keywords like
`actor`/`protocol`/`extension`, not the older from-scratch regex that
missed most of those. It tries the code index first (fast, when `--project`
resolves one) and always falls back to scanning `--code-root` directly if
that misses, so a missing or stale code index never regresses a resolution
the direct scan can still make.
`unmapped` and `code-reindex`/`code-search` are Anatomy's other two
extensions, described in their own subsection below. The rest of this
section describes the base commands.

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
  (the default) runs both and fuses ranks with Reciprocal Rank Fusion
  (`k=60`). Filters (`--status`, `--type`, `--area`, `--topic`,
  `--authority`) are always ANDed together, but *where* they're applied
  differs by mode: for plain `fts`/`vector`, the full ranked list is
  computed first and then filtered down to the allowed set (this can't
  change which of the allowed records place, since nothing outside that
  set was ever a real candidate); for `hybrid`, each side's ranked list is
  filtered to the allowed set *before* Reciprocal Rank Fusion runs, so a
  filtered-out record can never occupy a rank position that shifts the
  fused score of one that survives.
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
- `unmapped PATH...` classifies each given path against the current index
  without walking the code tree: `mapped_topic` (some topic's `code_refs`
  claims it), `mapped_concept_only` (no topic does, but a concept's
  `implemented_by`/`tested_by` does), or `unmapped` (neither). Self-healing:
  if the markdown under `--root` has drifted since the last `reindex`, it
  reindexes once (`--no-embed`) and rechecks; if drift still can't be
  resolved, `coverage_status` comes back `"unknown"` and nothing is ever
  reported as `unmapped` against an index of unknown freshness — a missing
  match is only trusted once the index is known-current. This is what
  `userprompt-remind.sh`'s coverage signal calls.

### Anatomy's code index: `code-reindex` / `code-search`

A completely separate SQLite database (`<project>-code.sqlite`, see
"Storage model" above) holds a chunked intent index over the code itself,
independent of any authored concept record.

- `code-reindex --code-root DIR` walks the tree (skipping `.git`, `.build`,
  `vendor`, `node_modules`, `Tests`, `Resources`) and chunks each source
  file into function/`init`/subscript/computed-property units with a
  lexer-aware brace walker (handles comments, strings including raw and
  multiline, string-interpolation closures, and `#if` branches).
  **Swift is the only language actually chunked today** — `--lang` takes a
  comma-separated filter (e.g. `swift,ts`), but only `swift` has a chunker
  behind it; naming any other language there just matches zero files,
  silently, not an error. Incremental by sha256, same as `reindex`, and it
  downloads the embedding model on the same terms `reindex` does (see
  Requirements) unless `--no-embed` is given.
- `code-search QUERY` runs fts/vector/hybrid search over those chunks —
  the same RRF fusion as `search`. Each hit optionally carries a
  `concept_id` when some concept's `implemented_by`/`tested_by` claims
  that exact symbol (preferred) or its containing file. Concept attachment
  always reads the *decision* database, never whatever `--db` means for
  this command (which selects the *code* database) — `--decision-db PATH`
  overrides which decision database gets consulted for attachment,
  independent of `--db`. Every call resolves one of three index-provenance
  states first and says which, rather than ever collapsing "no code index
  yet" into a bare empty result: **uninitialized** (`code-reindex` was
  never run for this project — refuses outright, exit 1, an error on
  stderr, and `results` comes back `[]` in `--json` too, since an empty
  list here would otherwise read as a real "nothing found"), **stale**
  (source under `--code-root` changed since the last `code-reindex` — a
  warning on stderr, search still runs), or **current**. `--json` wraps
  the hits in an envelope rather than a bare list so a caller can tell
  these apart without a separate call: `{"state", "code_root",
  "indexed_at", "head_sha", "results"}`. A "nothing found" is only real
  evidence when `state` is `current` (or `stale` with eyes open) — the
  `memory-search` skill tells agents to confirm that before reporting
  "none".

The `memory-search` skill has agents run `code-search` before writing a new
helper — see "How they work together" above.

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

`--code-root` is a schema §8.4 addition (optional; omit it and the checks
that need it are simply skipped) — it enables the concept-path existence
and symbol-vocabulary checks described below.

Walks `ROOT` for `.md` files, validates every topic-chain file against the
rules in `docs/SCHEMA.md` §7, and prints one `ERROR:`/`WARNING:` line per
finding. Topic-chain rules:

| rule | severity |
|---|---|
| a link's `ruling.authority` is `owner-verbatim`/`owner-ratified` but `ruling.text` and/or `ruling.source` is missing | error |
| a link has `status: superseded` with no `superseded_by` | error |
| a link has `reverses:` set with no `reason_for_change` | error |
| frontmatter `current:`, if present, does not equal the newest link with `status: active` | error (names the correct value) |
| a topic in area `processing/*` or `deletion/*` has no `code_refs` | warning |
| any `status` / `authority` / `kind` value is outside the five/five/five enumerated in the schema | error |
| an edge's `rel` is not one of the seven enumerated relations (schema §8.1) | error |

Concept-record rules (schema §8.4; these apply to `type: concept` files, not topic chains):

| rule | severity |
|---|---|
| (with `--code-root`) an `implemented_by`/`tested_by` path doesn't exist under it | error |
| (with `--code-root`) a `#symbol` fragment doesn't match anything the chunker itself would recognize in that file — reuses `code-reindex`'s own lexer-aware scan directly (funcs, `init`, subscripts, computed vars, backtick-quoted names included), not a separate regex | error |
| (with `--code-root`) `implemented_by` with no `#symbol` fragment on a file over 400 lines | error (an unqualified claim on a large file is too vague — narrow it to a symbol) |
| `governed_by` references a topic id not found anywhere in the linted corpus | error (only checked when the corpus has at least one topic record to validate against) |
| two concepts both claim the same `implemented_by` "path#symbol" | error (corpus-wide; `tested_by` is excluded — sharing a test file across concepts is fine) |
| a concept has no `tested_by` at all | warning — **unconditional**, fires with or without `--code-root` |
| a concept's body has no "not this concept" sentence (what it's explicitly *not*) | warning |

Exit code is 1 if any error was found anywhere under `ROOT`; warnings alone
exit 0. Standalone (non-topic, non-concept) records are only checked for
enum validity on whatever `status`/`authority` fields they happen to
carry — the other rules are about link chains or concepts and don't apply
to them.

**Deliberately not implemented:** "a link edited after being recorded (hash
mismatch vs git) → reject" — see `docs/SCHEMA.md` §7 for why this belongs at
the point where a canonical store's commits are made, not inside the linter.

## Hooks

Seven hook scripts under `hooks/`, wired into a project's `.claude/settings.local.json`
by `scripts/repo-init.sh` — plus `memcontinuum-detect.sh`, which is wired one level up, into
`~/.claude/settings.json` by `memcontinuum-setup.sh`, and is the only one that runs in
repositories this tool has never been installed into (see "Being asked, rather
than having to remember" above). All of them fail open (never block an edit, never block a commit
on a missing python or a lookup failure) and log one OUTCOME line per run
to `$MEMCONTINUUM_HOME/hook.log` (diagnostic lines may precede it, e.g.
`pre-edit-chain.sh`'s missing-python note before its own `outcome=...`
line) — including on a watchdog kill at budget expiry: the guarded hook
can't write its own outcome line then (it may be mid-call, or never got
that far), so `mc-watchdog.sh` itself writes
`outcome=watchdog-killed hook=<name>` before exiting, and a budget-expiry
kill still leaves its one outcome line. `hooks/memlib.sh` is the shared implementation the five
write-side hooks source; `hooks/mc-watchdog.sh` is a second shared file,
sourced by those same five hooks plus `newfile-nudge.sh` *before*
`memlib.sh`/its own logic, providing the wall-clock watchdog described
below. `hooks/install-hooks.md` documents the exact wiring each one gets.

| script | event | does |
|---|---|---|
| `pre-edit-chain.sh` | `PreToolUse` (Edit/Write, filtered to `--code-root`) | looks up the file being edited via `for-path`, injects the matching chain(s) as `additionalContext` |
| `newfile-nudge.sh` | `PreToolUse` (Write only, filtered to `--code-root`) | fires only when the write target does not exist yet (never on an edit to an existing file) and has an indexed source extension (`.swift` today); injects one reminder to check the code index before writing; never blocks; ~56ms p95 latency; runs under the shared watchdog |
| `ledger-post-edit.sh` | `PostToolUse` | appends the edit to a per-session ledger, scoped to `--code-root` and the store root |
| `precompact-persist.sh` | `PreCompact` | persists session state before context is compacted away |
| `sessionstart-remind.sh` | `SessionStart` | on `startup`/`resume`, only initializes session state (captures the code/store roots' current git HEAD, prunes state older than 24h) — no output; only on `source: compact` does it inject whatever `precompact-persist.sh` left pending |
| `userprompt-remind.sh` | `UserPromptSubmit` | never reads the prompt text itself — fires a *coverage* nudge when edited files carry no decision topic and the ledger has grown since the last nudge (past a cooldown), or a *look-back* nudge when several user turns or tens of minutes have passed with no ledger growth |
| `sessionend-stamp.sh` | `SessionEnd` | stamps session end into state |
| `post-commit-reindex.sh` | store's own git `post-commit` (not a Claude Code hook) | reindexes the store after every commit to it |
| `memcontinuum-detect.sh` | `SessionStart`, **user level** | in an un-initialized repo with no recorded answer, asks the assistant to put the question to a human — once. No python, no watchdog, no logging by default; silent in every other state |

Session state lives at `$MEMCONTINUUM_HOME/sessions/<project>/<id>.json`,
updated by atomic rename (`os.replace`) and guarded by a real file lock — a
Python `fcntl.flock(LOCK_EX)` call (retried up to 2s) inside the same
state-update helper in `memlib.sh`, never a shelled-out `flock` binary; the
write-side hooks' writable surface is that directory plus `hook.log` —
never the store or the code root — with one exception: `userprompt-remind.sh`'s
coverage check calls `memidx.py unmapped`, which self-heals a drifted
decision index by running `reindex --no-embed` when it detects the
markdown store has changed since the last reindex, writing to the
decision index's own SQLite cache (`$MEMCONTINUUM_HOME/<project>.sqlite`)
when that happens.

Every hook here except `pre-edit-chain.sh`, `post-commit-reindex.sh` and
`memcontinuum-detect.sh` runs under its own wall-clock watchdog — the five write-side hooks above
plus `newfile-nudge.sh` (which has no write-side state of its own but
shares the same guard, per its own header comment, rather than a second
bespoke timeout story for the one hook that happens to be fast): each
re-execs itself as a child under a small Python launcher
(`hooks/mc-watchdog.sh`) that kills the whole child process group once a
budget expires — 2 seconds by default, 1.2 seconds for `sessionend-stamp.sh`.

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

Unlike `scripts/repo-init.sh`, the tests don't fall back to `<checkout>/.venv/bin/python`
on their own — they read `$MEMCONTINUUM_PYTHON` and, for the tests that need a
real venv to drive the hooks/installer through, skip with a clear message if
it isn't set (a handful of others need extra machine-local test data of their
own — see below — and skip the same way without it).

`tests/run_bash32.sh` re-runs `test_hooks.py`/`test_write_hooks.py` under a
real bash 3.2.57 (building one into `~/.cache/bash32` on first use, or point
`MC_BASH32` at an existing bash binary to skip that) — the actual interpreter
stock macOS ships, not `bash --posix` under a newer bash, which doesn't
reject bash-4/5-only syntax the way an old interpreter does. Every hook
subprocess call in both test files goes through `$MC_BASH` (defaults to
`bash`), so this is the same test suite, just under a different shell.

Nearly all tests create their own temp directories and pass an explicit
`--db` (or set `MEMCONTINUUM_HOME`), so nothing here ever touches a real
`~/.memcontinuum/` index — the one exception is the gated real-corpus test
described below, off by default. The full suite takes about a minute once
`$MEMCONTINUUM_PYTHON` is set (most of that in the write-hooks tests, which
spawn a real subprocess per hook invocation; the vector/hybrid tests add one
warm load of the `bge-small-en-v1.5` embedding model on top of that).

`fixtures/records/incidents/` and `fixtures/records/queries.json` (both
gitignored, not part of this repo) are where a project's own real incident
notes and paraphrase-query expectations can be dropped in locally to
re-run the retrieval-quality tests against real data; every fixture
actually **tracked** in this repo is synthetic — invented dates, invented
rulings, a fictional example app — never a real project's decision
history. `test_memidx.py`'s D1 and D2 build their corpus from
`incidents/` and don't skip gracefully on a fresh clone with nothing
dropped in: with the directory empty, their assertions that a query
returns *something* fail rather than skip. D5 is the exception, not the
rule: it reads `queries.json` specifically and calls `self.skipTest(...)`
when that file is absent, so it skips cleanly rather than failing. This
split — D1/D2 fail, D5 skips — is a pre-existing property of those tests,
not something this pass changed. D8's timing test also skips cleanly: it
needs its own `$MEMCONTINUUM_TEST_SANDBOX_SYNTH` directory of synthetic
markdown for a fixed file-count assertion, and skips without that
variable set. `test_code_index.py`'s `TestGoldProbesRealCorpus` is double-gated:
`$MEMCONTINUUM_TEST_REAL_CORPUS` must be set **and** an untracked probe file
must exist (`$MEMCONTINUUM_TEST_PROBES`, default `docs/internal/gold-probes.tsv`),
or it skips. Everything project-specific — the corpus root, the probe queries,
the qualified names they expect, the pass threshold — is read from that file
rather than written into tracked content, because probe queries and expected
symbol names describe a real private codebase (the same privacy requirement
`tests/test_repo_init.py`'s `TestNoMachineIdentifyingContent` enforces, which
also refuses that codebase's name and module prefixes anywhere in tracked
files). `scripts/codanna-bench.sh` reads the same file via
`$MEMCONTINUUM_BENCH_PROBES` and takes its corpus root from argv, then
`$MEMCONTINUUM_BENCH_CORPUS`, then the file's own `#corpus:` line. Unlike
D5/D8, when the test *does* run it deliberately does not use a temp dir: it
reindexes the real corpus into a cache at
`~/.cache/codanna-bench/bench-code.sqlite`, reused (incrementally) across
runs rather than rebuilt from scratch each time, because a full embed of
a real corpus takes on the order of 20+ minutes on a typical dev machine.
See `fixtures/payloads/README.md` for the same guarantee about the hook
payload fixtures specifically. `scripts/codanna-bench.sh` is a further,
separate exception to "everything test-related stays inside a temp dir"
worth knowing about, even though it's a manual benchmark script (comparing
`code-search` against the Codanna tool), not part of the test suite: it
caches a downloaded/built Codanna binary under the same `~/.cache/codanna-bench`
directory across runs, rather than a temp directory.

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
