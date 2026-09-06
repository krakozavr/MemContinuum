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
[CLI semantics](#cli-semantics) ·
[tests](#test-conventions)

---

## Hooks and the fail-open contract

Nine hook scripts live under `hooks/`, alongside three shared libraries
(`memlib.sh`, sourced by the five write-side hooks; `mc-watchdog.sh`; and
`mc-path-lib.sh`, one pure side-effect-free function -- symlink-safe
containment -- sourced by `memlib.sh` and directly by `newfile-nudge.sh`,
which needs it but deliberately does not source `memlib.sh` itself).
Seven of the nine are wired into a project's
`.claude/settings.local.json` by `scripts/repo-init.sh`; `post-commit-reindex.sh`
is invoked from the store's own git `post-commit`; `memcontinuum-detect.sh` is
wired one level up, into `~/.claude/settings.json`, by `memcontinuum-setup.sh`.

| script | event | does |
|---|---|---|
| `pre-edit-chain.sh` | `PreToolUse` (Edit/Write, filtered to `--code-root`) | `for-path` lookup on the file being edited; injects matching chains as `additionalContext` |
| `newfile-nudge.sh` | `PreToolUse` (Write only, filtered to `--code-root`) | fires only when the write target does not exist yet and its extension is wired for this project; injects one reminder to search the code index first |
| `ledger-post-edit.sh` | `PostToolUse` (every tool; no settings-level matcher) | a bash-only prefilter exits before the watchdog for read-only built-ins (`Read`, `Grep`, ...); `Edit`/`Write`/`MultiEdit`/`NotebookEdit` ledger the tool's own file path (`source: tool`); `Bash` and any tool this hook has no dedicated branch for fall through to a shell-diff (`git status`) tree comparison against a per-root baseline (`source: shell-diff`); an unrecognized or missing `tool_name` additionally logs `outcome=unsupported-mutation-surface` |
| `precompact-persist.sh` | `PreCompact` | persists session state before context is compacted away |
| `sessionstart-remind.sh` | `SessionStart` | on `startup`/`resume`/`clear`, initializes session state only (captures the code/store roots' git HEAD, prunes state older than 24h; `clear` resets the session's counters and pending nudges but carries the edit ledger over, `resume` keeps everything); only on `source: compact` does it inject what `precompact-persist.sh` left pending |
| `userprompt-remind.sh` | `UserPromptSubmit` | never reads the prompt text; fires the coverage or look-back nudge |
| `sessionend-stamp.sh` | `SessionEnd` | stamps session end into state |
| `post-commit-reindex.sh` | store's git `post-commit` | a bounded content-only reindex after every commit; spawns a background embed-worker when vectors are left behind |
| `memcontinuum-detect.sh` | `SessionStart`, user level | classifies an un-initialized repo and asks once; no python, no watchdog, no logging by default |

**Fail-open is the contract, not a fallback.** No hook may block an edit or a
commit — not on a missing python, not on a stale index, not on a lookup error,
not on its own timeout. A hook that cannot do its job logs and exits 0. The
reason is asymmetric cost: a missed reminder costs one un-recorded ruling; a
hook that blocks an edit costs the user their tool, and the first thing anyone
does with a tool that blocks edits is remove it. Failing open also names its
reason: a degraded answer carries `reason_code`, `exception_type` and a safe
message; the traceback goes to `memidx-debug.log`; `--debug` re-raises.

**Logging, per hook.** The seven project-level hooks each write exactly one
`outcome=` line per run to `$MEMCONTINUUM_HOME/hook.log`.
Diagnostic lines may precede it (`pre-edit-chain.sh` logs a missing-python note
before its own `outcome=`). A watchdog kill is included in "every run": the
guarded hook cannot write its own outcome line then — it may be mid-call, or may
never have reached that code — so `mc-watchdog.sh` writes
`outcome=watchdog-killed hook=<name>` itself before exiting. The two hooks
outside that rule are deliberate: `post-commit-reindex.sh` writes its own
`post-commit-reindex: rc=… elapsed=… project=… root=… embed=pending|clean|skipped`
line instead, and
`memcontinuum-detect.sh` writes nothing at all unless
`$MEMCONTINUUM_DETECT_LOG` is set — it runs in every repo on the machine, so its
default is silence.

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
edits (`Edit`/`Write`/`MultiEdit`/`NotebookEdit`) get pre-retrieval, before
the edit happens; shell mutations get best-effort post-detection, after the
fact, bounded by whatever the git-status budget above could see in time.

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
path-key miss after a move the repo reads as `undecided` and is asked once
more — re-ask, never assume.

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
seven hook command lines, and `<!-- memcontinuum-rendered: <fingerprint> -->`
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
compares each registry row against `repo` and, under `--machine`, the
`~/.claude` detector entry against `machine`; `memcontinuum-state.sh`'s
per-repo hint uses `repo`. `memcontinuum-setup.sh` renders
`MEMCONTINUUM_RENDERED=<machine fingerprint>` onto the one hook line it
installs — that is what `--machine` reads back.

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
claude-dir the row lists, compares four things against the engine right now:
the stamp on that claude-dir's rendered hook lines, the row's own `store=`
against the rendered `MEMCONTINUUM_ROOT` on those same lines (a stamp match
alone cannot catch a store renamed under the same engine version), the rules
file's identity marker + stamp, and the installed `memory-search` skill
copy's identity + stamp. It prints one table row per
(row, claude-dir): `repo | claude-dir | stamped | engine | store-match |
rules | skill | action`, action being one of `ok`, `stale`, `store-mismatch`,
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
at a fixed line number. A missing or stale skill copy reports as the same generic `stale`
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

`--machine` reports the machine layer as one extra line
(`machine: DIR rendered by X, engine at Y -- ok|stale`), comparing the
`machine` fingerprint against the stamp on that dir's detector entry. *Which*
claude-dir is a fact only `memcontinuum-setup.sh` knows — it takes
`--claude-dir` and defaults to `~/.claude` — so it records the answer in
`config.sh` as `MEMCONTINUUM_MACHINE_CLAUDE_DIR`, and this reads it back
(falling back to `~/.claude` only for a `config.sh` written before that
existed). Assuming `~/.claude` reported a real install elsewhere as absent,
and `--apply` would then have rendered a *second* machine layer at the
default path while the stale one stayed stale. With `--apply` it re-runs
`memcontinuum-setup.sh --claude-dir DIR`, but only when the comparison says
`stale`. Off by default, since most drift is per-repo — and a missing
registry no longer skips it, because a machine can perfectly well have its
own layer installed before any repository is wired.

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
`pre-edit-chain.sh`, and `post-commit-reindex.sh` (its own budget,
`MEMCONTINUUM_POST_COMMIT_BUDGET`, default 30 seconds -- generous on purpose:
its guarded content pass is measured well under a second; the budget is a
backstop against a hung/slow filesystem, not a tuned ceiling). Unguarded:
`memcontinuum-detect.sh`.

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
heals, heals without embeddings until a `code-reindex` without
`--no-embed` runs. When the stored version is NEWER than this engine's
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

`memcontinuum-update.sh --apply --machine` runs this check on EVERY `--apply
--machine` run, warns by name about any row missing, and warns separately about
any row off its pins. The report sits outside the staleness gate that decides
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
the project's language set); it reindexes with embeddings only when the
project's own `embedding_mode` is already `full`, otherwise with
`--no-embed` — a heal can never be what silently leaves a `full` project's
new chunks unembedded, but it also never upgrades a `none` project to `full`
on its own. `code-search` prints `index healed` only when EVERY in-process
`code-reindex` call it ran exited 0 AND the state after healing reads
`current` or `metadata-current` (both mean `changed == 0` — the only
difference is whether this call proved it with a full hash) — a non-zero
exit (an integrity failure on some root) prints `heal did not complete
(code-reindex exit N); answering from the current index` instead, never
"healed" over it. Any exception during the heal is fail-open — the original
report stands and the search still answers from whatever was already
indexed.

**Multi-root output.** `code-search --json` wraps hits in an envelope:
`state`; `code_root`/`indexed_at`/`head_sha`, naming the first recorded root
alphabetically (`indexed_at` here is that root's `last_indexed_at`, renamed
at this top level only); `code_roots`, the full per-root report list (each
entry carries its own `last_indexed_at`/`head_sha`/`git_delta`/`verified`,
not renamed); `changed`, `failed`, `not_indexed`, `embedding_mode`; and
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

`memlint.py ROOT [--code-root DIR ...]` imports memidx's own walker, so a
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
| a `status`/`authority`/`kind` value outside the schema enums | error |
| an edge `rel` outside the seven enumerated relations | error |

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

**Deliberately not implemented:** "a link edited after being recorded (hash
mismatch vs git) → reject". See `docs/SCHEMA.md` §7 — that check belongs where a
canonical store's commits are made, not inside the linter.

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
that keeps non-record markdown out of the walk.

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

**Readers never hash; `check` and `unmapped` are the proof.** Hashing every
record on every reader call would put a full store read on the pre-edit hot
path — `for-path` runs twice per pre-edit under its own watchdog, on a store
whose stat-only walk already costs over a second on a slow (drvfs/9P)
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
`hooks/pre-edit-chain.sh` (which passes `--root "$MEMCONTINUUM_ROOT"` on both
its `for-path` calls whenever that env var is set), is logged under its own
hook.log outcome name, `index-stale-served`, distinct from a plain `matched`
— the staleness caveat itself reaches `hook.log` only via `for-path`'s own
stderr (the hook's existing redirect), never the injected `additionalContext`
payload.

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
stored `dim` column lies. Before either embedding zip (decision or code
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
post-commit hook's content pass leaves `N > 0`, it touches
`$MEMCONTINUUM_HOME/<project>.embed-pending` and spawns
`embed-worker --root … --project … --db …` detached
(`subprocess.Popen(start_new_session=True)` from Python -- never bash `&`,
never a `setsid` binary, which macOS does not ship; stdio redirected to
`<project>.embed.log`, never the shared `hook.log`). `embed-worker` takes a
non-blocking `fcntl.flock` on `<project>.embed.lock` first -- a second
worker finding it already held exits 0 immediately, so two quick commits
never run two overlapping embedding passes. It then loops: while the marker
exists, note its mtime, run the embedding backfill in-process (`cmd_reindex`
with embedding enabled), and on success remove the marker ONLY if its mtime
is still what was noted before the pass -- a commit landing mid-pass
retouches the marker, and the loop runs again rather than declaring victory
over content it never saw. On any exception from the backfill (a locked db,
a genuine bug), the marker is left in place, the traceback is appended to
`<project>.embed.log`, and the process exits 3 -- a crash is therefore
always retriable: the next commit, or a manual `embed-worker` run, picks the
marker back up. `check --json` and `stats --json` both report
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

**RRF, not score blending**, for hybrid search (`k=60`): bm25 scores and cosine
similarities live on incomparable scales, so any weighted sum of the two is
arbitrary.

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

## CLI semantics

`--help` on each subcommand is the flag reference. The semantics worth writing
down:

- **`search`** — `--mode fts` and `--mode vector` never both run; `hybrid` (the
  default) runs both and fuses ranks with RRF. Filters (`--status`, `--type`,
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
- **`code-search`** — same RRF fusion as `search`. Each hit optionally carries a
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
