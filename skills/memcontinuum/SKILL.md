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
recorded decision, and only SOME of the five hooks are present --
SessionStart's detector DOES ask here, same as `undecided`: a half-wired
repo with no recorded decision is still an open question, not a settled
one), `undecided`, `not-a-repo`, or `no-config` -- plus the repo key, store
path and project name where they apply. `wired` also covers an install
wired before the decision registry existed: `decision=none` with
`wiring=full` reads as already wired, since the wiring itself is the
evidence. Report `decision`/`wiring` plainly when they disagree; never guess
either one from the presence of a directory.

## 2. Ask — a structured prompt for every state, safe option first

One question, no advocacy: report what each option costs, then stop — never
argue for one, never read whether this particular repo deserves it. **Invoking
this skill is never destructive by accident: the first option offered is
always the one that changes nothing.** Both rules govern every state below,
not only `undecided`.

Read `state=` from step 1 and ask through the interface's structured multiple-choice
prompt, never prose. What the human is deciding, underneath
every state's wording below: whether this repo keeps an append-only record of
*why* its decisions were made — rulings, incidents, rejected alternatives —
indexed and surfaced to agents when they touch related code. It costs a git
repo for the store, hooks wired into the project's `.claude` settings (the
dry-run's plan lists exactly which, and how many), and the habit of writing
records.

**`undecided`** — four options:

1. Yes, with code retrieval — records, plus decisions surfaced before edits
   under the named code root
2. Yes, rationale only — records, no code retrieval
3. No — record the decline; this repo is never asked again
4. Not now — nothing is recorded; you will be asked again next session

**`partial-wired`** (some but not all write-side hooks already present, no
recorded decision) — three options:

1. Complete the wiring — nothing has changed yet; finishes what a prior
   install left half-done
2. Remove what is there
3. Not now — leave it half-wired; asked again next session

**`wired`** — first report the state as facts, straight out of step 1's own
output (`decision`, `wiring`, `decided_at`, `store`, `project`, `settings`),
then offer:

1. Keep as is — nothing changes
2. Change where the store lives
3. Add or remove code retrieval
4. Stop using MemContinuum here — record the decline (the hooks stay wired
   until removed by hand; say so)

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
  `MemContinuum` (reads as the tool itself). `repo-init.sh` does not enforce
  this name on an explicit `--store` — getting it right is the human's call,
  not a mistake the tool will catch.

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
install: re-run `scripts/repo-init.sh` with the same `--store`/`--project`
the repo already has (from step 1's `store=`/`project=` lines) so it
completes the missing wiring, then record it as below.

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
   cd REPO && bash "$ENGINE/scripts/repo-init.sh" --project NAME --code-root DIR --langs "chosen,langs"
   # -- or, for "skip":
   cd REPO && bash "$ENGINE/scripts/repo-init.sh" --project NAME --code-root DIR --non-interactive --dry-run
   cd REPO && bash "$ENGINE/scripts/repo-init.sh" --project NAME --code-root DIR --non-interactive
   ```

For a rationale-only install (no `--code-root` at all), there is nothing to
census or ask about — the bare two-line dry-run-then-run form is fine:

```bash
cd REPO && bash "$ENGINE/scripts/repo-init.sh" --project NAME --dry-run
cd REPO && bash "$ENGINE/scripts/repo-init.sh" --project NAME
```

Then record it. `--repo REPO` is REQUIRED here: `wired`, `declined`, and
`forget` all silence or unsilence a specific repo permanently, and there is
no safe default for that -- name the repo, don't rely on whatever directory
the shell happens to be sitting in:

```bash
bash "$ENGINE/scripts/memcontinuum-decide.sh" wired --repo REPO --store DIR --project NAME
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

- declined → wanted ("wire it after all" on a `declined` repo): run the
  `scripts/repo-init.sh` steps above, then
  `memcontinuum-decide.sh wired --repo REPO ...`.
- wired → unwanted ("stop using MemContinuum here" on a `wired` repo):
  `memcontinuum-decide.sh declined --repo REPO` records it, but that only
  stops the *asking*. The hooks stay wired until they are removed — see
  README.md "Uninstall". Tell the human which of the two they want; do not
  delete a store, ever. A store is its own git history, not an installer
  artifact.
- wired → a different store location ("change where the store lives"): move
  the store directory yourself — it is a plain git working tree, nothing
  installer-owned about where it sits — then
  `repo-init.sh --project NAME --store NEWDIR --claude-dir CLAUDE_DIR
  --adopt-only` (carry over the `--code-root`/`--langs`/`--never-ext` the
  repo already has, from step 1's lines) so the hooks point at the new path,
  then `memcontinuum-decide.sh wired --repo REPO --store NEWDIR
  --project NAME ...` to update the registry.
- wired → add code retrieval: the driven `--code-root` flow under "Yes →
  initialize" above, for the new root, then `memcontinuum-decide.sh wired
  --repo REPO --code-root DIR ...` naming every code-root the repo should
  have going forward (`decide.sh` replaces the row's code-roots, it does not
  union them with what was recorded before).
- wired → remove code retrieval: there is no flag that drops a `--code-root`
  from wiring once installed — removing the PreToolUse hook itself is a
  hand-edit, same as README.md "Uninstall" step 1. Then re-run
  `memcontinuum-decide.sh wired --repo REPO ...` naming only the code-roots
  that remain, so the registry matches.
- never ask in any repo on this machine: `memcontinuum-decide.sh never-ask`;
  undo with `memcontinuum-decide.sh ask-again`.

## 5. Rules

- Never initialize without an explicit yes in this conversation.
- Never delete a store, ever, regardless of what is asked. Never move one
  either, except the one flow in step 4 that exists precisely because a
  human explicitly chose "change where the store lives" — outside that
  flow, on your own initiative, a store never moves.
- Never write a decision the human did not give you.
- One question, no advocacy, governs every prompt in step 2, for every
  state — never argue for an option, never read whether a repo deserves
  one.
- The first option in every prompt in step 2 is always the one that changes
  nothing: invoking this skill is never destructive by accident.
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
