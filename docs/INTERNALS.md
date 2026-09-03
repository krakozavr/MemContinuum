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

**Logging, per hook.** The seven project-level hooks each write exactly one
`outcome=` line per run to `$MEMCONTINUUM_HOME/hook.log`.
Diagnostic lines may precede it (`pre-edit-chain.sh` logs a missing-python note
before its own `outcome=`). A watchdog kill is included in "every run": the
guarded hook cannot write its own outcome line then — it may be mid-call, or may
never have reached that code — so `mc-watchdog.sh` writes
`outcome=watchdog-killed hook=<name>` itself before exiting. The two hooks
outside that rule are deliberate: `post-commit-reindex.sh` writes its own
`post-commit-reindex: rc=… elapsed=… project=… root=…` line instead, and
`memcontinuum-detect.sh` writes nothing at all unless
`$MEMCONTINUUM_DETECT_LOG` is set — it runs in every repo on the machine, so its
default is silence.

**Writable surface.** The write-side hooks may write
`$MEMCONTINUUM_HOME/sessions/<project>/` and `hook.log`, and nothing else —
never the store, never the code root. Two of them reach the decision index's own
SQLite cache as well, and only that: `userprompt-remind.sh`'s coverage check
calls `memidx.py unmapped`, which self-heals a drifted index with a
`reindex --no-embed`, and `precompact-persist.sh` runs the same self-healing
`unmapped` call for ledger entries under the code root — or a plain
`reindex --no-embed` when the session only touched the store.

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
`rules-missing`, `rules-stale`, `rules-foreign`, `skill-foreign`, `migrate`,
`migrate-needs-claude-dirs`, `migrate-needs-langs`,
`migrate-needs-never-exts`, `migrate-dirs-disagree`, `store-missing`,
`no-wiring`, or
`unrecoverable`. `--dry-run` (the default with no `--apply`) only prints;
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
questions, and only then the drift it can actually re-render (`stale`,
`store-mismatch`, `rules-missing`, `rules-stale`, a missing/stale skill copy
folded into `stale`, `ok`). Ordering them the other way would name some
lesser drift in the action column and then have `--apply` call the installer
just to watch it refuse for a reason already known.

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

An index written by an older engine is rebuilt on first use, keeping its
roots and languages: `open_code_db` compares the stored schema version
against the current one and, on a mismatch, drops and recreates every
derived table (`chunks`, `fts`, `embeddings`, `file_sha`, `code_meta`) inside
one transaction, carrying the old `code_meta`'s `(project, code_root, langs)`
rows forward into the fresh tables first — an older schema keeps its roots
and languages in `code_meta` itself, since `code_project` (where `langs`
lives from schema v2 on) does not exist there yet. `embedding_mode` does
**not** survive: the fresh `code_project` row this rebuild inserts always
starts at `none`, since an older schema tracked no such concept to carry
forward — so the first `code-search` after a rebuild reads the project as
`stale` (never `uninitialized`, since its roots and languages did survive)
and, if it heals, heals without embeddings until a `code-reindex` without
`--no-embed` runs.

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
`head_sha`). `chunks` and `file_sha` carry `code_root` in their key, so a
project can index several trees at once without one root's rows colliding
with another's.

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

**`code_index_report(project)`** is the preflight both `code-search` and the
heal consult, one entry per recorded root. States, in order: **uninitialized**
(no `code-reindex` has ever run for this project — no `code_meta` rows at
all); **stale** (some root has files that changed, were removed, or moved to
a new chunker version since the last `code-reindex` — a removed file counts
as changed too); **degraded** (nothing changed, but some root has a
`not-indexed` file, a backend-availability change since a `not-indexed` row
was stamped, or a recorded root missing on disk — a missing root can never
read `current`); otherwise **current**. `failed` files are never part of this
state calculation at all — an index with only `failed` files reads `current`,
and `code-search` reports the failed count as its own separate line.
The preflight is otherwise read-only: the one write it may commit is
refreshing a drifted-looking file's stored `mtime`/`size` once its content
turns out unchanged (sha256 still matches) — cache bookkeeping so the same
file isn't re-hashed on the next call, never a chunk, status, or meta row.

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
on its own. `code-search` prints `index healed` only when the state after
healing reads `current`; any exception during the heal is fail-open — the
original report stands and the search still answers from whatever was
already indexed.

**Multi-root output.** `code-search --json` wraps hits in an envelope:
`state`; `code_root`/`indexed_at`/`head_sha`, naming the first recorded root
alphabetically; `code_roots`, the full per-root report list (each entry
carrying its own `indexed_at`/`head_sha`); `changed`, `failed`,
`not_indexed`, `embedding_mode`; and `results`. In plain output, a hit's
location is qualified with its root
(`root/path:line`) only once a project has more than one recorded root — the
common single-root case keeps its plain `path:line` line, since only a
multi-root project can have the same relative path indexed under two roots
at once.

The write-side hooks stay single-root, for a different reason: `memlib.sh`
carries one `MEMCONTINUUM_CODE_ROOT`, so the edit ledger — and therefore the
coverage and look-back nudges that read it — only ever sees the first
`--code-root` given. `newfile-nudge.sh` is the one per-root hook: it gets its
own wired entry, with its own `MEMCONTINUUM_CODE_ROOT`, for every
`--code-root` given.

## memlint

`memlint.py ROOT [--code-root DIR ...]` imports memidx's own walker, so a
session buffer is never linted as a topic, and reuses
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

Concept-record rules (`type: concept` files). `--code-root` is repeatable —
one project can have several code roots, and every root given is checked:

| rule | severity |
|---|---|
| (`--code-root`) an `implemented_by`/`tested_by` path is absolute | error |
| (`--code-root`) a relative path escapes every code root given (a `../` that walks outside all of them) | error |
| (`--code-root`) a path does not exist under any code root given | error (names every root tried) |
| (`--code-root`, several roots) a path exists under more than one code root | error (one reference must name one file) |
| (`--code-root`) a `#symbol` fragment matches nothing the chunker recognizes in that file | error |
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
filesystem walk rather than a git one.

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
the file stays indexed. The branch is taken on a YAML parse error and nothing
else, so it is invisible to any record that parses — which is every well-formed
hand-authored one. It exists for pre-existing markdown a project wants indexed
as-is.

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
  differs by mode: for plain `fts`/`vector` the ranked list is computed and
  then filtered (which cannot change which allowed records place, since nothing
  outside the set was ever a candidate); for `hybrid` each side is filtered
  **before** fusion, so a filtered-out record can never occupy a rank position
  that shifts the fused score of a survivor. The FTS side of that ranked list
  is not the full one: `fts_ranked` runs `ORDER BY bm25(fts) ... LIMIT 200`
  scoped to `--project` alone, before `--status`/`--type`/`--area`/`--topic`/
  `--authority` are known at all — a record that would pass every filter but
  ranks below 200th on raw bm25 for this project is never returned by that
  query, so FTS itself never considers it as a candidate, regardless of what
  the filters would have allowed. The vector side carries no such cap —
  `vector_ranked` scores every embedded row for the project — and `hybrid` is
  the default mode, so a record the FTS window missed can still surface
  through RRF fusion on the strength of its vector rank alone; only `--mode
  fts` on its own loses it outright.
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
- **`drift`** — checks every active link's checkable `invariant:` against a code
  tree.
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
  missing root separately when they apply. `--json` wraps hits in
  `{"state", "code_root", "code_roots", "indexed_at", "head_sha", "changed",
  "failed", "not_indexed", "embedding_mode", "results"}`. A "nothing found" is
  only evidence when `state` is `current`.

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
