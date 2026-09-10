---
name: memcontinuum
description: Use when deciding whether a repository should keep a MemContinuum decision store, when the SessionStart detector reports an undecided repo, or when someone asks to enable, disable, or check MemContinuum for a project. Records the human's answer so it is never asked again, and can reverse an earlier answer at any time.
---

# MemContinuum — per-repository decision

MemContinuum is installed once per machine (`memcontinuum-setup.sh`). Whether any given
repository *keeps a decision store* is a separate, per-repo choice, and it is
the human's to make. This skill is the only thing that records that choice.

**A hook may report the state. Only this skill, after a human has answered,
writes a decision down.** If you were triggered by the SessionStart detector
and the human has not actually answered yet, stop and ask them first.

## 1. Read the current state before saying anything

```bash
# config.sh is sourceable shell (values are single-quoted by sh_quote) --
# SOURCE it, never parse it with sed/tr: any quote-stripping of your own
# breaks the moment the quoting style changes.
#
# Two steps, because the fixed default path may hold only a POINTER
# (a custom-HOME install also writes a minimal config.sh at
# $HOME/.memcontinuum recording just the real MEMCONTINUUM_HOME -- see
# memcontinuum-setup.sh "3. config"). Source the default/env path first;
# if that just redefined MEMCONTINUUM_HOME to a different directory, it
# was a pointer -- follow through and source the REAL config.sh there too,
# or MEMCONTINUUM_ENGINE stays empty under a custom HOME.
MC_HOME_CONFIG_1="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}/config.sh"
[ -f "$MC_HOME_CONFIG_1" ] && . "$MC_HOME_CONFIG_1"
if [ -n "${MEMCONTINUUM_HOME:-}" ] && [ "$MEMCONTINUUM_HOME/config.sh" != "$MC_HOME_CONFIG_1" ]; then
    . "$MEMCONTINUUM_HOME/config.sh"
fi
ENGINE="$MEMCONTINUUM_ENGINE"
bash "$ENGINE/scripts/memcontinuum-state.sh" REPO     # the repo you're deciding for, not $PWD
```

It prints `decision=` (`wired`, `declined`, or `none` -- the human's recorded
answer, when there is one) and `wiring=` (`full`, `partial`, or `none` --
what the repo's `.claude` settings actually contain right now, plus
`missing=<basenames>` when partial) as two SEPARATE facts: a hand-edited
settings file or an interrupted install can leave them disagreeing. It also
prints a combined `state=` line -- `wired`, `declined`, `partial-wired` (no
recorded decision, and only SOME of the always-wired hooks are present --
SessionStart's detector DOES ask here, same as `undecided`: a half-wired
repo with no recorded decision is still an open question, not a settled
one), `undecided`, `not-a-repo`, or `no-config` -- plus the repo key, store
path and project name where they apply. `wired` also covers an install
wired before the decision registry existed: `decision=none` with
`wiring=full` reads as already wired, since the wiring itself is the
evidence. Report `decision`/`wiring` plainly when they disagree; never guess
either one from the presence of a directory.

## 2. Ask — a structured prompt for every state, never destructive by accident

One question, no advocacy: report each option in the words given below it —
its cost where one is named there, otherwise what it does — then stop; never
add a cost or a justification of your own, never argue for one, never read
whether this particular repo deserves it. **No
option ever deletes a store** (Section 5 makes this absolute, on its own).
**Where the repo already has a recorded answer (`wired`, `declined`), the
first option offered always keeps it, unchanged; where it does not, an
option that changes nothing — `Not now` — is always present, and it always
records nothing.** All three rules govern every state below, not only
`undecided`.

Read `state=` from step 1 and ask through the interface's structured multiple-choice
prompt, never prose. What the human is deciding, underneath
every state's wording below: whether this repo keeps an append-only record of
*why* its decisions were made — rulings, incidents, rejected alternatives —
indexed and surfaced to agents when they touch related code. It costs a git
repo for the store, hooks wired into the project's `.claude` settings (the
dry-run's plan prints a `code root   :` line per root for retrieval, and how
many hook groups merge into each event — not the individual hook script
names; read the plan's own lines to the human rather than restating them),
and the habit of writing records.

**`undecided`** — four options:

1. Yes, with code retrieval — records, plus decisions surfaced before edits
   under the named code root
2. Yes, rationale only — records, no code retrieval
3. No — record the decline; this repo is never asked again
4. Not now — nothing is recorded; you will be asked again next session

**`partial-wired`** (some but not all write-side hooks already present, no
recorded decision) — three options:

1. Complete the wiring — finishes what a prior install left half-done
2. Remove what is there
3. Not now — leave it half-wired; asked again next session

**`wired`** — first report the state as facts, straight out of step 1's own
output (`decision`, `wiring`, `decided_at` — present only when a registry row
exists; a repo whose `wired` reading comes purely from `decision=none` +
`wiring=full` has none, and step 1's output omits it there, not a blank
value — `store`, `project`, `settings`),
then offer:

1. Keep as is — nothing changes
2. Stop using MemContinuum here — record the decline (the hooks stay wired
   until removed by hand; say so)

Moving a wired store, or changing its code-root set, is not offered as a
prompt option here: this skill has no read-back of the repo's current
complete parameter set, so it will not guess at what a re-run should ask
for. The underlying tool can still do both, safely — see step 4 for the
supported path (`repo-init.sh --adopt-only` with the complete desired set)
and how a human finds their current one. If the human wants either, say so
plainly and point at step 4.

Neither option here repairs broken wiring: `state=wired` also covers
`decision=wired` with `wiring=partial` (a hand-edited or interrupted
settings file). "Keep as is" leaves it broken; there is no third PROMPT
option that fixes it, but it is not stranded — step 4's same complete-set
`repo-init.sh --adopt-only` path repairs it too. Say so plainly if step 1
showed that combination, and point at step 4.

**`declined`** — two options:

1. Keep declined
2. Wire it after all

**`not-a-repo` / `no-config`** — no prompt: state the fact and stop. These
are the only states where a prompt would be theatre.

Act on whichever option the human picks using step 3 below (a fresh
`undecided` install, or the `partial-wired` repair) or step 4 (every other
change to an existing answer).

## 3. Act on the answer

**Yes → initialize.** Two facts are needed and both are the human's call:

- `--project NAME` — the index namespace, also `<NAME>.sqlite`. Pick whatever
  name the human wants; if `repo-init.sh` refuses it, its own message names
  the characters it allows.
- `--store DIR` — where the store lives. **Naming convention: the folder is
  called `MemContinuum-Store`** — marked as this tool's, never a generic
  `memory/` (collides with other memory systems) and never bare
  `MemContinuum` (reads as the tool itself). Getting it right on an explicit
  `--store` is the human's call.

  Omit `--store` and let `repo-init.sh` pick the default. **Always dry-run
  first, and read the `store :` line — and any `note:` line above it — out
  of that dry-run's own output, verbatim, to the human.** Never predict,
  describe, or explain where the default will land, on any platform: the
  rule that computes it lives in `scripts/mc-registry-lib.sh` and can change
  without this skill knowing, so the only honest answer is whatever the tool
  just printed.
- `--code-root DIR` — repeatable; the code checkout(s) whose edits should
  trigger retrieval. Omit for a rationale-only store.

**"Complete the wiring" on a `partial-wired` repo** is a repair, not a fresh
install — but ONLY when this repo has no existing code retrieval: check
`<claude-dir>/settings.local.json` (and `settings.json`) yourself for a
`PreToolUse` entry naming `pre-edit-chain.sh`. If neither file has one,
re-run `scripts/repo-init.sh` with the same `--store`/`--project` the repo
already has (from step 1's `store=`/`project=` lines) plus
`--record-decision`, so it completes the missing wiring and records it in
one step. If either file DOES have one, do
not run the bare repair: omitting the existing `--code-root`/`--langs`/
`--never-ext` flags would erase or clear part of the existing wiring
instead of completing it, and this skill will not reconstruct that flag
set for you from memory. This is not stranded, though: step 4's "wired →
a different store location, or a different code-root set" bullet gives
the verified recovery route for every field of that complete set —
including the one field, language NAMES, that cannot be read back
verbatim, and what to do about that honestly — and the same
`repo-init.sh --adopt-only` path, with `--record-decision` added, completes
the missing hooks and records it, without dropping retrieval.

**"Remove what is there" on a `partial-wired` repo**: no decision was ever
recorded (`decision=none`), so there is no registry row to touch — just the
hooks that already exist. Remove them by hand the same way as README.md
"Uninstall" step 1.

Always dry-run first, show the plan, then run it. **With a `--code-root`,
never invoke `repo-init.sh` bare and interactive** — you (Claude Code's Bash
tool) have no tty, so a bare run against a repo with any supported-language
files hits `repo-init.sh`'s own `[ -t 0 ]` guard and fails with a clear
message (exit 12) rather than hanging; the message itself names the driven
path below. Run the driven flow instead:

1. Census the code root yourself, with the resolved python and `PYTHONPATH=`
   cleared (same resolution `repo-init.sh` uses — `$MEMCONTINUUM_PYTHON`,
   else `$ENGINE/.venv/bin/python`):
   ```bash
   PY="${MEMCONTINUUM_PYTHON:-$ENGINE/.venv/bin/python}"
   PYTHONPATH= "$PY" "$ENGINE/memidx.py" code-census --root DIR --json
   ```
2. Present the three categories to the human in conversation, reading all
   three straight out of that JSON — no separate list of known languages is
   needed. Each key maps to `{"files": N, "status": "supported"|"unsupported"}`:
   - **proposed** — `status: "supported"` with `files > 0` (the key is the
     language name),
   - **supported but not found** — `status: "supported"` with `files == 0`
     (every language this engine version knows always appears, at zero when
     the tree holds none of its files),
   - **unsupported** — `status: "unsupported"` (the key is the extension, or
     `"(no extension)"`).

   Offer **skip**, **enable all detected**, or **select** a subset. A fourth
   option: the human can name an extension the new-file reminder should
   never mention again — pass `--never-ext .cs` (comma-separated for
   several) alongside whichever language choice they made. It does not
   change which languages are enabled.
3. Run `repo-init.sh` with the human's answer turned into a flag — never
   bare, and the flag goes on the `--dry-run` preview line too (the census
   block, unlike the existence check, runs under `--dry-run` as well — a
   preview command with no tty and no bypass flag hits the same `[ -t 0 ]`
   guard and exit 12 that this whole driven flow exists to avoid):
   ```bash
   # human chose specific languages, or "enable all detected":
   cd REPO && bash "$ENGINE/scripts/repo-init.sh" --project NAME --code-root DIR --langs "chosen,langs" --dry-run
   cd REPO && bash "$ENGINE/scripts/repo-init.sh" --project NAME --code-root DIR --langs "chosen,langs" --record-decision
   # -- or, for "skip":
   cd REPO && bash "$ENGINE/scripts/repo-init.sh" --project NAME --code-root DIR --non-interactive --dry-run
   cd REPO && bash "$ENGINE/scripts/repo-init.sh" --project NAME --code-root DIR --non-interactive --record-decision
   ```

For a rationale-only install (no `--code-root` at all), there is nothing to
census or ask about — the bare two-line dry-run-then-run form is fine:

```bash
cd REPO && bash "$ENGINE/scripts/repo-init.sh" --project NAME --dry-run
cd REPO && bash "$ENGINE/scripts/repo-init.sh" --project NAME --record-decision
```

`--record-decision` (real run only — a no-op under `--dry-run`) records
`wired` the same way running `memcontinuum-decide.sh wired` by hand would
(store, project, and claude-dir all included) — **and**, because it goes
through the same shared builder `repo-init.sh` uses internally
(`mc_build_wiring_args`, `scripts/mc-registry-lib.sh`), it also records
this install's `--code-root`/`--langs`/`--never-ext` into the registry
note as `code-roots=`/`langs=`/`never=`. Step 4 depends on that note to
recover this repo's complete parameter set later without asking the human
to remember it — this is exactly the case `--record-decision`'s own help
text asks for: "from a driven flow where a human has already said yes."
When a row already exists for this repo, its recorded claude-dirs/code-
roots are unioned with this install's, never dropped. Use it on every
real (non-dry-run) `repo-init.sh` run this skill drives — including the
bare `partial-wired` repair re-run above and step 4's repair and
reconfiguration paths below — not only a fresh install.

**Recording `wired` for a repo that is ALREADY fully wired** (step 1 showed
`decision=none`, `wiring=full` — wired before the decision registry
existed, nothing to install or change) is the one case that legitimately
records without running `repo-init.sh` at all: there is no registry row
yet, so step 4's registry-first recovery has nothing to read, and no
install is needed since the wiring is already there. Recover
`--code-root`/`--never-ext` from the rendered hooks the same way step 4's
fallback does (`MEMCONTINUUM_CODE_ROOTS` off a write-side line,
`MEMCONTINUUM_NEVER_EXTS` off a `newfile-nudge.sh` line, both filtered by
`MEMCONTINUUM_PROJECT`); `--langs` has the same honest gap step 4
describes — ask the human if it is not already known. Pass everything
found:

```bash
bash "$ENGINE/scripts/memcontinuum-decide.sh" wired --repo REPO --store DIR --project NAME --code-root ... --langs ... --never-ext ...
```

**No → record the decline.** One command, and this repo is never asked again:

```bash
bash "$ENGINE/scripts/memcontinuum-decide.sh" declined --repo REPO
```

**"Not now" → record nothing.** Say so and move on; the detector stays quiet
for the rest of this session and asks again next time. An unrecorded maybe is
correct here — do not invent a decision to silence a prompt.

## 4. Reversing or changing an earlier answer

Either direction, at any point in a repo's life:

- declined → wanted ("wire it after all" on a `declined` repo): **first**
  `bash "$ENGINE/scripts/memcontinuum-decide.sh" forget --repo REPO` —
  verified live: `--record-decision` REFUSES to record over a `declined`
  row ("Only an undecided repo may be recorded as wired by the
  installer"), installs the wiring anyway, then leaves the row exactly as
  `declined` with no `code-roots=`/`langs=` recorded — the same failure
  this whole round exists to fix, reintroduced on this one path if
  skipped. Once forgotten, run the `scripts/repo-init.sh` steps above
  (with `--record-decision`) exactly as for a fresh `undecided` install.
- wired → unwanted ("stop using MemContinuum here" on a `wired` repo):
  `memcontinuum-decide.sh declined --repo REPO` records it, but that only
  stops the *asking*. The hooks stay wired until they are removed — see
  README.md "Uninstall". Tell the human which of the two they want; do not
  delete a store, ever. A store is its own git history, not an installer
  artifact.
- wired → a different store location, or a different code-root set: **this
  skill does not drive it** — it has no read-back it can trust for the
  repo's complete current parameter set, and handing `repo-init.sh` an
  incomplete one silently drops existing wiring (the exact failure mode the
  removed relocation/add-retrieval options hit). The product itself
  supports both changes: `repo-init.sh` always replaces prior wiring with
  the invocation's COMPLETE set, and `--adopt-only` wires an EXISTING
  store without creating one — including a store already relocated by hand
  (the store is its own git repo; `mv` it first, then point `--store` at
  the new path). The honest, human-directed path — every field below was
  checked against a live install's actual `settings.local.json` and
  registry row, not assumed from the template or library that produce
  them:

  - `--project` / `--store` / `--claude-dir`: off step 1's own
    `project=` / `store=` / `settings=` lines (the last names the
    directory holding `settings.local.json`).
  - `--code-root` / `--langs` / `--never-ext`: **first check the registry
    row** — `$MEMCONTINUUM_HOME/decisions.tsv`, keyed by step 1's own
    `key=` line. When this repo was recorded with `--record-decision`
    (step 3 now always uses it), the row's note carries `code-roots=`,
    `langs=`, and `never=` verbatim, semicolon-joined. Read them with the
    SAME shared library the installer itself uses — never a hand-rolled
    parse of the TSV or the note:
    ```bash
    source "$ENGINE/scripts/mc-registry-lib.sh"
    if mc_registry_lookup "${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}/decisions.tsv" "KEY"; then
        mc_note_field "$MC_LOOKUP_NOTE" "code-roots"; ROOTS_SEMI="$MC_NOTE_FIELD"
        mc_note_field "$MC_LOOKUP_NOTE" "langs";      LANGS_SEMI="$MC_NOTE_FIELD"
        mc_note_field "$MC_LOOKUP_NOTE" "never";      NEVER_SEMI="$MC_NOTE_FIELD"
        mc_build_wiring_args "$ROOTS_SEMI" "$(printf '%s' "$LANGS_SEMI" | tr ';' ',')" \
                              "$(printf '%s' "$NEVER_SEMI" | tr ';' ',')"
        # ${MC_BUILT_ARGS[@]} is now the exact --code-root/--langs/--never-ext
        # tail for the re-run below -- verified live: a --langs python
        # --record-decision install round-trips through this to
        # `--code-root DIR --langs python`.
    fi
    ```
    An EMPTY field here is ambiguous by construction (`mc_note_field`
    returns "" both for "recorded as empty" and "never recorded") — do
    not read an empty `LANGS_SEMI` as "no languages wired" without also
    checking `wiring=full` and a real `newfile-nudge.sh` line in
    `settings.local.json`; if one exists, the row predates
    `--record-decision` and the field is unknown, not empty.

    **These fields can also OVERSTATE the current set, never understate
    it.** `--record-decision` (and `memcontinuum-decide.sh` called by
    hand) UNIONS a new install's code-roots/langs/never into whatever the
    row already had — verified live: recording `python` then later
    `python,javascript` after dropping the python-only root left the row
    reading `langs=python;javascript` even though only `javascript` was
    still actually wired. The registry note is reliable for recovering a
    set you are ADDING to or leaving unchanged (relocating the store,
    adding a root); it is NOT reliable evidence of what survived a
    REMOVAL. After removing a root or a language, treat the RENDERED
    hooks (`MEMCONTINUUM_CODE_ROOTS`/`MEMCONTINUUM_LANG_EXTS` in
    `settings.local.json`, per the fallback below) as the one source of
    truth for what is wired right now, and the registry note only as a
    hint about what USED to be there.
  - **If the row lacks these fields** (recorded before this round, or by
    hand): `--code-root` can still be recovered independently — read
    `MEMCONTINUUM_CODE_ROOTS`, a JSON array naming every root already
    resolved, off any ONE write-side hook line (`PostToolUse`,
    `SessionStart`, `SessionEnd`, `UserPromptSubmit`, or `PreCompact`;
    **not** the two `PreToolUse` `pre-edit-chain.sh` lines, which carry
    `MEMCONTINUUM_ROOT` and `MEMCONTINUUM_STRIP_PREFIX`, never a
    code-root token). `--never-ext` likewise — read
    `MEMCONTINUUM_NEVER_EXTS` off any `PreToolUse` line naming
    `newfile-nudge.sh` (identical on every root's copy of it) and turn
    each glob back into a bare extension (`*.cs` → `.cs`).
  - **`--langs` is the one field that has no other reliable recovery
    route today, and saying otherwise is what made this recipe fail
    before this round.** The rendered hooks hold only
    `MEMCONTINUUM_LANG_EXTS`, EXTENSION GLOBS (e.g. `'*.py'`), not
    language names — feeding that straight to `--langs` fails outright
    (`ERROR: --langs names an unknown language: *.py`, exit 11).
    Inverting a glob set back to names correctly (handling a partial
    match, an unknown extension, or two languages that could both
    explain the same glob set) is real logic that already exists —
    `mc_update_recover_from_settings` in `scripts/memcontinuum-update.sh`
    — but it is that script's own internal migration helper, not a
    public interface: the whole file runs its top-level CLI the moment
    it is sourced, so calling just that one function safely is not
    possible today, and `memcontinuum-update.sh --repo REPO` on its own
    reports only a migration verdict (`ok`, `migrate-needs-langs`, …),
    never the recovered names themselves. **When the registry lacks
    `langs=`, this skill has no supported way to recover the language
    set — say so plainly and ask the human to state it again**, rather
    than guessing from step 3's `code-census` (that reports what
    languages are PRESENT in the tree right now, a different question
    from what was WIRED, since files can have been added or removed
    since the original install) or reimplementing the updater's own
    inversion by hand.
  - **A `.claude` directory can be shared by more than one project** —
    every hook line above also carries `MEMCONTINUUM_PROJECT=<name>` in
    the same command string, and two projects' entries use the identical
    seven basenames. Before reading anything off a matched line, confirm
    it also carries `MEMCONTINUUM_PROJECT=` followed by THIS repo's own
    project (step 1's `project=` line) — matching on basename alone can
    read another project's roots, extensions, or never-list instead of
    this one's.

  Then re-run naming that complete set plus whichever root, language, or
  never-ext is changing, with `--record-decision` so the registry stays
  complete for next time:
  `bash "$ENGINE/scripts/repo-init.sh" --adopt-only --store DIR
  --claude-dir DIR --project NAME --code-root ... --langs ...
  --never-ext ... --record-decision` — dry-run first, per step 3 (drop
  `--record-decision` on the dry-run line; it is a no-op there but the
  dry-run preview should still mirror the real command otherwise). This
  is a deliberate command run at the human's direction, not a step 2
  prompt option.

  **If the new `--langs` set drops a language the project had before**
  (removing the only root that used it, say), this re-run's own
  code-reindex step fails on purpose (verified live: `--lang javascript
  drops python from the project's stored language set python,javascript;
  ... pass a superset, or --full to change it for every root`, exit
  nonzero) — `repo-init.sh` has no `--full` passthrough of its own. The
  hook wiring is still updated correctly at this point; only the code
  index is left stale. Finish it with one direct call, same code-root,
  project, and new language set, adding `--full`:
  `PYTHONPATH= "$PY" "$ENGINE/memidx.py" code-reindex --code-root DIR
  --project NAME --lang "chosen,langs" --full` (`$PY` as step 3 resolves
  it). Re-running `repo-init.sh` again afterward is not required for the
  wiring or the index. It will not fix the registry either way: verified
  live, a SECOND `--record-decision` run (now exiting 0, since the index
  is already fixed) still left `langs=python;javascript` in the row —
  `--record-decision` unions, so once a language has been recorded it
  stays in the note permanently, whether or not it is still wired (see
  the union caveat under step 4's registry recovery). There is no
  supported command that removes a field from a registry row's note
  short of `memcontinuum-decide.sh forget --repo REPO` (which drops the
  WHOLE row, decision included) and recording it again. Say so plainly
  if it matters here; do not claim the registry now reads correctly.
- never ask in any repo on this machine: `memcontinuum-decide.sh never-ask`;
  undo with `memcontinuum-decide.sh ask-again`.

## 5. Rules

- Never initialize without an explicit yes in this conversation.
- Never delete a store, ever, regardless of what is asked. Never move one
  on your own judgment either — step 4's relocation path is a deliberate,
  human-directed command, never something this skill decides or automates
  by itself.
- Never write a decision the human did not give you.
- One question, no advocacy, governs every prompt in step 2, for every
  state — never argue for an option, never read whether a repo deserves
  one.
- Every prompt in step 2 is never destructive by accident: on a repo with a
  recorded answer, its first option always keeps that answer; on a repo
  with none, `Not now` is always offered and always records nothing.
- If `state=no-config`, MemContinuum was never bootstrapped on this machine.
  Point at `memcontinuum-setup.sh`; do not run it unasked.
- When two `status: active` links (in the same topic or across topics)
  genuinely conflict, the citation tiers already decide: the higher tier
  prevails (CONSTRAINT over HOLD over CONTEXT — SCHEMA section 4), and say
  so to whoever asked. Equal tier is not yours to pick between: two
  conflicting agent-level rulings are resolved by the orchestrator writing a
  new link that reverses one of them (`TOP-xxxx Ln`, `kind: reversed`,
  naming the other as the reason); two conflicting owner-level rulings go
  back to the owner, and the owner's answer is recorded as a new
  `owner-verbatim` link, not inferred.
