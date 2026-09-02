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
prints a backward-compatible `state=` line -- `wired`, `declined`,
`partial-wired` (no recorded decision, and only SOME of the five hooks are
present -- SessionStart's detector DOES ask here, same as `undecided`: a
half-wired repo with no recorded decision is still an open question, not a
grandfathered install), `undecided`, `not-a-repo`, or `no-config` -- plus
the repo key, store path and project name where they apply. Report
`decision`/`wiring` plainly when they disagree; never guess either one from
the presence of a directory.

## 2. Ask, if the state is `undecided` or `partial-wired`

One question, no advocacy. What the human is deciding: whether this repo should
keep an append-only record of *why* its decisions were made — rulings,
incidents, rejected alternatives — indexed and surfaced to agents when they
touch related code. It costs a git repo for the store, seven hooks in the
project's `.claude/settings.local.json`, and the habit of writing records.

It earns its keep on a codebase with contested history that outlives one
person's memory. It is overhead on a scratch repo, a fork you don't own, or
anything you will delete next week. Say which of those you think this repo is,
and let them decide.

## 3. Act on the answer

**Yes → initialize.** Two facts are needed and both are the human's call:

- `--project NAME` — the index namespace, also `<NAME>.sqlite`. Must match
  `[A-Za-z0-9._-]+` (repo-init.sh refuses anything else — it is embedded as
  an identity marker in every hook command line).
- `--store DIR` — where the store lives. It must be **its own git repo**, and
  by default `scripts/repo-init.sh` refuses a location inside another repo's working
  tree (`--force` overrides); it also refuses an existing git repo at `DIR` that carries
  none of this tool's markers (this protects against a mistyped `--store`
  landing store directories in an unrelated repo; adopt an existing store by pointing at
  one that already has `topics/`/`incidents/`/`concepts/` or a README mentioning
  MemContinuum). **Naming convention: the folder is called
  `MemContinuum-Store`** — marked as this tool's, never a
  generic `memory/` (collides with other memory systems) and never bare
  `MemContinuum` (reads as the tool itself). Omit `--store` and repo-init
  applies the convention on its own: `<repo>-MemContinuum-Store` beside the
  git repo the cwd is in, else `MemContinuum-Store` inside the cwd. Only pass
  `--store` when the human wants a different place — and when you do, pass
  `--claude-dir` alongside it (`repo-init.sh` refuses an explicit `--store`
  with no explicit `--claude-dir` rather than guess which `.claude` its hooks
  belong in).
- `--code-root DIR` — repeatable; the code checkout(s) whose edits should
  trigger retrieval. Omit for a rationale-only store.

**If the state was `partial-wired`** (some but not all five write-side hooks
already present, no recorded decision), a human "yes" is a repair, not a
fresh install: re-run `scripts/repo-init.sh` with the same `--store`/
`--project` the repo already has (from step 1's `store=`/`project=` lines) so
it completes the missing wiring — `memcontinuum-decide.sh wired` refuses
anything short of `wiring=full` and names the missing hooks. Only after
`repo-init.sh` reports full wiring does `decide.sh wired` succeed.

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

## 4. Reversing an earlier answer

Either direction, at any point in a repo's life:

- declined → wanted: run the `scripts/repo-init.sh` steps above, then
  `memcontinuum-decide.sh wired --repo REPO ...`.
- wired → unwanted: `memcontinuum-decide.sh declined --repo REPO` records it,
  but that only stops the *asking*. The hooks stay wired until they are removed — see
  README.md "Uninstall". Tell the human which of the two they want; do not
  delete a store, ever. A store is its own git history, not an installer
  artifact.
- never ask in any repo on this machine: `memcontinuum-decide.sh never-ask`;
  undo with `memcontinuum-decide.sh ask-again`.

## 5. Rules

- Never initialize without an explicit yes in this conversation.
- Never delete or move an existing store.
- Never write a decision the human did not give you.
- If `state=no-config`, MemContinuum was never bootstrapped on this machine.
  Point at `memcontinuum-setup.sh`; do not run it unasked.
