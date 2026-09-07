# MemContinuum

**Long-term memory for Claude Code software projects: what was decided, why,
and where it lives in the code.**

## The Two-Body problem

Projects that run for a long time under agent-driven development degrade in two
different ways.

**Forgotten decisions.** Two bodies forget here, the person and the model,
and both do it all the time. A decision gets made, the conversation that made it
scrolls out of context, and three weeks later someone — human or agent —
re-litigates the same question from scratch. Sometimes they land on the same
answer; sometimes they reinvent the thing that was already tried and rejected;
sometimes they reintroduce the bug an incident already taught everyone about.
Code alone does not answer "why is this written this way?", and chat history is
not searchable, not structured, and not there once the session ends.

**Code fragmentation.** The quieter rot: an agent working file-locally in a
large codebase cannot know that the helper it needs already exists two
directories away — so it writes a new one. Solved problems get re-solved,
slightly differently each time, until the same job is implemented in five places
with five behaviours and the design stops being one design. No decision log
fixes this, because nothing was ever *decided*: knowledge about what the code
already contains simply was not in front of the agent at the moment it started
typing.

MemContinuum's two layers map onto the two sides. **Rationale** holds the
decisions so they stop being forgotten. **Anatomy** holds what the code already
has — its concepts, owners, and boundaries — so it stops being reinvented.

## How it works

Decisions are kept as **append-only chains**: every ruling on one question,
newest first, dated, and tagged with *who actually said it* — the project
owner's own words, a summary they confirmed, an agent's inference, a reviewer's
finding, or something derived from code and tests. A changed mind is a new entry
in the chain, never an edit to the old one, so "we tried X, it did not work
because Y, so we do Z instead" stays intact and citable. The linter enforces this
invariant on every commit: the store's own git hook refuses to commit an edit to
a recorded link's body (its three lifecycle fields — status, the link it was
superseded by, the link that promoted it — may each move forward once, since
closing a link or promoting it is bookkeeping, not a change of mind), and the
same check can run again in CI for a
guarantee `git commit --no-verify` cannot bypass locally.

Retrieval is **automatic** for edits made with the Edit and Write tools, not
left to anyone's discipline. Before one of those touches a file, a hook looks
up whatever decision governs that file and hands it over. Every hook here
fails open: a missing index, a missing python, a failed lookup, or the hook
running too long means the hook stays silent (or, on a timeout specifically,
says outright that retrieval timed out rather than staying silent) — never
that your edit is blocked. A file changed from the shell instead — a script,
`sed`, `git apply`, anything run as a Bash command — gets no lookup before the
change and no requirement to declare what it touched: MemContinuum's best
effort is to notice it afterwards, from a tree diff, and record it in the
edit ledger, never to block the change over it — though one committed away
in the same breath, or made under a path git ignores, is not seen at all.
That lookup runs under a
2-second watchdog deadline — a real lookup measures in the low tenths of a
second, comfortably under it; a stale-but-present index is not one of the
fail-open cases: it still answers from whatever it has, which is why keeping
it current (`reindex`, or just committing the store) is worth doing. Nothing
in this tool can stop you from working.

The same idea runs in the other direction. When files have been edited under no
decision topic at all, or when the conversation has moved on for a while right
after real edits, the session gets **nudged** that something here might be worth
writing down. Neither trigger reads what you typed. If a nudge fired just before
a compaction, it survives into the session on the other side.

**Nothing is captured automatically.** A record is written deliberately, and a
ruling quoted as the owner's own words is meant to be written only after the
owner has seen and confirmed the exact text. That is an authoring rule
(`docs/SCHEMA.md` §5), not something the code can enforce; what the linter does
enforce is narrower — an owner-quoted ruling missing its text or its source is
rejected. A store that quietly guesses at what someone meant is worse than no
store, because it gets trusted the same as one that did not guess.

Against fragmentation, **Anatomy** works in two layers. The authored layer is a
small set of *concept* records: "this is one system; here are the files and
symbols that implement it, the tests that guard it, the decisions that govern
it, and where its boundary runs." Ask `why <symbol>` and you get the concept a
strange piece of code belongs to and its full decision chain — including
whatever alternative was tried and declined — before a well-meaning cleanup
deletes it.

The second layer needs nobody to have written anything: `code-reindex` chunks a
source tree into functions and methods, and `code-search` finds them by what
they do — "a helper that writes a debug image" — instead of by a name you would
have to already know to grep for. A hit inside an authored concept's boundary
carries that concept's id, so the trail from "code that does roughly this" to
"the decision that governs it" still closes when one exists. This is what lets
the `memory-search` skill tell agents to run `code-search` *before* writing a
new helper.

Creating a brand-new source file with the Write tool gets that reminder by
itself: a hook fires the moment the Write tool creates a path that does not
exist yet and asks for the code index to be searched first — the one moment a
duplicate helper is most likely to be written instead of found. It only ever
adds a line of context; it never blocks the write. A file created from the
shell instead does not trigger this particular reminder — like any other
shell-made change, it still lands in the edit ledger once something diffs the
tree.

Further reading: `docs/DESIGN.md` for why the engine is shaped this way,
`docs/SCHEMA.md` for authoring records, and — for maintainers —
[`docs/INTERNALS.md`](docs/INTERNALS.md).

## Who this is for

MemContinuum is built for Claude Code workflows where one lead model acts as
orchestrator: it plans the work, deploys subagents to write code, and
— optionally — consults independent external reviewers as gates and advisors. By
convention the orchestrator is the memory's canonical writer. That is a
governance pattern, not an access control: nothing in the code stops another
role from editing a store file directly. Each store gets
`inbox/{codex,grok,audit}` directories precisely so that "a reviewer proposes a
record" and "the orchestrator writes it up" stay two deliberate steps by habit.
An inbox drop indexes (so `check`/`reindex` count it) but is not first-class:
it types as `inbox` and `search` leaves it out of results by default
(`--include-inbox` widens back) until the orchestrator promotes it into a real
record.

Subagents get the relevant decision history handed to them before they touch a
file with the Edit or Write tools; they do not have to go looking for it. A
subagent that changes a file from the shell instead gets no such hand-off —
the edit still reaches the ledger afterwards, from the tree diff, same as any
other shell-made change.

The engine and the store are plain CLI tools and markdown files, so nothing here
is locked to Claude Code — other agent stacks can adopt the same store. The
automatic hooks, though, are written against Claude Code's hook events and would
need porting to fire the same way elsewhere.

## How this relates to Claude Code's own memory

Claude Code already ships two memory mechanisms: CLAUDE.md (standing
instructions, loaded every session) and auto-memory (notes the agent writes for
itself). MemContinuum replaces neither; it adds the layer both are bad at — a
durable record of *why*, with a clear answer to who actually said it.

Four layers, in order of authority:

1. **Code and tests** decide what the software does today. Nothing outranks
   them.
2. **MemContinuum** records why it got that way. Only a person's own confirmed
   words can block work; an incident or finding with real, checkable evidence
   can force a pause for revalidation (a HOLD) even without the owner's own
   words; an agent's unevidenced guess can inform a decision, never veto one.
3. **CLAUDE.md** holds standing rules for how to work — not knowledge, not
   history.
4. **Auto-memory** holds the agent's working notes: where things stood,
   preferences, machine quirks. Handy, never authoritative.

They divide the work cleanly. What is true for this session only stays in
auto-memory, which loads for free at session start; MemContinuum is deliberately
never auto-loaded. Once a fact graduates into a real ruling it moves into the
store, and auto-memory keeps a one-line pointer to it — never a copy, because
copies drift and a drifted copy gets quoted as if it were still true. The
pre-edit hook is the bridge running the other way: it pulls a store record into
the session exactly when a file it governs gets touched with the Edit or
Write tools (a shell-made change to that same file gets no such pull — only
the after-the-fact ledger entry).

If the same fact lives in two of these places at once, one of them is already
wrong. Pick its one home.

## Requirements

- macOS or Linux/WSL, `bash` 3.2+, `git`. The hooks and the installer run on
  stock macOS with nothing extra to install — no Homebrew, no GNU coreutils.
- Python 3.12+ for the engine itself (the pinned dependency set — numpy
  2.5.2's own wheels — ships for 3.12+); the hooks' own
  Python snippets stay on the standard library plus `PyYAML`, so they are
  content with the older Python macOS ships. A `sqlite3` with FTS5, which
  Python's own `sqlite3` module provides — nothing to install separately.
- ~100 MB of disk for the embedding model, downloaded once the first time
  something actually needs to embed. `--no-embed` and `--mode fts` never trigger
  that download; neither does committing to the store -- the store's
  `post-commit` hook runs a bounded, content-only pass and never touches the
  embedding backend itself, so that download (and the embedding work it
  precedes) happens in the background embed-worker it spawns, never inside
  `git commit`. The index records which model (and dimension) made its
  vectors and never mixes vectors from a different model or dimension into a
  ranking; a changed model means a re-embed, not a silently mixed result.
- Somewhere local for the index. It lives under `~/.memcontinuum/` by default;
  never put it on a synced or cloud-backed drive, where SQLite locking is not
  reliable.

If your shell exports a `PYTHONPATH` that shadows the venv's own site-packages,
prefix direct `memidx.py`/`memlint.py` calls with `PYTHONPATH=`. Every hook
already does this for itself.

## Install

Two layers, and mixing them up is the usual source of confusion:

| | what it sets up | how often | who runs it |
|---|---|---|---|
| `memcontinuum-setup.sh` | the venv, the machine config, and the **user-level** SessionStart detector + `memcontinuum` skill in `~/.claude` | once per machine | you, deliberately |
| `scripts/repo-init.sh` | one store, and this repo's **project-level** hooks | once per repository | the `memcontinuum` skill, after you say yes |

### Once per machine

```bash
bash memcontinuum-setup.sh [--venv DIR] [--python PATH] [--claude-dir DIR]
                           [--no-model-warm] [--dry-run] [--uninstall]
```

`--venv DIR` creates the venv (default `<checkout>/.venv`), or `--python PATH`
uses an existing one that already has `requirements.txt` installed.
`--claude-dir DIR` says where the user-level pieces go (default `~/.claude`).
`--no-model-warm` skips the one-time ~100 MB model download — it is on by
default because, left lazy, that download lands inside somebody's first reindex,
where it looks like a hang. `--dry-run` prints the plan and writes nothing.

Run it with neither `--venv` nor `--python`, from a real terminal, and it asks
which python you want (use this python, or create or reuse the engine venv)
instead of silently picking for you; a scripted or non-interactive run
keeps the create-if-absent default. The menu takes 1, 2 or 3 and nothing else
— any other answer, a bare Enter included, is asked again, three times, and
then the run aborts having written nothing. A venv this step creates is
engine-managed: `scripts/memcontinuum-update.sh --apply --machine` may
reinstall `requirements.lock` into it to reconcile the pinned tree-sitter grammar
wheels and the tree-sitter runtime
(below). A python you point it at with `--python` is not —
that command reports what is missing there instead of installing into it.

This step is what makes an un-initialized repository *noticeable*: the
user-level detector runs at every session start, in every repo on the machine,
and is the only piece of this tool that does.

### Once per repository

```bash
bash scripts/repo-init.sh --project NAME [--store DIR] [--code-root DIR ...]
                          [--claude-dir DIR] [--python PATH]
                          [--bootstrap-venv [DIR]] [--langs LIST]
                          [--never-ext LIST] [--non-interactive]
                          [--dry-run] [--force]
```

`--project NAME` is the only required flag: it is the namespace for everything,
and the index filename (`[A-Za-z0-9._-]` only — it is embedded in every hook
line this installer writes). `--store DIR` is where the markdown store lives;
omit it and the conventional name is used — `<repo>-MemContinuum-Store` beside
the repo you are in, or `MemContinuum-Store` inside the current directory when
that is not a git repo. On a checkout physically on a Windows-mounted drive
under WSL, where a store walk costs seconds rather than milliseconds, the
default instead lands on the WSL disk — `$HOME/dev/<repo>-MemContinuum-Store`,
or bare `$HOME/<repo>-MemContinuum-Store` when `$HOME/dev` does not exist —
and the installer prints one line saying why. That name is keyed on the
checkout's basename alone, so two different checkouts sharing one (two
clients both named `app`, say) never silently share it: when the plain name
already belongs to a different checkout or project, the checkout's own
parent directory disambiguates it instead (`<parent>-<repo>-MemContinuum-
Store`); when even that name is already taken, the installer refuses and
asks for an explicit `--store` rather than guess a third name.

`--code-root DIR` (repeatable) is the code checkout
whose edits should trigger retrieval; omit it entirely for a rationale-only
install, and the two edit-time hooks are simply not wired. `--python PATH`
names the python to run the engine with, and `--bootstrap-venv [DIR]` creates
one and installs `requirements.txt` into it if you have not set the machine up
yet.

The store is always its own git repository, never a subdirectory of the code it
describes. Two refusals protect that, and they behave differently:

- A store location **inside another repo's working tree** is refused so a store
  is never absorbed into unrelated history. `--force` overrides this one.
- An **existing git repo that is not a MemContinuum store** — no `topics/`,
  `incidents/` or `concepts/` directory, no README naming MemContinuum — is
  refused outright (exit 9), so a mistyped `--store` cannot seed store
  directories and a `post-commit` hook into someone else's repo. `--force` does
  **not** override this one; point `--store` somewhere else.

Run it with `--dry-run` first — it prints every path and every hook line and
writes nothing, with one exception: `--bootstrap-venv` really does create the
venv, because the rest of the plan cannot be resolved without a python. If you
pass `--store`, pass `--claude-dir` too: an explicit store can be wired from any
directory, so the installer refuses to guess which `.claude` the hooks belong in
rather than wiring the wrong repo. One gap `--dry-run` does not close: a
mistyped `--code-root` is not checked for existence there, so it just looks
like an empty tree in the census — every language at zero, reading as
"nothing proposed" rather than "you pointed this at nothing" — while a real
(non-dry-run) install does check, and refuses a `--code-root` that does not
exist.

**The census, and being asked before anything is indexed.** With a
`--code-root`, the installer first counts source files by extension — and by
reading the first line of extensionless files, so a `#!/usr/bin/env python3`
script with no `.py` suffix counts as Python too — skipping every language's
usual noise directories, and then asks what to do with what it found:

1. **skip** — wire the hooks, index no code
2. **enable all detected**
3. **select** from the detected set
4. **never mention this extension again** for this wiring (this keeps the
   languages the census proposed and only silences the new-file reminder for
   that one extension) — re-running the installer without `--never-ext`
   resets it

Nothing is enabled without your answer, and the dialogue always shows all three
categories: what it proposes, what this engine supports but did not find, and
what it found but cannot handle at all ("38 `.cs` files — no chunker
available"). You always learn that your language is out of scope; nothing is
silently unavailable.

For scripted runs, or when an agent drives the install: `--langs python,swift`
answers the dialogue, `--non-interactive` skips it and wires no language, and
`--never-ext .cs` is the fourth answer's flag form. The dialogue only happens
when the census actually proposes a supported language; a tree with none just
gets language-less wiring. With no terminal and something to propose, the
installer stops with a message naming the driven flow instead of hanging on a
prompt nobody can answer. Run `memidx.py code-census --root DIR` yourself any
time to see the same breakdown without wiring anything.

Re-running with the same `--project`/`--store`/`--claude-dir` is safe. The
settings file is backed up before every write, only this project's own hook
entries are touched, and two projects can share one `.claude` directory without
either re-run unwiring the other.

## Day to day

**You get asked once.** Open a repository that has never been set up, and at
session start the detector puts one question to you: should this repo keep a
decision store? Answer yes and it is initialized; answer no and this repo is
never asked again; say "not now" and nothing is recorded — you will be asked
next session. A repo that was half-installed is asked too: that is the repair
path, not a settled decision.

**`/memcontinuum` any time.** The skill is how you check, enable, disable, or
reverse the decision for a repository, whenever you want, without waiting to be
asked. It reads the current state, asks you the one question if there is one to
ask, runs the install, and records your answer. Reversal works in both
directions, at any point in a repo's life. Disabling stops the *asking*; the
hooks stay wired until you remove them (see Uninstall). No store is ever deleted
by anything here — a store is its own git history, not an installer artifact.
To stop the question in every repo on the machine at once, not just this one:
`scripts/memcontinuum-decide.sh never-ask`, undone with `ask-again`.

**`/memory-search`** is the other skill: a deliberate, on-demand search of the
store and the code index, rather than the automatic per-edit lookup (which
only fires for the Edit and Write tools — reach for this skill by hand before
changing a file from the shell instead).

**A nudge is an action item, not a notice.** When the session is reminded that
recent work is not covered by any decision record, the answer is either new
records or an explicit "none — nothing here was a decision". Silently ignoring
it is how a project ends up with a store that stopped being true. Writing a
source file that does not exist yet raises its own reminder — search the code
index before adding a helper that may already be there.

**Where things go.** Decisions, rulings, incidents, and rejected alternatives go
in the MemContinuum store, written at the moment they happen, with the owner's
exact words and a source. Session state, open questions not yet ruled, machine
facts and recovery recipes stay in Claude Code's auto-memory, which keeps at
most a one-line pointer to a store record. Never both; never copy content across
the two.

**The commands a person actually types.** The store's own `README.md` lists
most of these with your paths already filled in — not the code-index or
maintenance commands (`code-search`, `code-reindex`, `embed-worker`, `stats`,
`backend-preflight`):

```bash
memidx.py why <symbol-or-path> --project NAME --code-root DIR                    # why is this here?
memidx.py search "<question>" --project NAME --status active                     # search the decisions
memidx.py code-search "<what it does>" --project NAME                            # find existing code
memidx.py for-path <file> --project NAME                                         # what governs this file?
memidx.py chain <topic-id> --project NAME                                        # one question's full history
memidx.py drift --code-root DIR --project NAME                                   # has the code grown a bypass?
memidx.py reindex --root STORE --project NAME                                    # after editing the store by hand
memidx.py embed-worker --root STORE --project NAME --db DB                       # run the background embed backfill by hand
memidx.py code-reindex --code-root DIR --project NAME                            # after the code moved on
memidx.py stats --project NAME [--days 7] [--store DIR]                          # is retrieval actually firing?
memidx.py backend-preflight [--json]                                             # which backends import here, and do their versions match the pins?
memlint.py STORE --code-root DIR                                                 # validate records
```

`--project NAME` is not optional in practice: leave it out and everything goes
to a shared `default` namespace and its `default.sqlite`, mixing projects into
one index. `search` defaults to `active` records — plus anything that never
carries a `status` of its own at all (a `sources/` file, an `inbox/` drop) —
with no flag needed for the common case; `--status any` widens to superseded, declined,
provisional and historical rulings too, and plain (non-`--json`) output does
not show each hit's status, so a stray one there reads as current. `drift` is the one
that reads rulings written with a checkable shape ("all deletes go through the
one gate") and turns them into failing checks when the code quietly grows a
way around them. `stats` is the health check: it reads `hook.log` and reports,
per project, whether the read side (pre-edit lookups) and the write side
(nudges vs. this project's own ledger appends) actually fired in a trailing
window — every hook here fails open, so a dead hook and a healthy one that
found nothing look identical from inside a session; `stats` is what tells them
apart, and flags the silent side when it finds one.

A few more things worth knowing: committing the store refreshes its text
index at once (the post-commit hook's own bounded, content-only pass), so
`reindex` by hand is only for edits you have not committed yet; vectors
follow in the background (a detached embed-worker the same hook spawns,
coalescing repeated commits into one pass), and `stats`/`check --json` show
the backlog (`embedding_backlog`) while it catches up. A
project can have several code roots — `code-search` says whether a root's
index is proven current, metadata-checked, stale, or incomplete, rather than
returning an empty list that reads like "nothing found", and `--verify-content`
proves it by hashing every indexed file; either way it heals a stale or
incomplete index itself before searching, in one attempt, when the drift is
small enough. A failed cleanup — the rare case where the index itself cannot
be safely updated for one file or record — fails the reindex with a non-zero
exit instead of reporting success, and internal errors are named by reason
rather than folded into a bare "something went wrong". Run any command with
`--help` for its full flag list.

A constraint or hold ruling can also be mirrored at its bound symbol with a
`decision: TOP-xxxx Ln` comment — the linter checks both ways with
`--code-root`: a marker naming a decision the store does not carry (or one
that is not a constraint or hold) is an error, and a constraint whose symbol
carries no marker yet is a warning.

Name the decision the change lands under in the commit message too
(`TOP-xxxx Ln`): the session nudges once when a commit names none and its
edited files carry no topic.

**Keeping a wired repo up to date.** A fix that only touches a script (a hook,
`memidx.py`, `memlint.py`) reaches every wired repo the moment you pull —
nothing to run. A fix that changes what gets *rendered* into a repo (the hook
lines in its settings, its rules file, its copy of the search skill) needs
`scripts/memcontinuum-update.sh` re-run there. `scripts/memcontinuum-state.sh`
prints an `update:` line naming the repair command when this repo's wiring is
out of date.

Read its silence narrowly, though. What that line compares is the *stamp on
this repo's wired hook line* against the engine's fingerprint — so silence
means the hook lines were rendered by this checkout, and nothing more. It does
not notice a rules file that has gone missing, a `store=` that no longer
matches what is wired, or a store that has been renamed or deleted out from
under the wiring. The check that looks at all of those is
`scripts/memcontinuum-update.sh --dry-run`, which writes nothing and prints a
row per repository and claude-dir.

That distinction is real, not a rule of thumb. Each rendered artifact carries
a fingerprint of the things it was rendered *from* — the templates, the
installer, the skill copies — so pulling a change to a hook script leaves
every repository reading as current, and changing a template flips exactly the
repositories that need re-rendering.

The machine-wide pieces (the detector, the skill in your own user-level Claude
directory) are tracked the same way but separately, under `--machine`: a
change there is reported where the one command that fixes it applies, rather
than as drift in every repository you have ever wired. Setup records which
directory it installed those into, so if you gave it `--claude-dir`, that is
the one reported and refreshed — never a second copy at the default path.

Run `memcontinuum-update.sh` with no flags and it only prints a table: one line
per repository and claude-dir, saying what is current, what has drifted, and
what it will not touch. `--apply` does the work. It can re-render wiring; it
can never create a store — if the store a row names has been renamed or
deleted, the row is reported `store-missing` and skipped, because re-running
the installer against a missing store would seed a new, empty one in its
place.

Two things it will not decide for you. **A project's set of claude-dirs is
named by a person, never discovered.** A project can have more than one (a
session-home `.claude` beside a bare checkout, say), and nothing on disk says
how many — so for a repository wired before the registry recorded them, the
table *proposes* the one it can find and waits: you name the full set with
`--apply --repo PATH --claude-dir DIR [--claude-dir DIR ...]`, and a second
claude-dir only ever joins a repository's record by being named on such a
command line. Likewise, wiring old enough not to record which languages it
indexes is reported as needing `--langs`, not quietly recorded as indexing
none — that would switch off code indexing for a project that had it on.

**A repository has one set of languages, and it applies to all of its
claude-dirs.** So when such a repository is being brought onto the new record
format, each of its claude-dirs is read separately, and if they turn out to
disagree — different code roots, different languages — the table says so and
stops rather than picking one and re-rendering the others to match. You settle
it with `--code-root`, `--langs` and `--set-never-ext` on the command line.

## Languages

The code index handles **Swift and Python**, both natively — Swift with a tuned
walker, Python with the standard library's own parser — plus **JavaScript,
TypeScript, TSX, Java, PHP, Rust, and Lua** through tree-sitter: one generic
backend, a grammar and a query file per language, so a new language in this
tier is a data row and a query, not a new parser. 

More languages follow the same path: a pinned grammar, a query file, a table row 
and a fixture corpus. Contributions in that shape are welcome.

TypeScript and TSX are two languages here, not one. `--lang typescript` covers
`.ts` files; `.tsx` files need `--lang tsx`, and the census offers `tsx` as its
own proposal alongside `typescript`. A React project wants both. They share one
grammar and one query file, which is why they behave identically otherwise —
but wiring only `typescript` leaves every `.tsx` file unindexed, and the
provenance line at the end of `code-reindex` says so by extension.

A project chooses its languages once, at install, through the census dialogue
above — not by flag guesswork. `--lang` is **required on a project's first**
`code-reindex` and reused from then on, so nothing is ever indexed under a
language nobody chose.

Nothing is skipped in silence. Every `code-reindex` ends with a line naming each
unindexed extension and how many files it passed over — whether the engine
cannot handle that language or you simply did not enable it. A file whose
chunker fails outright is reported by name, and whatever was previously indexed
for it is removed rather than left behind as an answer nothing on disk still
backs. A growing blind spot that nobody can see is the failure mode these rules
exist to prevent.

With several `--code-root` directories, every one of them is indexed and
searchable — `code-reindex`/`code-search` are scoped per root, so repairing
one root never touches another's rows. The write-side hooks watch every root
too: the edit ledger records which root an edit landed under, and the
coverage/look-back nudges classify edits across all of them. The new-file
reminder is the one per-root hook: it is wired for, and fires under, every
`--code-root` given.

## Uninstall

Machine level: `bash memcontinuum-setup.sh --uninstall` removes the user-level
detector hook, the skill, and the machine config. It never touches a venv, a
store, or any per-repo wiring. It also keeps `decisions.tsv` on purpose: your
answers survive a reinstall.

Project level, by hand:

1. Remove this tool's hook entries from the project's `settings.local.json` — or
   restore `settings.local.json.bak-memcontinuum`.
2. Delete `<claude-dir>/skills/memory-search/`.
3. Delete `<store>/.git/hooks/post-commit`.
4. Delete `~/.memcontinuum/<project>.sqlite`, and
   `~/.memcontinuum/<project>-code.sqlite` if `code-reindex` was ever run, and
   `~/.memcontinuum/<project>.embed-pending`, `<project>.embed.lock` and
   `<project>.embed.log` if the post-commit hook's embed-worker ever ran, and
   `memidx-debug.log` if any command ever degraded on an internal error (or
   wherever `MEMCONTINUUM_HOME` points).

Leave `<store>` itself alone. It is your decision history, not an installer
artifact.

## Running the tests

```bash
export MEMCONTINUUM_PYTHON="$PWD/.venv/bin/python"   # or wherever the venv is
PYTHONPATH= "$MEMCONTINUUM_PYTHON" -m unittest discover -s tests
bash tests/run_bash32.sh
```

The second command re-runs the hook suites under a real bash 3.2.57 — the
interpreter stock macOS ships — building one into `~/.cache/bash32` on first use
(or point `MC_BASH32` at an existing binary). The full suite takes about five
minutes on a 24-core machine; the bash 3.2 harness takes about four. A handful
of tests need machine-local data of their own and skip with a clear message
when it is absent; every fixture tracked in this repository is synthetic.

Leaving `$MEMCONTINUUM_PYTHON` unset does not read as "OK (skipped=N)" —
dozens of whole test classes need the pinned venv (tree-sitter, embeddings,
dependency reconciliation), and one guard test (`tests/test_env_gate.py`)
fails the run instead, naming the variable and how many classes would
silently skip. Set `$MEMCONTINUUM_ALLOW_UNGATED=1` to run without the venv
anyway, accepting the skipped coverage.

## Acknowledgements & prior art

- [fastembed](https://github.com/qdrant/fastembed) — the embedding runtime this
  engine calls for vector search.
- [BAAI/bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5) — the
  embedding model `fastembed` loads by default here.
- [SQLite FTS5](https://www.sqlite.org/fts5.html) — the full-text half of hybrid
  search.
- [Claude Code hooks](https://code.claude.com/docs/en/hooks) — the mechanism the
  retrieval and reminder hooks are built on.
- [Basic Memory](https://github.com/basicmachines-co/basic-memory) — its
  local-markdown-first philosophy, files as the real store and a database as a
  derived index, directly influenced the storage model here.

## License

MIT — see `LICENSE`.
