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
# SOURCE it, never parse it with sed/tr: quote-stripping broke the moment
# the quoting style changed (regate finding, 2026-08-31).
. "${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}/config.sh"
ENGINE="$MEMCONTINUUM_ENGINE"
bash "$ENGINE/scripts/memcontinuum-state.sh"          # add a path to inspect another repo
```

It prints one `state=` line: `wired`, `declined`, `undecided`, `not-a-repo`, or
`no-config`, plus the repo key, store path and project name where they apply.
Report that state plainly; never guess it from the presence of a directory.

## 2. Ask, if the state is `undecided`

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

- `--project NAME` — the index namespace, also `<NAME>.sqlite`. No `/`.
- `--store DIR` — where the store lives. It must be **its own git repo**, and
  by default `scripts/repo-init.sh` refuses a location inside another repo's working
  tree (`--force` overrides). **Naming convention (owner ruling 2026-08-31):
  the folder is called `MemContinuum-Store`** — marked as this tool's, never a
  generic `memory/` (collides with other memory systems) and never bare
  `MemContinuum` (reads as the tool itself). Omit `--store` and repo-init
  applies the convention on its own: `<repo>-MemContinuum-Store` beside the
  git repo the cwd is in, else `MemContinuum-Store` inside the cwd. Only pass
  `--store` when the human wants a different place.
- `--code-root DIR` — repeatable; the code checkout(s) whose edits should
  trigger retrieval. Omit for a rationale-only store.

Always dry-run first, show the plan, then run it:

Run from the repo being initialized, WITHOUT --store, so the conventional
default applies (and --claude-dir defaults to that repo's own .claude):

```bash
cd REPO && bash "$ENGINE/scripts/repo-init.sh" --project NAME --code-root DIR --dry-run
cd REPO && bash "$ENGINE/scripts/repo-init.sh" --project NAME --code-root DIR
```

Then record it:

```bash
bash "$ENGINE/scripts/memcontinuum-decide.sh" wired --store DIR --project NAME
```

**No → record the decline.** One command, and this repo is never asked again:

```bash
bash "$ENGINE/scripts/memcontinuum-decide.sh" declined
```

**"Not now" → record nothing.** Say so and move on; the detector stays quiet
for the rest of this session and asks again next time. An unrecorded maybe is
correct here — do not invent a decision to silence a prompt.

## 4. Reversing an earlier answer

Either direction, at any point in a repo's life:

- declined → wanted: run the `scripts/repo-init.sh` steps above, then
  `memcontinuum-decide.sh wired ...`.
- wired → unwanted: `memcontinuum-decide.sh declined` records it, but that
  only stops the *asking*. The hooks stay wired until they are removed — see
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
